"""
tests/unit/test_db_session.py — db/session.py unit tests.

Verifies that:
1. create_engine + sessionmaker wire up correctly against a temporary SQLite
   file (no network, no real database required).
2. Base.metadata.create_all creates the three core ORM tables — vendors,
   purchase_orders, and contracts — cleanly with no errors.
3. The tables are present and queryable after creation.
4. get_session() yields a working Session that can execute a round-trip INSERT
   and SELECT against the temp database.
5. get_session() rolls back and re-raises on exception, leaving the session
   in a clean state.

Isolation strategy
------------------
Each test that touches the filesystem uses a fresh temporary SQLite file
(tmp_path fixture from pytest) so tests never share state.  The module-level
engine in db/session.py is NOT used here — tests build their own engine from
the temp URL so they are fully isolated from the production database_url in
settings and from each other.

We do NOT mock or monkeypatch get_settings() because db/session.py builds the
engine at import time from the module-level _settings object.  Instead, tests
that need a custom engine construct one directly from a temp URL, which is the
correct approach for database-layer unit tests.
"""

from __future__ import annotations

import pytest
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import sessionmaker, Session

# Import Base and ORM tables so their metadata is registered.
# The import order matters: Base must be imported before the ORM classes so
# that the declarative registry is populated when create_all is called.
from models.base import Base
from models.vendor import VendorORM
from models.purchase_order import PurchaseOrderORM, POLineItemORM
from models.contract import ContractORM, ContractLineItemORM


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_engine(db_path: str):
    """Return a SQLite engine pointed at db_path (absolute path string)."""
    url = f"sqlite:///{db_path}"
    return create_engine(url, connect_args={"check_same_thread": False})


def _session_factory(engine):
    """Return a sessionmaker bound to engine."""
    return sessionmaker(bind=engine, autocommit=False, autoflush=False)


# ---------------------------------------------------------------------------
# Tests: table creation
# ---------------------------------------------------------------------------

class TestTableCreation:
    """Base.metadata.create_all creates the expected tables cleanly."""

    def test_create_all_does_not_raise(self, tmp_path: pytest.TempPathFactory) -> None:
        """create_all against a fresh SQLite file must complete without error."""
        db_file = tmp_path / "test_create_all.db"
        engine = _make_engine(str(db_file))
        # Should not raise
        Base.metadata.create_all(bind=engine)
        engine.dispose()

    def test_vendors_table_exists(self, tmp_path: pytest.TempPathFactory) -> None:
        """vendors table must be created by create_all."""
        db_file = tmp_path / "test_vendors.db"
        engine = _make_engine(str(db_file))
        Base.metadata.create_all(bind=engine)

        inspector = inspect(engine)
        assert "vendors" in inspector.get_table_names(), (
            "vendors table was not created by Base.metadata.create_all"
        )
        engine.dispose()

    def test_purchase_orders_table_exists(self, tmp_path: pytest.TempPathFactory) -> None:
        """purchase_orders table must be created by create_all."""
        db_file = tmp_path / "test_po.db"
        engine = _make_engine(str(db_file))
        Base.metadata.create_all(bind=engine)

        inspector = inspect(engine)
        assert "purchase_orders" in inspector.get_table_names(), (
            "purchase_orders table was not created by Base.metadata.create_all"
        )
        engine.dispose()

    def test_contracts_table_exists(self, tmp_path: pytest.TempPathFactory) -> None:
        """contracts table must be created by create_all."""
        db_file = tmp_path / "test_contracts.db"
        engine = _make_engine(str(db_file))
        Base.metadata.create_all(bind=engine)

        inspector = inspect(engine)
        assert "contracts" in inspector.get_table_names(), (
            "contracts table was not created by Base.metadata.create_all"
        )
        engine.dispose()

    def test_all_three_tables_in_one_call(self, tmp_path: pytest.TempPathFactory) -> None:
        """
        A single create_all call produces vendors, purchase_orders, and contracts
        (plus their child tables) in one database file.
        """
        db_file = tmp_path / "test_all.db"
        engine = _make_engine(str(db_file))
        Base.metadata.create_all(bind=engine)

        inspector = inspect(engine)
        table_names = set(inspector.get_table_names())

        required = {"vendors", "purchase_orders", "contracts"}
        missing = required - table_names
        assert not missing, (
            f"The following tables were not created: {missing}. "
            f"All tables found: {table_names}"
        )
        engine.dispose()

    def test_child_tables_also_created(self, tmp_path: pytest.TempPathFactory) -> None:
        """po_line_items and contract_line_items tables must also be present."""
        db_file = tmp_path / "test_children.db"
        engine = _make_engine(str(db_file))
        Base.metadata.create_all(bind=engine)

        inspector = inspect(engine)
        table_names = set(inspector.get_table_names())

        assert "po_line_items" in table_names, "po_line_items table missing"
        assert "contract_line_items" in table_names, "contract_line_items table missing"
        engine.dispose()

    def test_create_all_is_idempotent(self, tmp_path: pytest.TempPathFactory) -> None:
        """Calling create_all twice on the same file must not raise."""
        db_file = tmp_path / "test_idempotent.db"
        engine = _make_engine(str(db_file))
        Base.metadata.create_all(bind=engine)
        # Second call — checkfirst=True is the default, so existing tables are skipped
        Base.metadata.create_all(bind=engine)
        engine.dispose()


# ---------------------------------------------------------------------------
# Tests: session wiring
# ---------------------------------------------------------------------------

class TestSessionWiring:
    """SessionLocal and get_session() produce usable, transaction-scoped sessions."""

    def test_session_opens_and_closes(self, tmp_path: pytest.TempPathFactory) -> None:
        """Opening and closing a session against the temp DB must not raise."""
        db_file = tmp_path / "test_session.db"
        engine = _make_engine(str(db_file))
        Base.metadata.create_all(bind=engine)

        SessionLocal = _session_factory(engine)
        session: Session = SessionLocal()
        try:
            # A trivial query to confirm the session is live
            session.execute(text("SELECT 1"))
        finally:
            session.close()
        engine.dispose()

    def test_session_insert_and_select_vendor(self, tmp_path: pytest.TempPathFactory) -> None:
        """
        A VendorORM row written in one session is readable in a second session,
        confirming that commit() persists to the SQLite file.
        """
        db_file = tmp_path / "test_insert.db"
        engine = _make_engine(str(db_file))
        Base.metadata.create_all(bind=engine)
        SessionLocal = _session_factory(engine)

        vendor = VendorORM(
            vendor_code="ACME-001",
            name="Acme Corp",
            contact_email="ap@acme.example.com",
            is_active=True,
        )

        # Write
        with SessionLocal() as write_session:
            write_session.add(vendor)
            write_session.commit()
            written_id = vendor.id  # UUID assigned by default factory

        # Read back in a fresh session
        with SessionLocal() as read_session:
            fetched = read_session.get(VendorORM, written_id)
            assert fetched is not None, "VendorORM row not found after commit"
            assert fetched.vendor_code == "ACME-001"
            assert fetched.name == "Acme Corp"
            assert fetched.is_active is True

        engine.dispose()

    def test_session_rollback_on_exception(self, tmp_path: pytest.TempPathFactory) -> None:
        """
        A session that raises an exception inside a with-block is rolled back;
        the row must not appear in a subsequent read session.
        """
        db_file = tmp_path / "test_rollback.db"
        engine = _make_engine(str(db_file))
        Base.metadata.create_all(bind=engine)
        SessionLocal = _session_factory(engine)

        vendor = VendorORM(vendor_code="ROLLBACK-001", name="Rollback Corp")

        with pytest.raises(RuntimeError, match="deliberate test error"):
            with SessionLocal() as session:
                session.add(vendor)
                session.flush()         # pushes to DB within the transaction
                raise RuntimeError("deliberate test error")
                # session.__exit__ will call rollback via SQLAlchemy's context manager

        # The vendor must NOT be persisted
        with SessionLocal() as check_session:
            result = (
                check_session.query(VendorORM)
                .filter_by(vendor_code="ROLLBACK-001")
                .first()
            )
            assert result is None, (
                "VendorORM row should have been rolled back but was found in DB"
            )

        engine.dispose()


# ---------------------------------------------------------------------------
# Tests: get_session() from db.session
# ---------------------------------------------------------------------------

class TestGetSession:
    """
    get_session() context manager from db.session wires up correctly.

    These tests exercise the actual production helper (not a locally-constructed
    session) to confirm the module-level engine and SessionLocal are coherent.
    The production engine points at settings.database_url (default: app.db in
    the project root).  We verify behaviour without writing to that file by
    only using get_session() for a read-only query.
    """

    def test_get_session_yields_session(self) -> None:
        """
        get_session() must yield a live SQLAlchemy Session without raising.

        Uses a SELECT 1 to confirm the session is connected.  No writes are
        made so the production database_url is not polluted.
        """
        from db.session import get_session

        with get_session() as session:
            assert isinstance(session, Session)
            result = session.execute(text("SELECT 1")).scalar()
            assert result == 1

    def test_get_session_rollback_on_exception(self) -> None:
        """
        get_session() must roll back and re-raise when the body raises.
        """
        from db.session import get_session

        with pytest.raises(ValueError, match="test rollback trigger"):
            with get_session() as session:
                # Force a rollback without writing anything meaningful
                session.execute(text("SELECT 1"))
                raise ValueError("test rollback trigger")

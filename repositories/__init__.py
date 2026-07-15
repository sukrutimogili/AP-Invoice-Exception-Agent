# repositories/__init__.py — database query layer (Phase 9 wiring point).
#
# Each module exposes a single query function that accepts a SQLAlchemy Session
# (from db.session.get_session) and returns a Pydantic *Create schema or None.
# No FastAPI dependency; usable from any service or test.

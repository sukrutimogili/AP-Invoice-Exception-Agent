# LedgerGate-Agent

AP Invoice & Contract Exception Agent — extracts, validates, matches, and routes invoices against Purchase Orders and Contracts, with an append-only audit trail and early-payment discount optimization.

---

## 1. Project Overview

LedgerGate-Agent automates the accounts-payable matching workflow. Every incoming invoice is extracted from raw text or PDF, validated against a strict schema, matched field-by-field against the referenced PO and Contract, and then either straight-through-processed (STP) for automatic payment scheduling or routed to a human queue with a structured reason code.

The system never fabricates missing fields. Malformed invoices are rejected before matching runs. Every decision is written to an append-only audit trail that reconstructs full history without re-running the agent.

Vendors referenced in uploaded PO or Contract documents that are not yet in the vendor master are auto-created on first upload, with a full audit event emitted only after the DB commit succeeds.

---

## 2. Features

- LLM extraction with retry-and-repair loop; fails closed on second validation failure
- Pydantic v2 schema validation on every module boundary
- Deterministic matching engine — per-line qty, unit price, grand total, approval threshold, vendor known, PO/contract resolved; configurable tolerance
- STP routing for invoices passing all checks; automatic payment scheduling
- Exception routing with structured reason codes: `PRICE_VARIANCE`, `QTY_MISMATCH`, `TOTAL_MISMATCH`, `MISSING_APPROVAL`, `UNKNOWN_VENDOR`, `PO_NOT_FOUND`, `CONTRACT_NOT_FOUND`, `DOCUMENT_CONFLICT`
- Human gate — approve-with-override or reject, both audited with actor identity
- Early-payment discount optimization — annualized return vs. configurable hurdle rate; pure arithmetic, no LLM
- PO/Contract upload with conflict detection — prevents silent data overwrites
- Vendor auto-create — unknown vendor codes trigger automatic vendor master creation; audit event deferred until after commit
- Append-only audit trail queryable by invoice, vendor, PO, date range, and outcome
- Adversarial prompt-injection test suite
- FastAPI REST API with auto-generated OpenAPI docs
- Streamlit UI for invoice processing, audit log, discount optimization, and system info

---

## 3. System Architecture

```
Streamlit UI (streamlit_app.py)    FastAPI (app/main.py)
         |                                  |
ui/components/pipeline_runner.py    api/{invoices,exceptions,payments,audit}.py
         |                                  |
         +------------- services/invoice_service.py -------------+
                                    |
              +---------------------+---------------------+
              |                     |                     |
        extraction/           matching/             routing/
        agent.py              engine.py             decision.py
        (LLM + retry)         (pure math)           (FR-3.1 gate)
              |                     |                     |
              +---------------------+---------------------+
                                    |
                          discount/calculator.py
                          audit/writer.py
                          repositories/{po,contract,vendor}_repo.py
                          db/session.py (SQLAlchemy)
                          SQLite / Postgres
```

Business logic lives exclusively in `matching/`, `routing/`, `discount/`, and `services/` — never in route handlers. `audit/writer.py` is the single write path for all audit events. Callers own every `session.commit()`.

---

## 4. Project Structure

```
ap-invoice-exception-agent/
├── app/                    # FastAPI entry point and config
├── models/                 # Pydantic schemas + SQLAlchemy ORM tables
├── extraction/             # LLM extraction agents, prompts, parser
├── matching/               # Deterministic matching engine
├── routing/                # STP vs. exception routing
├── discount/               # Early-payment discount calculator and parser
├── audit/                  # Append-only audit writer
├── repositories/           # SQL query helpers (po, contract, vendor)
├── db/                     # Session factory and entity resolver
├── services/               # Invoice pipeline orchestrator
├── api/                    # FastAPI route handlers
├── ui/                     # Streamlit pages and pipeline adapter
├── tests/                  # unit/, integration/, security/, golden/, e2e/
├── alembic/                # Database migrations
├── scripts/seed_db.py      # Local dev seed data
├── streamlit_app.py        # Streamlit entry point
├── .env.example
└── pyproject.toml
```

---

## 5. Tech Stack

| Layer | Choice |
|---|---|
| Language | Python 3.11+ |
| API framework | FastAPI 0.115 |
| Validation | Pydantic v2 |
| UI | Streamlit 1.40 |
| LLM gateway | OpenRouter (OpenAI-compatible) |
| LLM fallback chain | `openrouter/auto` → Gemma 3 27B → Qwen3 235B → DeepSeek R1 |
| PDF extraction | pdfplumber 0.11 |
| ORM | SQLAlchemy 2.0 |
| Migrations | Alembic 1.14 |
| Default database | SQLite (`app.db`) — swap to Postgres via `DATABASE_URL` |
| HTTP client | httpx 0.28 |
| Settings | pydantic-settings 2.7 |
| Test runner | pytest 8.3 + pytest-asyncio |
| Linting / formatting | ruff 0.8 + black 24.10 |
| Static analysis | mypy 1.14 strict |

---

## 6. Installation

**Prerequisites:** Python 3.11+, an [OpenRouter API key](https://openrouter.ai/keys), Git.

```bash
git clone https://github.com/sukrutimogili/AP-Invoice-Exception-Agent.git
cd AP-Invoice-Exception-Agent

python -m venv .venv
# Windows
.venv\Scripts\activate
# macOS / Linux
source .venv/bin/activate

pip install -e ".[dev]"

cp .env.example .env
# edit .env — set OPENROUTER_API_KEY

alembic upgrade head

# optional: seed sample vendors, POs, and contracts
python scripts/seed_db.py
```

---

## 7. Configuration (`.env`)

All settings are loaded at startup via `app/config.py` (pydantic-settings). The process fails immediately with a clear error if any required variable is missing.

```dotenv
# Required
OPENROUTER_API_KEY=sk-or-v1-your-key-here

# Optional — defaults shown
OPENROUTER_FALLBACK_CHAIN=["openrouter/auto","google/gemma-3-27b-it:free","qwen/qwen3-235b-a22b:free","deepseek/deepseek-r1-0528:free"]
OPENROUTER_BASE_URL=https://openrouter.ai/api/v1
DATABASE_URL=sqlite:///./app.db
APPROVAL_THRESHOLD_DEFAULT=10000
DISCOUNT_HURDLE_RATE_DEFAULT=0.10
MATCH_TOLERANCE_PERCENT=0.0
LOG_LEVEL=INFO
```

| Variable | Required | Default | Description |
|---|---|---|---|
| `OPENROUTER_API_KEY` | Yes | — | OpenRouter API key |
| `OPENROUTER_FALLBACK_CHAIN` | No | 4-model chain | JSON array of model slugs tried in order |
| `DATABASE_URL` | No | `sqlite:///./app.db` | SQLAlchemy connection string |
| `APPROVAL_THRESHOLD_DEFAULT` | No | `10000` | Approval threshold amount |
| `DISCOUNT_HURDLE_RATE_DEFAULT` | No | `0.10` | Cost-of-capital hurdle rate |
| `MATCH_TOLERANCE_PERCENT` | No | `0.0` | Price/qty variance tolerance (%) |
| `LOG_LEVEL` | No | `INFO` | Logging verbosity |

---

## 8. Running the Project

**FastAPI (REST API)**

```bash
uvicorn app.main:app --reload
# API:      http://localhost:8000
# Docs:     http://localhost:8000/docs
# Health:   http://localhost:8000/health
```

**Streamlit UI**

```bash
streamlit run streamlit_app.py
# Opens:   http://localhost:8501
```

The Streamlit UI calls domain modules directly and does not depend on the REST API running. Both can run independently.

---

## 9. Workflow

```
1. Upload
   Invoice text or PDF submitted via Streamlit or POST /invoices/upload

2. Document Loading
   PDF  → pdfplumber text extraction (rejects scanned/image-only PDFs)
   Text → UTF-8 decode

3. LLM Extraction  (extraction/agent.py)
   Attempt 1 → parse → Pydantic validate
   On failure → retry with error feedback
   On second failure → NEEDS_REEXTRACTION (never fabricate)

4. PO / Contract Upload  (ui/components/pipeline_runner.py)
   Extract PO/contract via their own agents
   Resolve vendor code → UUID  (auto-create if unknown, same transaction)
   upsert_po() / upsert_contract()
     UpsertCreated / UpsertUnchanged → session.commit()
       → write VENDOR_AUTO_CREATED audit event (after commit)
     UpsertConflict → DOCUMENT_CONFLICT exception, no DB write

5. Entity Resolution  (db/resolver.py)
   PO by invoice.po_reference
   Contract by invoice.contract_reference
   Vendor by PO.vendor_id FK

6. Matching  (matching/engine.py)
   vendor_known, po_resolved, contract_resolved,
   quantities_match (per line), prices_match (per line, within tolerance),
   total_matches, approval_satisfied

7. Routing  (routing/decision.py)
   All pass → STPDecision + PaymentSchedule
   Any fail → ExceptionDecision + ExceptionRecord with reason codes

8. Discount Evaluation  (discount/calculator.py)  [STP only]
   annualized_return = (d/(1-d)) × (365/(net_days - discount_days))
   Compare against DISCOUNT_HURDLE_RATE_DEFAULT
   Recommend TAKE_DISCOUNT or HOLD_TO_NET
   Detect DISCOUNT_WINDOW_MISSED if window has lapsed

9. Audit  (audit/writer.py)
   INVOICE_RECEIVED → EXTRACTION_SUCCEEDED/FAILED → MATCHING_COMPLETED
   → STP_APPROVED / EXCEPTION_RAISED → PAYMENT_SCHEDULED
   → DISCOUNT_EVALUATED → VENDOR_AUTO_CREATED → DOCUMENT_CONFLICT_DETECTED
```

---

## 10. Core Data Models

| Model | Key Fields |
|---|---|
| `Invoice` | invoice_number, vendor_name, po_reference, contract_reference, line_items, grand_total, due_date |
| `PurchaseOrder` | po_number, vendor_id (FK), po_total, approval_threshold, line_items |
| `Contract` | contract_reference, vendor_id (FK), discount_term, approval_threshold, line_items |
| `Vendor` | vendor_code (unique), name, is_active, contact_email |
| `MatchResult` | per-line qty/price variance, overall_passed, all FR-2 sub-check booleans |
| `ExceptionRecord` | reason codes + supporting data, status (OPEN/RESOLVED), human action |
| `PaymentSchedule` | invoice_id, scheduled_date, amount, discount_taken |
| `DiscountRecommendation` | discount_pct, annualized_return, hurdle_rate, recommendation, window_missed |
| `AuditEvent` | invoice_id, event_type, payload_json, actor_id, created_at (no updated_at — append-only) |

Vendor resolution always uses `PO.vendor_id` FK — free-text name matching is explicitly rejected as fragile.

---

## 11. Business Rules

**STP eligibility (FR-3.1)** — all must be true:

1. Extraction produced a valid schema
2. Vendor exists in the approved vendor master and is active
3. PO reference resolves to a known PO
4. Contract reference resolves to a known contract
5. All line item quantities match the PO (within `MATCH_TOLERANCE_PERCENT`)
6. All line item unit prices match the contract (within tolerance)
7. Invoice grand total matches PO total (within tolerance)
8. Invoice total is below `APPROVAL_THRESHOLD_DEFAULT`, or an approval is on file

Any single failure routes the invoice to the human exception queue with the specific reason code(s). STP and exception are mutually exclusive — a partial payment schedule is never created.

**Discount recommendation (FR-7.2):**

```
annualized_return = (discount_pct / (1 - discount_pct)) × (365 / (net_days - discount_days))
```

If `annualized_return >= DISCOUNT_HURDLE_RATE_DEFAULT` → `TAKE_DISCOUNT`; otherwise `HOLD_TO_NET`. Only runs on STP-eligible invoices. If the discount window has already lapsed, records `DISCOUNT_WINDOW_MISSED` — this is visibility, not an exception.

**Vendor auto-create:** when a PO or Contract is uploaded with an unknown vendor code, the vendor is inserted in the same DB transaction as the PO/Contract. The `VENDOR_AUTO_CREATED` audit event is written only after `session.commit()` succeeds. If the PO/Contract upsert returns `UpsertConflict`, the entire transaction is rolled back — no vendor row is persisted and no audit event is emitted.

**Document conflict detection:** if an uploaded PO or Contract has the same natural key as an existing row but different field values, the upsert returns `UpsertConflict`. The existing row is never overwritten; the invoice is routed to `EXCEPTION` with reason code `DOCUMENT_CONFLICT` and a field-level diff is recorded.

---

## 12. API Endpoints

All endpoints except `/health` require an internal service token (auth TBD — Phase 9). Pass in `Authorization: Bearer <token>`.

| Method | Path | Description |
|---|---|---|
| GET | `/health` | Liveness probe |
| POST | `/invoices/submit` | Submit a pre-extracted invoice for processing |
| POST | `/invoices/upload` | Upload raw text/PDF invoice; runs extraction then pipeline |
| GET | `/payments/` | List all payment schedules |
| GET | `/payments/{invoice_id}` | Get payment schedule for one invoice |
| GET | `/exceptions/{invoice_id}` | Get exception record |
| POST | `/exceptions/{invoice_id}/approve` | Approve-with-override (human gate) |
| POST | `/exceptions/{invoice_id}/reject` | Reject invoice back to vendor |
| GET | `/audit/invoice/{invoice_id}` | Full ordered audit trail for one invoice |
| GET | `/audit/search` | Filter audit events by vendor, PO, date range, event type |
| GET | `/audit/invoice/{invoice_id}/outcome` | Final outcome event for one invoice |

Full request/response schemas are available at `/docs` (Swagger UI) or `/redoc` when the API is running.

---

## 13. Testing

```bash
# Run all tests
pytest

# Unit tests only (no network, no real DB for most)
pytest tests/unit/

# Integration tests (real SQLite in-memory DB, LLM patched)
pytest tests/integration/

# Adversarial prompt-injection suite (re-run after any prompt change)
pytest -m security

# End-to-end scenarios
pytest -m e2e
```

The test suite covers:

- Schema validation (valid and invalid inputs for every model)
- Extraction agent retry-and-repair loop (mocked LLM responses)
- Matching engine — every FR-2 sub-condition, including edge cases
- Routing — every combination of passing/failing checks
- Upsert logic — create, unchanged, conflict for PO, Contract, and Vendor
- Vendor auto-create — unknown vendor creation, contract reuse, rollback on PO conflict, end-to-end STP chain
- Discount calculator — annualized return formula, all three outcome scenarios
- Prompt injection — adversarial invoice samples must never bypass matching
- PDF upload — text-layer PDFs, scanned/image-only rejection, generic content-type handling

---

## 14. Design Decisions

**Single audit write path.** `audit/writer.py` is the only module that appends to the audit log. No other module calls the store directly. This makes the append-only guarantee enforceable in one place.

**Vendor resolution via FK, not name.** `InvoiceCreate.vendor_name` is free-text extracted by an LLM. It may vary in capitalisation or include trading-name suffixes. Matching by name would silently produce wrong results. Instead, once a PO is resolved, `PurchaseOrderORM.vendor_id` is the unambiguous UUID FK.

**Callers own every commit.** `db/session.py` configures `autocommit=False, autoflush=False`. Repository functions call `session.flush()` (assigns PKs) but never `session.commit()`. This lets the pipeline commit vendor + PO/Contract in a single atomic transaction.

**Deferred audit events.** `VENDOR_AUTO_CREATED` events are written to the audit log only after `session.commit()` returns successfully. The audit trail must never claim a record exists that wasn't durably persisted — a rollback must leave no trace in the audit log.

**LLM for extraction only.** The LLM extracts and parses; it never performs financial arithmetic. Discount math is a pure deterministic Python function. This makes the financial calculation auditable, testable, and independent of model quality.

**Model portability.** Every LLM call prompts for JSON matching an explicit schema, parses defensively (strips markdown fences, attempts repair), validates against Pydantic, and retries once with error feedback. This pipeline works on free-tier models that don't support native JSON mode.

**PO/Contract upsert — no silent overwrites.** Uploading a document whose natural key already exists but with different values returns `UpsertConflict` — the existing row is never overwritten. The diff is surfaced to the user and recorded in the audit trail.

---

## 15. Limitations

- **No OCR.** Scanned or image-only PDFs are rejected. The system requires a text layer.
- **In-process audit store.** The audit log is currently an in-process Python list. It does not survive process restarts and is not shared across workers. Replacing it with a DB-backed store requires only changes to `audit/writer.py`.
- **Single currency.** All monetary comparisons assume the same currency. Multi-currency support is flagged as a configuration extension point but not implemented.
- **Auth is a placeholder.** All API endpoints document their authorization requirement, but enforcement (service token validation) is marked TBD for Phase 9 deployment.
- **No duplicate-invoice detection.** Reprocessing the same invoice ID is idempotent but submitting two different invoice IDs for the same underlying document is not detected.
- **No ERP write-back.** The system schedules payment but does not move money or write back to an ERP system.

---

## 16. Future Improvements

- Persist the audit log to the database (replace the in-process list with `AuditEventORM` writes)
- Add service token authentication to all API endpoints
- Duplicate invoice detection using invoice number + vendor + amount fingerprinting
- Vendor risk scoring from repeated exception history
- Multi-currency support with configurable base currency
- Async SQLAlchemy sessions for higher throughput under concurrent load
- ERP write-back integration (SAP, NetSuite, etc.) as a pluggable output adapter
- OCR support for scanned PDFs via a pluggable extraction-agent upgrade

---

## 17. License

MIT License. See [LICENSE](LICENSE) for details.

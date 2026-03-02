# DELTA Inventory / POS

Production-oriented Django POS and inventory system for multi-branch auto parts operations.

## Features
- Role-aware access model (`admin`, `manager`, `cashier`) via `UserProfile`
- Branch-scoped inventory and sales visibility
- Active-branch enforcement for write operations (checkout/stock/transfers)
- Transaction-safe checkout and stock movements (`atomic` + row locking)
- Location/bin-aware stock tracking with movement history
- Reservation-aware transfer workflow
- Refund workflow with audit trail and idempotency
- Tech-support ticket workflow (create/list/detail, assignment, priority, screenshot)
- Role-restricted AI assistant for stock and transfer actions
- UTF-8 BOM CSV export (Excel-friendly Arabic)
- Arabic-first RTL UI

## Quick Start
1. Create and activate a virtual environment.
2. Install dependencies:
```bash
pip install -r requirements.txt
```
3. Copy env template:
```bash
cp .env.example .env
```
4. Run migrations:
```bash
python manage.py migrate
```
5. Create a superuser:
```bash
python manage.py createsuperuser
```
6. Run the app:
```bash
python manage.py runserver
```

## Environment Variables
Use `.env.example` as reference.

- `DJANGO_SECRET_KEY`
- `DJANGO_DEBUG`
- `DJANGO_ALLOWED_HOSTS`
- `DJANGO_CSRF_TRUSTED_ORIGINS`
- `DJANGO_SECURE_COOKIES`
- `DJANGO_SECURE_SSL_REDIRECT`
- `DJANGO_SECURE_HSTS_SECONDS`
- `DJANGO_SECURE_HSTS_INCLUDE_SUBDOMAINS`
- `DJANGO_SECURE_HSTS_PRELOAD`
- `DJANGO_LOG_LEVEL`
- `DJANGO_DB_ENGINE` (optional: `sqlite3`, `postgresql`, `mysql`)
- `DJANGO_DB_NAME`, `DJANGO_DB_USER`, `DJANGO_DB_PASSWORD`, `DJANGO_DB_HOST`, `DJANGO_DB_PORT` (when not using sqlite)
- `DJANGO_DB_COLLATION` (optional MySQL/MariaDB collation, default `utf8mb4_unicode_ci`)
- `AI_ASSISTANT_ENABLED` (default `True`)
- `AI_ASSISTANT_PROVIDER` (default `openai`)
- `AI_ASSISTANT_MODEL` (default `gpt-4.1-mini`)
- `AI_ASSISTANT_API_KEY` (required for live model mode)
- `AI_ASSISTANT_BASE_URL` (default `https://api.openai.com/v1`)
- `AI_ASSISTANT_TIMEOUT_SECONDS` (default `20`)

## Real AI Copilot (Chat Assistant)
- Endpoint: `/inventory/assistant/` (alias: `/inventory/chat/`)
- Live model mode is enabled automatically when `AI_ASSISTANT_API_KEY` is set.
- If no key is set, the app falls back to deterministic parser mode.
- Write operations still follow server-enforced safety:
  - Draft -> Confirm -> Apply
  - Role/branch permission checks at execution time
  - Audit log + stock movement records for every mutation

## Seed Global Admin Accounts
```bash
python manage.py seed_global_admins
```

The command prompts for each password interactively:
- `saleh` (صالح الجابري) -> `فرع الجمعية`
- `osama` (أسامة الجابري) -> `فرع مخرج 18`
- `abdulaziz` (عبدالعزيز الجابري) -> `فرع الصناعية القديمة`

## Arabic UTF-8 Checklist
- Templates: keep `<meta charset="utf-8">` in base template.
- CSV exports: UTF-8 BOM is already enabled.
- SQLite: Unicode-safe by default (current local engine).
- PostgreSQL (prod): database encoding must be `UTF8`.
- MySQL/MariaDB (prod): use `utf8mb4` + `utf8mb4_unicode_ci` and connection charset `utf8mb4`.
- Django DB settings are configured to enforce:
  - MySQL/MariaDB: `charset=utf8mb4` + init command `SET NAMES utf8mb4 COLLATE ...`
  - PostgreSQL: connection option `client_encoding=UTF8`

## Branch Cleanup Commands
Dry-run first, then apply:
```bash
python manage.py delta_cleanup_garbled_arabic
python manage.py delta_cleanup_branches
python manage.py delta_cleanup_garbled_arabic --apply
python manage.py delta_cleanup_branches --apply
```

Target business branches after cleanup:
- الصناعية القديمة
- مخرج 18
- شارع الجمعية

## User Password Helper (DBP02 / DBP03)
```bash
python manage.py set_branch_user_passwords --generate
```
Or set explicit values:
```bash
python manage.py set_branch_user_passwords --dbp02-password "Dbp02#Pass1" --dbp03-password "Dbp03#Pass1"
```

## Realistic Inventory Seed
Dry-run:
```bash
python manage.py seed_realistic_inventory
```
Apply:
```bash
python manage.py seed_realistic_inventory --apply
```
Reset and reseed:
```bash
python manage.py seed_realistic_inventory --apply --reset
```

## Scanner Notes
- Camera scanning requires HTTPS in browsers (or `localhost` for local dev).
- Hardware scanner mode works via focused barcode input + Enter.
- The camera scanner is loaded locally from `static/vendor/barcode/html5-qrcode.min.js` (no runtime CDN dependency).

## Role Enforcement (Custom App)
- Admin: all operations.
- Manager: reports, stock/location operations, transfer approvals, refunds.
- Cashier: POS + transfer request creation.
- Tech support: ticket management (`abdullah`, superuser, or `tech/support` group).
- Important: custom web views enforce role checks server-side; Django admin model permissions alone are not sufficient.

## Tests
Run the suite with:
```bash
python manage.py test
```

## Export Notes
- CSV export uses UTF-8 BOM for proper Arabic rendering in Excel.
- CSV rows are sanitized to reduce formula-injection risk.
- XLSX export is available from the same endpoint using `?format=xlsx` when `openpyxl` is installed.

## Pre-Deploy Checklist
- `DEBUG` off: set `DJANGO_DEBUG=False`
- For local HTTP-only development: set `DJANGO_DEBUG=True` (or disable secure cookie/SSL redirect vars)
- `ALLOWED_HOSTS`: set `DJANGO_ALLOWED_HOSTS` for your production domain(s)
- `SECRET_KEY` from environment: set `DJANGO_SECRET_KEY` (do not hardcode)
- `CSRF_TRUSTED_ORIGINS`: set `DJANGO_CSRF_TRUSTED_ORIGINS` with full HTTPS origins
- WhiteNoise/static:
  - keep `whitenoise` in dependencies
  - ensure WhiteNoise middleware is enabled
  - run `python manage.py collectstatic --noinput`
- Gunicorn start command:
  - `gunicorn config.wsgi:application --bind 0.0.0.0:$PORT`

## Render Deployment (Minimal)
This repo includes `render.yaml` for a basic web service setup.

1. Push repository to GitHub.
2. In Render, create a **Blueprint** deployment from the repo.
3. Create or attach a PostgreSQL database and set `DATABASE_URL`.
4. Confirm env vars:
   - `DJANGO_DEBUG=False`
   - `DJANGO_SECRET_KEY` (generated secret)
   - `DJANGO_ALLOWED_HOSTS=.onrender.com`
   - `DJANGO_CSRF_TRUSTED_ORIGINS=https://*.onrender.com`
5. Deploy and verify static files, migrations, and app boot.

## Ngrok Local HTTPS
For local tunnel usage, keep these values (or set via env vars):
```python
ALLOWED_HOSTS = [
    "127.0.0.1",
    "localhost",
    ".ngrok-free.dev",
]

CSRF_TRUSTED_ORIGINS = [
    "https://*.ngrok-free.dev",
]
```
Run locally:
```bash
python manage.py runserver 0.0.0.0:8000
```

## Saudi ZATCA QR (TLV Base64)
DELTA stores the QR payload on each posted invoice (`TaxInvoice.qr_payload`) for audit traceability.

Implemented helper (`inventory/zatca.py`):
```python
def generate_zatca_qr(seller_name, vat_number, timestamp_iso, total_amount, vat_amount):
    import base64

    def tlv(tag, value):
        tag_bytes = bytes([tag])
        value_bytes = value.encode("utf-8")
        length_bytes = bytes([len(value_bytes)])
        return tag_bytes + length_bytes + value_bytes

    qr_bytes = (
        tlv(1, seller_name) +
        tlv(2, vat_number) +
        tlv(3, timestamp_iso) +
        tlv(4, f"{total_amount:.2f}") +
        tlv(5, f"{vat_amount:.2f}")
    )

    return base64.b64encode(qr_bytes).decode("utf-8")
```

## Invoice Immutability Rules
- State flow: `DRAFT -> POSTED`.
- At `POSTED`, invoice header/lines are immutable in model validation.
- Reversal must be through `CreditNote`.
- VAT amounts are snapshotted when invoice is created, never recalculated during render.
- Sequential invoice numbering is per branch (`BranchInvoiceSequence`).

## Async SMACC Architecture
- POS checkout is source-of-truth and does not block on SMACC.
- A queue item (`SmaccSyncQueue`) is created when invoice posts.
- Worker command:
```bash
python manage.py process_smacc_sync_queue --limit 25
```
- Webhook endpoint:
`/inventory/webhooks/smacc/`
  - signature verification
  - rate limiting
  - optional IP allowlist
  - queue status updates + sync logs

## Security Notes
- Keep SMACC credentials in environment variables only.
- Do not print access tokens in logs.
- Use webhook signature verification + allowlist where available.
- Keep `DEBUG=False` in production.
- Use HTTPS for camera scanner (`localhost` allowed for development browsers).

## Purchasing / Receiving (New)
- Vendors module:
  - `/inventory/vendors/`
  - `/inventory/vendors/new/`
- Purchase Orders:
  - `/inventory/purchases/`
  - `/inventory/purchases/new/`
  - `/inventory/purchases/<po_id>/`
- Receiving (GRN):
  - `/inventory/receiving/`
  - Create draft GRN from PO detail
  - Add lines manually or by scan code
  - Posting a GRN increases stock, writes stock movements, updates branch average cost, and updates PO status (`DRAFT/SENT -> PARTIAL_RECEIVED -> RECEIVED`)

## Customer Ledger / Payments / Credit Notes (New)
- Customer profile:
  - `/inventory/customers/<customer_id>/`
  - Shows running balance, ledger statement, payments, credit note history
- Payment endpoint:
  - `POST /inventory/customers/<customer_id>/payments/`
- Credit note endpoint:
  - `POST /inventory/orders/<order_id>/credit-note/`
  - Supports return-to-stock and non-stock return options
  - Writes ledger entry (`CREDIT_NOTE`) and stock movements when stock return is enabled

## Barcode Insight + Linking (New)
- Part insight page:
  - `/inventory/parts/<part_id>/insight/`
  - Shows stock per branch + last 10 sales/transfers/purchases
- Unmatched barcode linking:
  - `/inventory/barcode/unmatched/?code=<scan>`
  - Links a new barcode to an existing part (`PartBarcode`)

## Transfer Receive Improvements (New)
- Pick list print page:
  - `/inventory/transfers/<transfer_id>/pick-list/`
- Transfer receive scan endpoint:
  - `POST /inventory/transfers/<transfer_id>/scan-receive/`
- Partial receive supported:
  - `received_quantity` tracked
  - backorder stays open until fully received
  - over-receive blocked unless manager override is explicitly used

## Cycle Count (New)
- Session list/create:
  - `/inventory/cycle-count/`
- Session detail:
  - `/inventory/cycle-count/<session_id>/`
- Flow:
  - `DRAFT -> IN_PROGRESS -> SUBMITTED -> APPROVED/REJECTED`
- On approval, stock adjustments are applied in a transaction and audited.

## Effective Permission Check Command
Inspect what a user can actually do (direct perms + group perms + flags):
```bash
python manage.py delta_check_user_effective_perms <username>
```

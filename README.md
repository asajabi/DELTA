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
- `DJANGO_LOG_LEVEL`
- `DJANGO_DB_ENGINE` (optional: `sqlite3`, `postgresql`, `mysql`)
- `DJANGO_DB_NAME`, `DJANGO_DB_USER`, `DJANGO_DB_PASSWORD`, `DJANGO_DB_HOST`, `DJANGO_DB_PORT` (when not using sqlite)
- `DJANGO_DB_COLLATION` (optional MySQL/MariaDB collation, default `utf8mb4_unicode_ci`)

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

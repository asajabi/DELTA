# DELTA Inventory / POS

Production-oriented Django POS and inventory system for multi-branch auto parts operations.

## Features
- Role-aware access model (`admin`, `manager`, `cashier`) via `UserProfile`
- Branch-scoped inventory and sales visibility for non-manager users
- Transaction-safe checkout with stock locking and negative-stock prevention
- Paginated order/sales/low-stock pages with query optimizations
- Secure sales export (CSV with UTF-8 BOM for Arabic Excel + optional XLSX)
- Manager-only refund workflow with automatic stock restock
- Arabic-first UI with RTL-ready base layout and keyboard-friendly POS workflow
- Automated tests for auth/permissions, checkout, branch isolation, and exports

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

## Assign User Roles / Branches
Use Django admin (`/admin`) and edit `UserProfile`:
- `role`: `admin`, `manager`, or `cashier`
- `branch`: default branch scope for non-manager users

Admin bulk actions on `UserProfile` support:
- quick role assignment (`admin`, `manager`, `cashier`),
- assign selected users to a branch,
- clear selected branch assignments.

## Refund Workflow
- Refund actions are manager-only and available from Sales History.
- Refunding a sale marks `is_refunded=True` and restores sold quantity back to inventory.
- Re-running refund on the same sale is idempotent (no double restock).

## Tests
Run the suite with:
```bash
python manage.py test
```

## Export Notes
- CSV export uses UTF-8 BOM for proper Arabic rendering in Excel.
- CSV rows are sanitized to reduce formula-injection risk.
- XLSX export is available from the same endpoint using `?format=xlsx` when `openpyxl` is installed.

## Deployment Notes (Gunicorn + WhiteNoise)
Typical production stack:
1. Set `DJANGO_DEBUG=False`
2. Configure secure hosts (`DJANGO_ALLOWED_HOSTS`)
3. Enable secure cookies (`DJANGO_SECURE_COOKIES=True`)
4. Run static collection:
```bash
python manage.py collectstatic --noinput
```
5. Run app server:
```bash
gunicorn config.wsgi:application --bind 0.0.0.0:8000
```
6. Serve behind reverse proxy (Nginx/Caddy) with HTTPS.

If you use WhiteNoise, add it to dependencies and middleware for static file serving in production.

# Changelog

## 2026-02-26

### Operations and Data Hygiene
- Added branch governance and cleanup tooling:
  - `enforce_branches`,
  - `delta_cleanup_branches`,
  - `delta_cleanup_garbled_arabic`,
  - `repair_arabic_text`.
- Added utilities for practical environment setup:
  - `seed_realistic_inventory`,
  - `set_branch_user_passwords`.

### Inventory and Workflow Expansion
- Added location-aware stock tracking models and migration flow:
  - `Location`,
  - `StockLocation`,
  - `StockMovement`,
  - `Stock.min_stock_level`.
- Added ticketing workflow (create/list/detail, priority, branch linkage, optional screenshot upload).
- Added AI assistant helper module for guided stock and transfer operations with audit integration.

### Deployment and Assets
- Added `render.yaml` for streamlined Render blueprint deployment.
- Added local scanner assets under `static/` to avoid runtime CDN dependency.

### Quality Status
- Test suite expanded and validated at `91` tests passing.
- `manage.py check`, `makemigrations --check --dry-run`, and `migrate --plan` all clean.

## 2026-02-22

### Security and Access
- Added `UserProfile` model with role + branch scope.
- Enforced branch-aware visibility for inventory, carts, receipts, orders, low stock, and sales reporting.
- Enforced unique, non-empty `employee_id` for staff profiles.
- Hardened redirects (`next` validation) and protected state-changing endpoints with `POST` + CSRF.
- Added secure export response headers and CSV formula-injection mitigation.
- Added refund workflow endpoint (`POST`) with manager-only access checks.
- Added strict transfer permissions:
  - cashiers can request for their branch only,
  - managers approve only for their branch,
  - admins can approve all branches.
- Added admin-only audit log page with employee/branch/action/date filters.
- Added manager/admin-only AI analytics assistant endpoint with branch-scoped read-only query execution.

### Data Integrity and Business Rules
- Added model validators/constraints for non-negative money values and positive sale quantities.
- Added transactional checkout with row locking to prevent race conditions and negative inventory.
- Added order-level branch assignment and migration backfill for existing orders.
- Added transactional refund behavior that marks sales as refunded and restocks inventory safely.
- Added `TransferRequest` lifecycle model and reservation checks.
- Added reservation-aware stock availability for sales/cart flows.
- Added transfer completion stock movement on receiving confirmation.
- Added `AuditLog` model and `employee_id` on `UserProfile` for auditable actor identity.
- Added mandatory `reason` field on audit records and required reason inputs for transfer/refund actions.
- Added audit events with before/after JSON snapshots for:
  - sale creation and refund,
  - stock adjustments,
  - transfer request/approve/reject/pickup/deliver/receive,
  - part price changes,
  - user role changes.

### Performance and Reporting
- Added `select_related`/`prefetch_related` and pagination on heavy list pages.
- Added reports dashboard with revenue/profit summaries, daily totals, and top sellers.
- Upgraded sales export with date/branch filters, streaming CSV, and optional XLSX output.

### UI/UX
- Rebuilt shared base template for cleaner Arabic-first RTL-compatible layout.
- Refreshed login, search, POS console, checkout, order list, low stock, scanner, and sales history templates.
- Added keyboard-friendly POS shortcuts and clearer validation/error feedback.
- Added transfer workflow pages: list, create, approvals, driver tasks, and receiving confirmation.
- Added receipt improvements: customer phone visibility and explicit print/reprint CTA.
- Added reprint shortcut from sales history.
- Added AI assistant page with question input, intent selection, scope filters, and grounded result tables.

### Testing and Documentation
- Expanded automated tests (22 tests) covering:
  - auth and permissions,
  - branch isolation,
  - checkout + stock deduction,
  - export structure/encoding/sanitization.
- Added refund tests for permissions, idempotency, and stock restoration.
- Added transfer tests for stock reservation, completion stock updates, and permission enforcement.
- Added audit tests to validate log creation for sales, refunds, transfers, and admin-side price/role changes.
- Added `README.md`, `.env.example`, and dependency list (`requirements.txt`).
- Added initial CI workflow to run checks and tests on push/PR.
- Added `COMMIT_READY_FILES.md` for clean staging without cache/venv artifacts.

### Admin Productivity
- Added quick admin bulk actions for role updates (`admin/manager/cashier`) and branch assignment/clear on `UserProfile`.
- Added action-form controls to apply role and branch in one step for selected users.
- Added `seed_global_admins` management command to create/update:
  - `saleh` -> `فرع الجمعية`,
  - `osama` -> `فرع مخرج 18`,
  - `abdulaziz` -> `فرع الصناعية القديمة`,
  with prompted passwords, `admin` role, and fixed employee IDs.

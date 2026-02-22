# Changelog

## 2026-02-22

### Security and Access
- Added `UserProfile` model with role + branch scope.
- Enforced branch-aware visibility for inventory, carts, receipts, orders, low stock, and sales reporting.
- Hardened redirects (`next` validation) and protected state-changing endpoints with `POST` + CSRF.
- Added secure export response headers and CSV formula-injection mitigation.
- Added refund workflow endpoint (`POST`) with manager-only access checks.

### Data Integrity and Business Rules
- Added model validators/constraints for non-negative money values and positive sale quantities.
- Added transactional checkout with row locking to prevent race conditions and negative inventory.
- Added order-level branch assignment and migration backfill for existing orders.
- Added transactional refund behavior that marks sales as refunded and restocks inventory safely.

### Performance and Reporting
- Added `select_related`/`prefetch_related` and pagination on heavy list pages.
- Added reports dashboard with revenue/profit summaries, daily totals, and top sellers.
- Upgraded sales export with date/branch filters, streaming CSV, and optional XLSX output.

### UI/UX
- Rebuilt shared base template for cleaner Arabic-first RTL-compatible layout.
- Refreshed login, search, POS console, checkout, order list, low stock, scanner, and sales history templates.
- Added keyboard-friendly POS shortcuts and clearer validation/error feedback.

### Testing and Documentation
- Expanded automated tests (12 tests) covering:
  - auth and permissions,
  - branch isolation,
  - checkout + stock deduction,
  - export structure/encoding/sanitization.
- Added refund tests for permissions, idempotency, and stock restoration.
- Added `README.md`, `.env.example`, and dependency list (`requirements.txt`).
- Added initial CI workflow to run checks and tests on push/PR.
- Added `COMMIT_READY_FILES.md` for clean staging without cache/venv artifacts.

### Admin Productivity
- Added quick admin bulk actions for role updates (`admin/manager/cashier`) and branch assignment/clear on `UserProfile`.
- Added action-form controls to apply role and branch in one step for selected users.

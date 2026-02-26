# Commit-Ready File List

Use this list to commit meaningful project changes from the current working tree.
Exclude runtime artifacts like `venv/`, `__pycache__/`, `db.sqlite3`, and `db.sqlite3-journal`.

```text
CHANGELOG.md
COMMIT_READY_FILES.md
README.md
config/settings.py
inventory/admin.py
inventory/audit.py
inventory/chat_assistant.py
inventory/context_processors.py
inventory/management/commands/delta_cleanup_branches.py
inventory/management/commands/delta_cleanup_garbled_arabic.py
inventory/management/commands/enforce_branches.py
inventory/management/commands/repair_arabic_text.py
inventory/management/commands/seed_global_admins.py
inventory/management/commands/seed_realistic_inventory.py
inventory/management/commands/set_branch_user_passwords.py
inventory/migrations/0007_transferrequest.py
inventory/migrations/0008_userprofile_employee_id_auditlog.py
inventory/migrations/0009_alter_userprofile_employee_id_and_more.py
inventory/migrations/0010_auditlog_reason_and_more.py
inventory/migrations/0011_location_stocklocation_stockmovement_and_more.py
inventory/migrations/0012_stock_min_stock_level.py
inventory/migrations/0013_ticket.py
inventory/migrations/0014_ticket_branch_ticket_priority_ticket_screenshot_and_more.py
inventory/models.py
inventory/signals.py
inventory/templates/inventory/ai_assistant.html
inventory/templates/inventory/audit_log.html
inventory/templates/inventory/base.html
inventory/templates/inventory/locations_list.html
inventory/templates/inventory/low_stock.html
inventory/templates/inventory/order_list.html
inventory/templates/inventory/pos_checkout.html
inventory/templates/inventory/pos_console.html
inventory/templates/inventory/receipt.html
inventory/templates/inventory/reports_dashboard.html
inventory/templates/inventory/sales_history.html
inventory/templates/inventory/scanner.html
inventory/templates/inventory/search.html
inventory/templates/inventory/stock_locations.html
inventory/templates/inventory/ticket_create.html
inventory/templates/inventory/ticket_detail.html
inventory/templates/inventory/ticket_list.html
inventory/templates/inventory/transfer_approvals.html
inventory/templates/inventory/transfer_create.html
inventory/templates/inventory/transfer_create_general.html
inventory/templates/inventory/transfer_driver_tasks.html
inventory/templates/inventory/transfer_receive.html
inventory/templates/inventory/transfers_list.html
inventory/tests.py
inventory/urls.py
inventory/views.py
render.yaml
static/js/barcode_scanner.js
static/vendor/barcode/html5-qrcode.min.js
```

Optional add command:

```bash
git add CHANGELOG.md COMMIT_READY_FILES.md README.md config/settings.py inventory/admin.py inventory/audit.py inventory/chat_assistant.py inventory/context_processors.py inventory/management/commands/delta_cleanup_branches.py inventory/management/commands/delta_cleanup_garbled_arabic.py inventory/management/commands/enforce_branches.py inventory/management/commands/repair_arabic_text.py inventory/management/commands/seed_global_admins.py inventory/management/commands/seed_realistic_inventory.py inventory/management/commands/set_branch_user_passwords.py inventory/migrations/0007_transferrequest.py inventory/migrations/0008_userprofile_employee_id_auditlog.py inventory/migrations/0009_alter_userprofile_employee_id_and_more.py inventory/migrations/0010_auditlog_reason_and_more.py inventory/migrations/0011_location_stocklocation_stockmovement_and_more.py inventory/migrations/0012_stock_min_stock_level.py inventory/migrations/0013_ticket.py inventory/migrations/0014_ticket_branch_ticket_priority_ticket_screenshot_and_more.py inventory/models.py inventory/signals.py inventory/templates/inventory/ai_assistant.html inventory/templates/inventory/audit_log.html inventory/templates/inventory/base.html inventory/templates/inventory/locations_list.html inventory/templates/inventory/low_stock.html inventory/templates/inventory/order_list.html inventory/templates/inventory/pos_checkout.html inventory/templates/inventory/pos_console.html inventory/templates/inventory/receipt.html inventory/templates/inventory/reports_dashboard.html inventory/templates/inventory/sales_history.html inventory/templates/inventory/scanner.html inventory/templates/inventory/search.html inventory/templates/inventory/stock_locations.html inventory/templates/inventory/ticket_create.html inventory/templates/inventory/ticket_detail.html inventory/templates/inventory/ticket_list.html inventory/templates/inventory/transfer_approvals.html inventory/templates/inventory/transfer_create.html inventory/templates/inventory/transfer_create_general.html inventory/templates/inventory/transfer_driver_tasks.html inventory/templates/inventory/transfer_receive.html inventory/templates/inventory/transfers_list.html inventory/tests.py inventory/urls.py inventory/views.py render.yaml static/js/barcode_scanner.js static/vendor/barcode/html5-qrcode.min.js
```

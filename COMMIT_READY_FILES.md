# Commit-Ready File List

Use this list to commit only meaningful source/docs changes (exclude `venv/`, `__pycache__/`, and `db.sqlite3`):

```text
.env.example
.github/workflows/ci.yml
.gitignore
CHANGELOG.md
COMMIT_READY_FILES.md
README.md
config/settings.py
config/urls.py
inventory/admin.py
inventory/apps.py
inventory/context_processors.py
inventory/migrations/0006_userprofile_order_branch_alter_customer_phone_number_and_more.py
inventory/models.py
inventory/signals.py
inventory/templates/inventory/base.html
inventory/templates/inventory/low_stock.html
inventory/templates/inventory/order_list.html
inventory/templates/inventory/pos_checkout.html
inventory/templates/inventory/pos_console.html
inventory/templates/inventory/reports_dashboard.html
inventory/templates/inventory/sales_history.html
inventory/templates/inventory/scanner.html
inventory/templates/inventory/search.html
inventory/templates/registration/login.html
inventory/tests.py
inventory/urls.py
inventory/views.py
requirements.txt
```

Optional add command:

```bash
git add .env.example .github/workflows/ci.yml .gitignore CHANGELOG.md COMMIT_READY_FILES.md README.md config/settings.py config/urls.py inventory/admin.py inventory/apps.py inventory/context_processors.py inventory/migrations/0006_userprofile_order_branch_alter_customer_phone_number_and_more.py inventory/models.py inventory/signals.py inventory/templates/inventory/base.html inventory/templates/inventory/low_stock.html inventory/templates/inventory/order_list.html inventory/templates/inventory/pos_checkout.html inventory/templates/inventory/pos_console.html inventory/templates/inventory/reports_dashboard.html inventory/templates/inventory/sales_history.html inventory/templates/inventory/scanner.html inventory/templates/inventory/search.html inventory/templates/registration/login.html inventory/tests.py inventory/urls.py inventory/views.py requirements.txt
```

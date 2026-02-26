import secrets

from django.contrib.auth.models import User
from django.core.management.base import BaseCommand
from django.db import transaction

from inventory.models import Branch, REQUIRED_BRANCH_CODES, UserProfile


class Command(BaseCommand):
    help = "Create/update DBP02 and DBP03 users, set passwords, and enforce non-superuser roles."

    def add_arguments(self, parser):
        parser.add_argument("--dbp02-password", dest="dbp02_password", default="", help="Password for DBP02.")
        parser.add_argument("--dbp03-password", dest="dbp03_password", default="", help="Password for DBP03.")
        parser.add_argument(
            "--generate",
            action="store_true",
            help="Generate secure random passwords for missing values.",
        )

    def _password_value(self, provided: str, generate: bool) -> str:
        value = (provided or "").strip()
        if value:
            return value
        if generate:
            return secrets.token_urlsafe(9)
        raise ValueError("Password is required (provide value or use --generate).")

    @transaction.atomic
    def handle(self, *args, **options):
        dbp02_password = self._password_value(options.get("dbp02_password"), bool(options.get("generate")))
        dbp03_password = self._password_value(options.get("dbp03_password"), bool(options.get("generate")))

        exit18_branch, _ = Branch.objects.get_or_create(
            name="مخرج 18",
            defaults={"code": REQUIRED_BRANCH_CODES["مخرج 18"]},
        )
        jamiah_branch, _ = Branch.objects.get_or_create(
            name="شارع الجمعية",
            defaults={"code": REQUIRED_BRANCH_CODES["شارع الجمعية"]},
        )

        users_spec = [
            ("DBP02", dbp02_password, exit18_branch, "EMP-DBP02"),
            ("DBP03", dbp03_password, jamiah_branch, "EMP-DBP03"),
        ]

        for username, password, branch, employee_id in users_spec:
            user, created = User.objects.get_or_create(username=username)
            user.is_superuser = False
            user.is_staff = True
            user.is_active = True
            user.set_password(password)
            user.save()

            UserProfile.objects.update_or_create(
                user=user,
                defaults={
                    "role": UserProfile.Roles.CASHIER,
                    "branch": branch,
                    "employee_id": employee_id,
                },
            )
            mode = "created" if created else "updated"
            self.stdout.write(f"{username}: {mode}, role=cashier, branch={branch.name}")

        self.stdout.write(self.style.SUCCESS("Passwords set successfully."))
        self.stdout.write("DBP02 password: " + dbp02_password)
        self.stdout.write("DBP03 password: " + dbp03_password)

from getpass import getpass

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError

from inventory.models import Branch, UserProfile


class Command(BaseCommand):
    help = "Create or update the three global DELTA admin accounts and assign branch responsibility."

    ADMIN_SPECS = [
        {
            "username": "saleh",
            "full_name": "صالح الجابري",
            "employee_id": "ADM-SALEH",
            "branch_name": "شارع الجمعية",
            "branch_code": "ASSN",
        },
        {
            "username": "osama",
            "full_name": "أسامة الجابري",
            "employee_id": "ADM-OSAMA",
            "branch_name": "مخرج 18",
            "branch_code": "EX18",
        },
        {
            "username": "abdulaziz",
            "full_name": "عبدالعزيز الجابري",
            "employee_id": "ADM-AZIZ",
            "branch_name": "الصناعية القديمة",
            "branch_code": "OLDIND",
        },
    ]

    def _prompt_password(self, username: str) -> str:
        self.stdout.write(f"Set password for {username}:")
        while True:
            first = getpass("  Password: ")
            second = getpass("  Confirm:  ")
            if not first:
                self.stderr.write("  Password cannot be empty.")
                continue
            if first != second:
                self.stderr.write("  Passwords do not match. Try again.")
                continue
            return first

    def handle(self, *args, **options):
        User = get_user_model()

        for spec in self.ADMIN_SPECS:
            branch, _ = Branch.objects.get_or_create(
                name=spec["branch_name"],
                defaults={"code": spec["branch_code"]},
            )
            if branch.code != spec["branch_code"]:
                code_in_use = Branch.objects.filter(code=spec["branch_code"]).exclude(pk=branch.pk).exists()
                if not code_in_use:
                    branch.code = spec["branch_code"]
                    branch.save(update_fields=["code"])

            password = self._prompt_password(spec["username"])

            user, _ = User.objects.get_or_create(
                username=spec["username"],
                defaults={
                    "first_name": spec["full_name"],
                    "is_staff": True,
                    "is_superuser": True,
                    "is_active": True,
                },
            )

            user.first_name = spec["full_name"]
            user.last_name = ""
            user.is_staff = True
            user.is_superuser = True
            user.is_active = True
            user.set_password(password)
            user.save()

            conflict = (
                UserProfile.objects.exclude(user=user)
                .filter(employee_id=spec["employee_id"])
                .select_related("user")
                .first()
            )
            if conflict:
                raise CommandError(
                    f"Employee ID '{spec['employee_id']}' is already used by '{conflict.user.username}'. "
                    "Resolve conflict before running command."
                )

            UserProfile.objects.update_or_create(
                user=user,
                defaults={
                    "role": UserProfile.Roles.ADMIN,
                    "branch": branch,
                    "employee_id": spec["employee_id"],
                },
            )

            self.stdout.write(
                self.style.SUCCESS(
                    f"Configured admin '{spec['username']}' ({spec['full_name']}) for '{branch.name}'."
                )
            )

        self.stdout.write(self.style.SUCCESS("Global admin provisioning completed."))

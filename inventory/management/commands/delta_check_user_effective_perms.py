from django.contrib.auth.models import User
from django.core.management.base import BaseCommand, CommandError


class Command(BaseCommand):
    help = "Print effective permissions for a given username (direct + group + flags)."

    def add_arguments(self, parser):
        parser.add_argument("username", type=str, help="Username to inspect.")

    def handle(self, *args, **options):
        username = (options.get("username") or "").strip()
        if not username:
            raise CommandError("Username is required.")
        user = User.objects.filter(username=username).first()
        if not user:
            raise CommandError(f"User '{username}' not found.")

        perms = sorted(user.get_all_permissions())
        group_names = list(user.groups.values_list("name", flat=True))
        self.stdout.write(f"User: {user.username}")
        self.stdout.write(f"is_active={user.is_active} is_staff={user.is_staff} is_superuser={user.is_superuser}")
        self.stdout.write("Groups: " + (", ".join(group_names) if group_names else "-"))
        self.stdout.write(f"Effective permissions ({len(perms)}):")
        for perm in perms:
            self.stdout.write(f" - {perm}")

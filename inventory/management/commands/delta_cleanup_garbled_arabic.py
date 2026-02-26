import re

from django.core.management import call_command
from django.core.management.base import BaseCommand
from django.db import transaction

from inventory.models import Branch, Location


GARBAGE_QMARK_RE = re.compile(r"\?{3,}")


def _is_garbled(value: str) -> bool:
    text = (value or "").strip()
    if not text:
        return False
    return (
        bool(GARBAGE_QMARK_RE.search(text))
        or "\ufffd" in text
        or text.startswith("????")
        or "???" in text
    )


def _safe(value: str) -> str:
    return (value or "").encode("unicode_escape").decode("ascii")


class Command(BaseCommand):
    help = (
        "Detect and cleanup obvious garbled Arabic records. "
        "Dry-run by default. Use --apply to execute."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--apply",
            action="store_true",
            help="Apply changes. Without this flag command runs in dry-run mode.",
        )

    def handle(self, *args, **options):
        apply_changes = bool(options.get("apply"))
        dry_run = not apply_changes

        self.stdout.write("Arabic garbage cleanup started.")
        self.stdout.write("Backup note: take a DB backup before running with --apply.")
        self.stdout.write(f"Mode: {'DRY-RUN' if dry_run else 'APPLY'}")

        garbled_branches = [
            branch for branch in Branch.objects.all().order_by("id") if _is_garbled(branch.name)
        ]
        garbled_locations = [
            location
            for location in Location.objects.select_related("branch").all().order_by("id")
            if _is_garbled(location.name_ar) or _is_garbled(location.name_en)
        ]

        for branch in garbled_branches:
            self.stdout.write(
                f"[PLAN] Garbled branch #{branch.id}: name='{_safe(branch.name)}' code='{_safe(branch.code)}'"
            )

        for location in garbled_locations:
            self.stdout.write(
                f"[PLAN] Garbled location #{location.id}: branch='{_safe(location.branch.name)}' "
                f"code='{_safe(location.code)}' ar='{_safe(location.name_ar)}' en='{_safe(location.name_en)}'"
            )

        if dry_run:
            self.stdout.write(
                self.style.SUCCESS(
                    f"Dry-run complete. branches={len(garbled_branches)}, locations={len(garbled_locations)}"
                )
            )
            return

        with transaction.atomic():
            garbled_location_ids = [location.id for location in garbled_locations]
            if garbled_location_ids:
                self.stdout.write(f"[APPLY] Deleting garbled locations: {garbled_location_ids}")
                Location.objects.filter(id__in=garbled_location_ids).delete()

        # Deterministically merge/delete invalid branches and keep only canonical 3.
        call_command("delta_cleanup_branches", apply=True, stdout=self.stdout)

        # Final pass: remove any branch names still matching garbage pattern.
        leftovers = [branch for branch in Branch.objects.all() if _is_garbled(branch.name)]
        if leftovers:
            for branch in leftovers:
                self.stdout.write(self.style.WARNING(f"[DELETE] leftover garbled branch #{branch.id} '{_safe(branch.name)}'"))
            Branch.objects.filter(id__in=[branch.id for branch in leftovers]).delete()

        self.stdout.write(
            self.style.SUCCESS(
                "Arabic garbage cleanup applied successfully."
            )
        )

import re

from django.core.management.base import BaseCommand
from django.db import transaction

from inventory.models import Branch, Location


CANONICAL_BRANCH_NAMES = {
    "الصناعية القديمة",
    "مخرج 18",
    "شارع الجمعية",
}

# Safe mappings only when branch code is known.
BRANCH_NAME_BY_CODE = {
    "DBP-01": "الصناعية القديمة",
    "DBP-02": "مخرج 18",
    "DBP-03": "شارع الجمعية",
    "OLD": "الصناعية القديمة",
    "EXIT18": "مخرج 18",
    "JAM": "شارع الجمعية",
}


def _normalize_spaces(value: str) -> str:
    return " ".join((value or "").split())


def _safe_console(value: str) -> str:
    return (value or "").encode("unicode_escape").decode("ascii")


def _target_branch_name(branch: Branch) -> str | None:
    current = _normalize_spaces(branch.name)
    if current in CANONICAL_BRANCH_NAMES:
        return None

    expected = BRANCH_NAME_BY_CODE.get((branch.code or "").strip().upper())
    if expected:
        return expected

    # Common harmless aliases where mapping is still clear.
    aliases = {
        "الجمعية": "شارع الجمعية",
        "فرع الجمعية": "شارع الجمعية",
        "مخرج18": "مخرج 18",
        "الصناعية": "الصناعية القديمة",
    }
    return aliases.get(current)


def _target_location_name(location: Location) -> str | None:
    code = (location.code or "").strip().upper()
    current = _normalize_spaces(location.name_ar)

    if code == "UNASSIGNED":
        return None if current == "غير محدد" else "غير محدد"

    # Fix rows that were already damaged into question marks like "?? 3".
    qmark_match = re.fullmatch(r"\?+\s*(\d+)", current)
    if qmark_match:
        return f"رف {qmark_match.group(1)}"

    if current:
        return None

    # If Arabic name is empty and location code ends in digits, provide a shelf label.
    digit_match = re.search(r"(\d+)$", code)
    if digit_match:
        return f"رف {digit_match.group(1)}"
    return None


class Command(BaseCommand):
    help = "Repairs common corrupted Arabic branch/location labels. Dry-run by default."

    def add_arguments(self, parser):
        parser.add_argument(
            "--apply",
            action="store_true",
            help="Apply changes. Without this flag the command runs in dry-run mode.",
        )

    def handle(self, *args, **options):
        apply_changes = bool(options.get("apply"))
        dry_run = not apply_changes

        self.stdout.write("Arabic text repair started.")
        self.stdout.write("Backup note: take a DB backup before running with --apply.")
        self.stdout.write(f"Mode: {'DRY-RUN' if dry_run else 'APPLY'}")

        branch_updates: list[tuple[Branch, str]] = []
        unresolved_branches: list[Branch] = []
        for branch in Branch.objects.all().order_by("id"):
            target = _target_branch_name(branch)
            if target and target != _normalize_spaces(branch.name):
                branch_updates.append((branch, target))
            elif "?" in (branch.name or ""):
                unresolved_branches.append(branch)

        location_updates: list[tuple[Location, str]] = []
        for location in Location.objects.select_related("branch").all().order_by("id"):
            target = _target_location_name(location)
            if target and target != _normalize_spaces(location.name_ar):
                location_updates.append((location, target))

        for branch, target in branch_updates:
            self.stdout.write(
                f"[PLAN] Branch #{branch.id} '{_safe_console(branch.name)}' -> '{_safe_console(target)}'"
            )
        for location, target in location_updates:
            self.stdout.write(
                f"[PLAN] Location #{location.id} ({location.branch.code}/{location.code}) "
                f"'{_safe_console(location.name_ar)}' -> '{_safe_console(target)}'"
            )
        for branch in unresolved_branches:
            self.stdout.write(
                self.style.WARNING(
                    f"[SKIP] Branch #{branch.id} has unclear corrupted name '{_safe_console(branch.name)}' "
                    f"(code={branch.code}); not modified."
                )
            )

        if dry_run:
            self.stdout.write(
                self.style.SUCCESS(
                    f"Dry-run complete. Planned updates: branches={len(branch_updates)}, "
                    f"locations={len(location_updates)}, unresolved={len(unresolved_branches)}."
                )
            )
            return

        with transaction.atomic():
            for branch, target in branch_updates:
                branch.name = target
                branch.save(update_fields=["name"])
            for location, target in location_updates:
                location.name_ar = target
                location.save(update_fields=["name_ar"])

        self.stdout.write(
            self.style.SUCCESS(
                f"Applied updates: branches={len(branch_updates)}, "
                f"locations={len(location_updates)}. "
                f"Unresolved branches={len(unresolved_branches)}."
            )
        )

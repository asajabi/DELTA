from django.core.management.base import BaseCommand
from django.db import transaction

from inventory.management.commands.enforce_branches import (
    CANONICAL_BRANCHES,
    Command as EnforceBranchesCommand,
    _detect_branch_key,
)
from inventory.models import Branch


CANONICAL_KEYS = ("old", "exit18", "jamiah")
DEFAULT_UNKNOWN_KEY = "old"


def _norm(value: str) -> str:
    return " ".join((value or "").split()).casefold()


def _safe(value: str) -> str:
    return (value or "").encode("unicode_escape").decode("ascii")


def _best_branch_key(branch: Branch) -> str:
    direct = _detect_branch_key(branch)
    if direct:
        return direct

    text = _norm(branch.name)
    if "18" in text or "exit" in text:
        return "exit18"
    if "\u062c\u0645\u0639" in text or "jam" in text:
        return "jamiah"
    if "\u0635\u0646\u0627\u0639" in text or "old" in text:
        return "old"
    return DEFAULT_UNKNOWN_KEY


class Command(BaseCommand):
    help = (
        "Ensure Branch table contains ONLY canonical 3 branches. "
        "Unknown/garbled branches are merged into the default branch."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--apply",
            action="store_true",
            help="Apply changes. Without this flag command runs in dry-run mode.",
        )

    def _pick_primary(self, branches: list[Branch], key: str) -> Branch:
        canonical_name = _norm(CANONICAL_BRANCHES[key]["name"])
        canonical_code = CANONICAL_BRANCHES[key]["code"].upper()
        for branch in branches:
            if _norm(branch.name) == canonical_name:
                return branch
        for branch in branches:
            if (branch.code or "").strip().upper() == canonical_code:
                return branch
        return sorted(branches, key=lambda row: row.id)[0]

    def handle(self, *args, **options):
        apply_changes = bool(options.get("apply"))
        dry_run = not apply_changes

        self.stdout.write("DELTA branch cleanup started.")
        self.stdout.write("Backup note: take a DB backup before running with --apply.")
        self.stdout.write(f"Mode: {'DRY-RUN' if dry_run else 'APPLY'}")

        all_branches = list(Branch.objects.all().order_by("id"))
        buckets: dict[str, list[Branch]] = {key: [] for key in CANONICAL_KEYS}
        for branch in all_branches:
            buckets[_best_branch_key(branch)].append(branch)

        target_by_key: dict[str, Branch | None] = {}
        for key in CANONICAL_KEYS:
            candidates = buckets.get(key, [])
            target_by_key[key] = self._pick_primary(candidates, key) if candidates else None

        for key in CANONICAL_KEYS:
            target = target_by_key[key]
            canonical_name = CANONICAL_BRANCHES[key]["name"]
            if target is None:
                self.stdout.write(f"[PLAN] Create canonical branch '{_safe(canonical_name)}'.")
            elif _norm(target.name) != _norm(canonical_name):
                self.stdout.write(
                    f"[PLAN] Rename branch #{target.id} '{_safe(target.name)}' -> '{_safe(canonical_name)}'."
                )

        for key, branches in buckets.items():
            target = target_by_key.get(key)
            for branch in branches:
                if target and branch.id == target.id:
                    continue
                target_name = CANONICAL_BRANCHES[key]["name"]
                self.stdout.write(
                    f"[PLAN] Merge branch #{branch.id} '{_safe(branch.name)}' -> '{_safe(target_name)}'."
                )

        if dry_run:
            self.stdout.write(
                self.style.SUCCESS(
                    "Dry-run complete. Unknown/garbled branches will be merged to default branch."
                )
            )
            return

        helper = EnforceBranchesCommand()
        with transaction.atomic():
            for key in CANONICAL_KEYS:
                if target_by_key[key] is None:
                    spec = CANONICAL_BRANCHES[key]
                    target_by_key[key] = Branch.objects.create(name=spec["name"], code=spec["code"])

            for key in CANONICAL_KEYS:
                target = target_by_key[key]
                spec = CANONICAL_BRANCHES[key]
                if target is None:
                    continue
                updates = []
                if target.name != spec["name"]:
                    target.name = spec["name"]
                    updates.append("name")
                canonical_code = spec["code"].upper()
                if (target.code or "").strip().upper() != canonical_code:
                    target.code = canonical_code
                    updates.append("code")
                if updates:
                    target.save(update_fields=updates)

            for key in CANONICAL_KEYS:
                target = target_by_key[key]
                if target is None:
                    continue
                for branch in list(Branch.objects.all().order_by("id")):
                    if branch.id == target.id:
                        continue
                    if _best_branch_key(branch) == key:
                        helper._apply_branch_merge(branch, target)

            canonical_ids = {target_by_key[key].id for key in CANONICAL_KEYS if target_by_key[key]}
            leftovers = Branch.objects.exclude(id__in=canonical_ids).order_by("id")
            default_target = target_by_key[DEFAULT_UNKNOWN_KEY]
            if default_target is not None:
                for branch in leftovers:
                    if branch.id != default_target.id:
                        helper._apply_branch_merge(branch, default_target)

            for key in CANONICAL_KEYS:
                target = Branch.objects.filter(id=target_by_key[key].id).first() if target_by_key[key] else None
                if target:
                    target.name = CANONICAL_BRANCHES[key]["name"]
                    target.code = CANONICAL_BRANCHES[key]["code"]
                    target.save(update_fields=["name", "code"])

        remaining = list(Branch.objects.order_by("name").values_list("name", flat=True))
        self.stdout.write(self.style.SUCCESS(f"Cleanup complete. Remaining branches: {remaining}"))

from collections import defaultdict

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from inventory.models import (
    AuditLog,
    Branch,
    Location,
    Order,
    Sale,
    Stock,
    StockLocation,
    StockMovement,
    TransferRequest,
    UserProfile,
    sync_stock_total_from_locations,
)


CANONICAL_BRANCHES = {
    "old": {
        "name": "الصناعية القديمة",
        "code": "OLDIND",
        "name_aliases": {"الصناعية القديمة", "فرع الصناعية القديمة", "الصناعية", "old industrial"},
        "code_aliases": {"OLDIND", "DBP-01", "OLD", "OLDX", "OLDDBG", "OLDDBG2", "OLDDBG3", "OLDDBG4"},
    },
    "exit18": {
        "name": "مخرج 18",
        "code": "EX18",
        "name_aliases": {"مخرج 18", "فرع مخرج 18", "مخرج18", "الخرج", "exit 18", "exit18"},
        "code_aliases": {"EX18", "EXIT18", "DBP-02", "M18"},
    },
    "jamiah": {
        "name": "شارع الجمعية",
        "code": "ASSN",
        "name_aliases": {"شارع الجمعية", "فرع الجمعية", "الجمعية", "jamiah", "al jamiah"},
        "code_aliases": {"ASSN", "DBP-03", "JAM", "JAMIAH"},
    },
}


def _norm(value: str) -> str:
    return " ".join((value or "").split()).casefold()


def _safe(value: str) -> str:
    return (value or "").encode("unicode_escape").decode("ascii")


def _detect_branch_key(branch: Branch) -> str | None:
    name = _norm(branch.name)
    code = (branch.code or "").strip().upper()
    for key, spec in CANONICAL_BRANCHES.items():
        if name == _norm(spec["name"]):
            return key
        if name in {_norm(alias) for alias in spec["name_aliases"]}:
            return key
        if code in spec["code_aliases"]:
            return key
    return None


class Command(BaseCommand):
    help = "Enforce exactly three business branches with safe dry-run-by-default reassignment."

    def add_arguments(self, parser):
        parser.add_argument(
            "--apply",
            action="store_true",
            help="Apply changes. Without this flag command runs in dry-run mode.",
        )
        parser.add_argument(
            "--map",
            action="append",
            default=[],
            help=(
                "Explicit branch-code mapping for ambiguous rows. Format: CODE=TARGET, "
                "where TARGET is one of: old, exit18, jamiah."
            ),
        )

    def _reference_count(self, branch: Branch) -> int:
        return (
            UserProfile.objects.filter(branch=branch).count()
            + Location.objects.filter(branch=branch).count()
            + Stock.objects.filter(branch=branch).count()
            + StockLocation.objects.filter(branch=branch).count()
            + StockMovement.objects.filter(branch=branch).count()
            + Order.objects.filter(branch=branch).count()
            + Sale.objects.filter(branch=branch).count()
            + TransferRequest.objects.filter(source_branch=branch).count()
            + TransferRequest.objects.filter(destination_branch=branch).count()
            + AuditLog.objects.filter(branch=branch).count()
        )

    def _choose_primary(self, branches: list[Branch], key: str) -> Branch:
        canonical_name = _norm(CANONICAL_BRANCHES[key]["name"])
        preferred_code = CANONICAL_BRANCHES[key]["code"]

        for branch in branches:
            if _norm(branch.name) == canonical_name:
                return branch
        for branch in branches:
            if (branch.code or "").strip().upper() == preferred_code:
                return branch
        return sorted(branches, key=lambda row: row.id)[0]

    def _apply_branch_merge(self, source: Branch, target: Branch):
        # Reassign straightforward FK references first.
        UserProfile.objects.filter(branch=source).update(branch=target)
        Order.objects.filter(branch=source).update(branch=target)
        Sale.objects.filter(branch=source).update(branch=target)
        AuditLog.objects.filter(branch=source).update(branch=target)
        StockMovement.objects.filter(branch=source).update(branch=target)
        TransferRequest.objects.filter(source_branch=source).update(source_branch=target)
        TransferRequest.objects.filter(destination_branch=source).update(destination_branch=target)

        # Move/merge locations by code.
        location_map: dict[int, int] = {}
        duplicate_location_ids: list[int] = []
        for location in Location.objects.filter(branch=source).order_by("id"):
            existing = Location.objects.filter(branch=target, code=location.code).first()
            if existing:
                location_map[location.id] = existing.id
                duplicate_location_ids.append(location.id)
                updates = {}
                if not existing.name_ar and location.name_ar:
                    updates["name_ar"] = location.name_ar
                if not existing.name_en and location.name_en:
                    updates["name_en"] = location.name_en
                if updates:
                    Location.objects.filter(id=existing.id).update(**updates)
            else:
                Location.objects.filter(id=location.id).update(branch=target)
                location_map[location.id] = location.id

        # Reassign/merge stock locations.
        sync_pairs: set[tuple[int, int]] = set()
        for row in StockLocation.objects.filter(branch=source).order_by("id"):
            target_location_id = location_map.get(row.location_id, row.location_id)
            merged = (
                StockLocation.objects.filter(
                    part_id=row.part_id,
                    branch=target,
                    location_id=target_location_id,
                )
                .exclude(id=row.id)
                .first()
            )
            if merged:
                merged.quantity += row.quantity
                merged.save(update_fields=["quantity"])
                row.delete()
            else:
                StockLocation.objects.filter(id=row.id).update(branch=target, location_id=target_location_id)
            sync_pairs.add((row.part_id, target.id))

        # Update movement location references for merged duplicate locations.
        for old_location_id, new_location_id in location_map.items():
            if old_location_id == new_location_id:
                continue
            StockMovement.objects.filter(from_location_id=old_location_id).update(from_location_id=new_location_id)
            StockMovement.objects.filter(to_location_id=old_location_id).update(to_location_id=new_location_id)

        if duplicate_location_ids:
            Location.objects.filter(id__in=duplicate_location_ids).delete()

        # Reassign/merge stock rows.
        for stock in Stock.objects.filter(branch=source).order_by("id"):
            target_stock = Stock.objects.filter(part=stock.part, branch=target).exclude(id=stock.id).first()
            if target_stock:
                target_stock.quantity += stock.quantity
                target_stock.min_stock_level = max(target_stock.min_stock_level, stock.min_stock_level)
                if not target_stock.location_in_warehouse and stock.location_in_warehouse:
                    target_stock.location_in_warehouse = stock.location_in_warehouse
                target_stock.save(update_fields=["quantity", "min_stock_level", "location_in_warehouse"])
                stock.delete()
            else:
                Stock.objects.filter(id=stock.id).update(branch=target)
            sync_pairs.add((stock.part_id, target.id))

        # Sync branch stock totals from location rows.
        for part_id, branch_id in sync_pairs:
            sync_stock_total_from_locations(part_id=part_id, branch_id=branch_id)

        source.delete()

    def handle(self, *args, **options):
        apply_changes = bool(options.get("apply"))
        dry_run = not apply_changes

        self.stdout.write("Branch enforcement started.")
        self.stdout.write("Backup note: take a DB backup before running with --apply.")
        self.stdout.write(f"Mode: {'DRY-RUN' if dry_run else 'APPLY'}")

        override_map: dict[str, str] = {}
        for raw in options.get("map") or []:
            token = (raw or "").strip()
            if "=" not in token:
                raise CommandError(f"Invalid --map '{token}'. Expected CODE=TARGET.")
            code, target = token.split("=", 1)
            code = (code or "").strip().upper()
            target = (target or "").strip().lower()
            if target not in CANONICAL_BRANCHES:
                raise CommandError(f"Invalid TARGET '{target}' in --map {token}.")
            if not code:
                raise CommandError(f"Invalid CODE in --map {token}.")
            override_map[code] = target

        def resolve_key(branch: Branch) -> str | None:
            code = (branch.code or "").strip().upper()
            if code in override_map:
                return override_map[code]
            return _detect_branch_key(branch)

        all_branches = list(Branch.objects.all().order_by("id"))
        buckets: dict[str, list[Branch]] = defaultdict(list)
        unresolved: list[Branch] = []
        for branch in all_branches:
            key = resolve_key(branch)
            if key is None:
                unresolved.append(branch)
            else:
                buckets[key].append(branch)

        target_by_key: dict[str, Branch | None] = {}
        create_plans: list[str] = []
        rename_plans: list[str] = []

        for key, spec in CANONICAL_BRANCHES.items():
            candidates = buckets.get(key, [])
            if candidates:
                primary = self._choose_primary(candidates, key)
                target_by_key[key] = primary
                if primary.name != spec["name"]:
                    rename_plans.append(f"Branch #{primary.id}: '{_safe(primary.name)}' -> '{_safe(spec['name'])}'")
            else:
                target_by_key[key] = None
                create_plans.append(f"Create branch '{_safe(spec['name'])}' ({spec['code']})")

        for plan in create_plans:
            self.stdout.write(f"[PLAN] {plan}")
        for plan in rename_plans:
            self.stdout.write(f"[PLAN] {plan}")

        for branch in unresolved:
            self.stdout.write(
                self.style.WARNING(
                    f"[UNRESOLVED] Branch #{branch.id} '{_safe(branch.name)}' (code={branch.code}) "
                    "could not be mapped safely."
                )
            )

        # Pre-check transfers that would collapse source/destination into same canonical branch.
        unsafe_transfers: list[int] = []
        for transfer in TransferRequest.objects.select_related("source_branch", "destination_branch"):
            source_key = resolve_key(transfer.source_branch)
            destination_key = resolve_key(transfer.destination_branch)
            if source_key and destination_key and source_key == destination_key:
                unsafe_transfers.append(transfer.id)
        if unsafe_transfers:
            self.stdout.write(
                self.style.WARNING(
                    f"[UNSAFE] Transfer IDs would collapse source/destination after merge: {unsafe_transfers}"
                )
            )

        unresolved_with_refs = [branch for branch in unresolved if self._reference_count(branch) > 0]
        if unresolved_with_refs:
            self.stdout.write(
                self.style.WARNING(
                    f"[STOP] {len(unresolved_with_refs)} unresolved branch(es) still have linked records."
                )
            )
        if dry_run:
            self.stdout.write(
                self.style.SUCCESS(
                    "Dry-run complete. No changes applied. "
                    f"mapped_groups={sum(1 for key in target_by_key if target_by_key[key] is not None)}, "
                    f"planned_creates={len(create_plans)}, unresolved={len(unresolved)}, "
                    f"unsafe_transfers={len(unsafe_transfers)}."
                )
            )
            return

        if unresolved_with_refs or unsafe_transfers:
            raise CommandError("Cannot apply safely: unresolved branches or unsafe transfers detected.")

        with transaction.atomic():
            # Create missing canonical branches.
            for key, branch in list(target_by_key.items()):
                if branch is not None:
                    continue
                spec = CANONICAL_BRANCHES[key]
                target_by_key[key] = Branch.objects.create(name=spec["name"], code=spec["code"])

            # Normalize primary canonical names.
            for key, branch in target_by_key.items():
                if branch is None:
                    continue
                canonical_name = CANONICAL_BRANCHES[key]["name"]
                if branch.name != canonical_name:
                    branch.name = canonical_name
                    branch.save(update_fields=["name"])

            # Merge all non-primary mapped branches into their canonical target.
            for key, branches in buckets.items():
                target = target_by_key[key]
                if target is None:
                    continue
                for branch in branches:
                    if branch.id == target.id:
                        continue
                    self.stdout.write(
                        f"[APPLY] Reassign branch #{branch.id} '{_safe(branch.name)}' -> "
                        f"#{target.id} '{_safe(target.name)}'"
                    )
                    self._apply_branch_merge(branch, target)

            # Remove unresolved branches only if they are empty.
            for branch in unresolved:
                if self._reference_count(branch) == 0:
                    self.stdout.write(f"[APPLY] Deleting empty unresolved branch #{branch.id} '{_safe(branch.name)}'")
                    branch.delete()

        remaining = list(Branch.objects.order_by("id").values_list("name", "code"))
        self.stdout.write(self.style.SUCCESS(f"Branch enforcement completed. Remaining branches: {remaining}"))

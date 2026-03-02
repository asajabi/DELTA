from django.core.management.base import BaseCommand, CommandError
from django.db import OperationalError, transaction
from django.db.models import Sum
from django.db.models.functions import Coalesce

from inventory.models import (
    Stock,
    StockLocation,
    ensure_stock_locations_seeded_from_branch_stock,
    sync_stock_total_from_locations,
)


class Command(BaseCommand):
    help = (
        "Synchronize branch stock totals from shelf/location rows (StockLocation). "
        "Dry-run by default; use --apply to write changes."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--apply",
            action="store_true",
            help="Apply changes. Without this flag, command runs in dry-run mode.",
        )
        parser.add_argument(
            "--no-seed",
            action="store_true",
            help="Do not seed missing default location rows from stock totals.",
        )
        parser.add_argument(
            "--part-id",
            type=int,
            default=None,
            help="Limit sync to one part id.",
        )
        parser.add_argument(
            "--branch-id",
            type=int,
            default=None,
            help="Limit sync to one branch id.",
        )

    def _location_total(self, *, part_id: int, branch_id: int) -> int:
        return int(
            StockLocation.objects.filter(part_id=part_id, branch_id=branch_id).aggregate(
                total=Coalesce(Sum("quantity"), 0)
            )["total"]
            or 0
        )

    def _stock_qty(self, *, part_id: int, branch_id: int) -> int:
        return int(
            Stock.objects.filter(part_id=part_id, branch_id=branch_id).values_list("quantity", flat=True).first()
            or 0
        )

    def handle(self, *args, **options):
        apply_changes = bool(options["apply"])
        seed_missing = not bool(options["no_seed"])
        part_id = options["part_id"]
        branch_id = options["branch_id"]

        self.stdout.write("Stock totals sync started.")
        self.stdout.write(f"Mode: {'APPLY' if apply_changes else 'DRY-RUN'}")
        self.stdout.write(f"Seed missing locations: {'YES' if seed_missing else 'NO'}")

        stock_qs = Stock.objects.select_related("part", "branch").order_by("branch_id", "part_id")
        location_qs = StockLocation.objects.order_by("branch_id", "part_id")
        if part_id:
            stock_qs = stock_qs.filter(part_id=part_id)
            location_qs = location_qs.filter(part_id=part_id)
        if branch_id:
            stock_qs = stock_qs.filter(branch_id=branch_id)
            location_qs = location_qs.filter(branch_id=branch_id)

        stock_rows = list(stock_qs)
        location_pairs = set(location_qs.values_list("part_id", "branch_id"))

        seeded_count = 0
        changed_count = 0
        unchanged_count = 0
        inspected_pairs = set(location_pairs)

        def seed_missing_rows():
            nonlocal seeded_count
            if not seed_missing:
                return
            for stock in stock_rows:
                if int(stock.quantity or 0) <= 0:
                    continue
                has_rows = StockLocation.objects.filter(part_id=stock.part_id, branch_id=stock.branch_id).exists()
                if has_rows:
                    continue
                if apply_changes:
                    ensure_stock_locations_seeded_from_branch_stock(stock)
                seeded_count += 1
                inspected_pairs.add((stock.part_id, stock.branch_id))

        def sync_pairs():
            nonlocal changed_count, unchanged_count
            if not inspected_pairs:
                return
            for current_part_id, current_branch_id in sorted(inspected_pairs):
                before_qty = self._stock_qty(part_id=current_part_id, branch_id=current_branch_id)
                location_qty = self._location_total(part_id=current_part_id, branch_id=current_branch_id)

                if apply_changes:
                    after_qty = sync_stock_total_from_locations(part_id=current_part_id, branch_id=current_branch_id)
                else:
                    after_qty = location_qty

                if int(before_qty) != int(after_qty):
                    changed_count += 1
                    self.stdout.write(
                        f"[SYNC] part={current_part_id} branch={current_branch_id} {before_qty} -> {after_qty}"
                    )
                else:
                    unchanged_count += 1

        try:
            if apply_changes:
                with transaction.atomic():
                    seed_missing_rows()
                    # Rebuild pair list after seeding.
                    if seed_missing and seeded_count > 0:
                        refreshed_pairs = location_qs.values_list("part_id", "branch_id")
                        inspected_pairs.update(refreshed_pairs)
                    sync_pairs()
            else:
                seed_missing_rows()
                sync_pairs()
        except OperationalError as exc:
            raise CommandError(
                "Database write failed. If using SQLite, stop runserver and ensure db.sqlite3 is writable, then retry."
            ) from exc

        self.stdout.write(
            self.style.SUCCESS(
                "Stock totals sync finished. "
                f"seeded={seeded_count}, changed={changed_count}, unchanged={unchanged_count}, pairs={len(inspected_pairs)}."
            )
        )

from dataclasses import dataclass

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from inventory.models import (
    Branch,
    Category,
    Part,
    REQUIRED_BRANCH_CODES,
    Stock,
)


@dataclass(frozen=True)
class CategorySpec:
    code: str
    name: str
    items: list[str]


CATEGORY_SPECS = [
    CategorySpec("EN", "Engine / محرك", ["Piston Kit", "Engine Mount", "Timing Chain Kit", "Valve Cover Gasket", "Oil Pan Gasket", "Camshaft Sensor", "Crankshaft Sensor", "PCV Valve", "Head Gasket Set", "Engine Bearing Set"]),
    CategorySpec("OF", "Oil & Fluids / زيوت وسوائل", ["Engine Oil 5W30 1L", "Engine Oil 10W40 4L", "ATF Fluid 1L", "Brake Fluid DOT4", "Coolant Red 1L", "Power Steering Fluid", "Gear Oil 75W90", "Hydraulic Oil", "Diesel Additive", "Engine Flush"]),
    CategorySpec("FL", "Filters / فلاتر", ["Oil Filter", "Air Filter", "Cabin Filter", "Fuel Filter", "Transmission Filter", "Performance Air Filter", "Heavy Duty Fuel Filter", "Diesel Water Separator", "AC Dryer Filter", "Intake Filter"]),
    CategorySpec("BR", "Brakes / فرامل", ["Front Brake Pads", "Rear Brake Pads", "Front Brake Disc", "Rear Brake Disc", "Brake Shoe Set", "Brake Master Cylinder", "Brake Caliper Front", "Brake Caliper Rear", "ABS Sensor", "Brake Booster"]),
    CategorySpec("SU", "Suspension / تعليق", ["Front Shock Absorber", "Rear Shock Absorber", "Control Arm Left", "Control Arm Right", "Ball Joint", "Stabilizer Link", "Strut Mount", "Tie Rod End", "Steering Rack Boot", "Wheel Bearing"]),
    CategorySpec("EL", "Electrical / كهرباء", ["Battery 70Ah", "Alternator", "Starter Motor", "Headlight Bulb H7", "Tail Light Bulb", "Fuse 15A", "Horn", "Window Switch", "Ignition Switch", "Relay 12V"]),
    CategorySpec("BT", "Belts / سيور", ["Timing Belt", "Serpentine Belt", "AC Belt", "Power Steering Belt", "Alternator Belt", "Belt Tensioner", "Idler Pulley", "Timing Belt Tensioner", "Drive Belt Kit", "V-Belt"]),
    CategorySpec("CL", "Cooling / تبريد", ["Radiator", "Radiator Cap", "Thermostat", "Water Pump", "Cooling Fan", "Fan Clutch", "Coolant Hose Upper", "Coolant Hose Lower", "Heater Core", "Expansion Tank"]),
    CategorySpec("IG", "Ignition / إشعال", ["Spark Plug", "Ignition Coil", "Ignition Wire Set", "Distributor Cap", "Rotor Arm", "Knock Sensor", "Glow Plug", "Coil Connector", "Ignition Module", "Cam Ignition Seal"]),
]

CANONICAL_BRANCHES = [
    ("الصناعية القديمة", REQUIRED_BRANCH_CODES["الصناعية القديمة"]),
    ("مخرج 18", REQUIRED_BRANCH_CODES["مخرج 18"]),
    ("شارع الجمعية", REQUIRED_BRANCH_CODES["شارع الجمعية"]),
]


class Command(BaseCommand):
    help = "Seed realistic categories, parts, and branch stock for DELTA POS. Dry-run by default."

    def add_arguments(self, parser):
        parser.add_argument("--apply", action="store_true", help="Apply changes. Default is dry-run.")
        parser.add_argument(
            "--reset",
            action="store_true",
            help="Delete existing Parts/Categories before seeding. Requires --apply.",
        )

    def handle(self, *args, **options):
        apply_changes = bool(options.get("apply"))
        do_reset = bool(options.get("reset"))
        if do_reset and not apply_changes:
            raise CommandError("--reset requires --apply.")

        total_parts = sum(len(spec.items) for spec in CATEGORY_SPECS)
        self.stdout.write(f"Mode: {'APPLY' if apply_changes else 'DRY-RUN'}")
        self.stdout.write(f"Planned categories: {len(CATEGORY_SPECS)}")
        self.stdout.write(f"Planned parts: {total_parts}")
        self.stdout.write("Planned branches: " + ", ".join(name for name, _ in CANONICAL_BRANCHES))

        if not apply_changes:
            self.stdout.write(self.style.SUCCESS("Dry-run complete. No changes applied."))
            return

        with transaction.atomic():
            branches = []
            for name, code in CANONICAL_BRANCHES:
                branch, _ = Branch.objects.get_or_create(name=name, defaults={"code": code})
                if (branch.code or "").strip().upper() != code:
                    branch.code = code
                    branch.save(update_fields=["code"])
                branches.append(branch)

            if do_reset:
                Stock.objects.all().delete()
                Part.objects.all().delete()
                Category.objects.all().delete()

            category_map: dict[str, Category] = {}
            for spec in CATEGORY_SPECS:
                category, _ = Category.objects.get_or_create(name=spec.name)
                category_map[spec.code] = category

            part_index = 0
            for spec in CATEGORY_SPECS:
                category = category_map[spec.code]
                for item in spec.items:
                    part_index += 1
                    part_number = f"{spec.code}-{part_index:04d}"
                    barcode = f"{6281000000000 + part_index:013d}"
                    cost = round(8 + ((part_index * 7) % 90), 2)
                    sell = round(cost * 1.45, 2)
                    part, _ = Part.objects.update_or_create(
                        part_number=part_number,
                        defaults={
                            "name": item,
                            "barcode": barcode,
                            "category": category,
                            "cost_price": cost,
                            "selling_price": sell,
                        },
                    )

                    for branch_idx, branch in enumerate(branches):
                        base_qty = ((part_index * 3) + (branch_idx * 7)) % 45 + 1
                        min_level = (part_index % 6) + 2
                        if (part_index + branch_idx) % 9 == 0:
                            qty = max(min_level - 1, 0)
                        else:
                            qty = base_qty
                        Stock.objects.update_or_create(
                            part=part,
                            branch=branch,
                            defaults={"quantity": qty, "min_stock_level": min_level},
                        )

        self.stdout.write(self.style.SUCCESS(f"Seed complete. Categories={len(CATEGORY_SPECS)}, parts={total_parts}"))

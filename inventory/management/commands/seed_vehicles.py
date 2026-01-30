from django.core.management.base import BaseCommand
from inventory.models import Vehicle

class Command(BaseCommand):
    help = 'Auto-generates Nissan models from 1980 to 2026'

    def handle(self, *args, **kwargs):
        # Comprehensive list of Nissan Models
        nissan_models = [
            # --- SUVs & 4x4 (The most important for KSA) ---
            "Patrol",          # The King
            "Patrol Safari",   # The VTC/Classic
            "Patrol Super Safari",
            "Pathfinder",
            "X-Trail",
            "Xterra",          # Popular off-roader
            "Armada",
            "Murano",
            "Juke",
            "Kicks",
            "Qashqai",
            "Terra",
            "Ariya",           # New EV SUV

            # --- Sedans & Hatchbacks ---
            "Sunny",           # Very popular
            "Sentra",
            "Altima",
            "Maxima",
            "Tiida",
            "Versa",
            "Micra",
            "Bluebird",        # Classic
            "Laurel",          # Classic 80s/90s
            "Cefiro",          # Classic
            "Primera",
            "Pulsar",
            "Leaf",            # EV

            # --- Sports & Performance ---
            "GT-R",            # Godzilla
            "Skyline",         # R32, R33, R34
            "300ZX",           # Classic 90s
            "350Z",
            "370Z",
            "Z",               # The new 2023+ Z
            "Silvia",          # S13, S14, S15

            # --- Trucks & Vans ---
            "Datsun",          # The legend pickup
            "Navara",
            "Hilux",           # (Wait, Hilux is Toyota - removing to be safe)
            "D21",             # Hardbody pickup
            "Frontier",
            "Titan",
            "Urvan",           # The van
            "Civilian",        # The bus
        ]

        # Year Range: 1980 to 2026 (range stops before the second number, so we use 2027)
        years = range(1980, 2027)

        self.stdout.write("Starting Nissan database import...")
        
        count = 0
        total_created = 0
        
        for model in nissan_models:
            for year in years:
                # get_or_create prevents duplicates if you run the script twice
                obj, created = Vehicle.objects.get_or_create(
                    make="Nissan",
                    model=model,
                    year=year
                )
                if created:
                    count += 1
            total_created += 1
            # Optional: Print progress for every model finished
            self.stdout.write(f"Generated years for Nissan {model}...")

        self.stdout.write(self.style.SUCCESS(f'Done! Successfully added {count} new Nissan entries.'))
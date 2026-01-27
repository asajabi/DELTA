from django.db import models

# 1. The Car Database (SMACC doesn't have this)
class Vehicle(models.Model):
    make = models.CharField(max_length=50)        # e.g., "Nissan"
    model = models.CharField(max_length=50)       # e.g., "Patrol"
    year = models.IntegerField()                  # e.g., 2024

    def __str__(self):
        return f"{self.make} {self.model} ({self.year})"

# 2. The Branch (For your 3 locations)
class Branch(models.Model):
    name = models.CharField(max_length=50)        # e.g., "Riyadh Exit 10"
    code = models.CharField(max_length=10, unique=True) # e.g., "RIY-01"

    def __str__(self):
        return self.name

# 3. The Category (Engine, Brakes, Body)
class Category(models.Model):
    name = models.CharField(max_length=100)

    def __str__(self):
        return self.name

# 4. The Product (The Part itself)
class Part(models.Model):
    name = models.CharField(max_length=200)       # e.g., "Front Bumper"
    part_number = models.CharField(max_length=100, unique=True) # The OEM Number
    barcode = models.CharField(max_length=100, blank=True, null=True) # Scanned Code
    
    category = models.ForeignKey(Category, on_delete=models.SET_NULL, null=True)
    description = models.TextField(blank=True)
    image = models.ImageField(upload_to='parts_images/', blank=True, null=True)
    
    # Financials (Hidden from normal staff later)
    cost_price = models.DecimalField(max_digits=10, decimal_places=2) # Buying Price
    selling_price = models.DecimalField(max_digits=10, decimal_places=2) # Selling Price

    # THE KILLER FEATURE: Link One Part to Many Cars
    compatible_vehicles = models.ManyToManyField(Vehicle, blank=True)

    def __str__(self):
        return f"{self.name} ({self.part_number})"

# 5. The Stock (How many in each branch?)
class Stock(models.Model):
    part = models.ForeignKey(Part, on_delete=models.CASCADE)
    branch = models.ForeignKey(Branch, on_delete=models.CASCADE)
    quantity = models.IntegerField(default=0)
    location_in_warehouse = models.CharField(max_length=50, blank=True, help_text="e.g. Shelf A-4")

    class Meta:
        unique_together = ('part', 'branch') # Prevents duplicates
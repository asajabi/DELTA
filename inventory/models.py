import uuid
from django.db import models
from django.contrib.auth.models import User
from django.utils import timezone


# 1. New Customer Model
class Customer(models.Model):
    phone_number = models.CharField(max_length=20, unique=True)
    name = models.CharField(max_length=100)
    car_model = models.CharField(max_length=100, blank=True, help_text="e.g. 2018 Camry")
    
    def __str__(self):
        return f"{self.name} ({self.phone_number})"

# 1. The Car Database
class Vehicle(models.Model):
    make = models.CharField(max_length=50)
    model = models.CharField(max_length=50)
    year = models.IntegerField()

    def __str__(self):
        return f"{self.make} {self.model} ({self.year})"

# 2. The Branch
class Branch(models.Model):
    name = models.CharField(max_length=50)
    code = models.CharField(max_length=10, unique=True)

    def __str__(self):
        return self.name

# 3. The Category
class Category(models.Model):
    name = models.CharField(max_length=100)

    def __str__(self):
        return self.name

# 4. The Product (The Part itself)
class Part(models.Model):
    name = models.CharField(max_length=200)
    part_number = models.CharField(max_length=100, unique=True)
    barcode = models.CharField(max_length=100, blank=True, null=True)
    
    category = models.ForeignKey(Category, on_delete=models.SET_NULL, null=True)
    description = models.TextField(blank=True)
    image = models.ImageField(upload_to='parts_images/', blank=True, null=True)
    
    cost_price = models.DecimalField(max_digits=10, decimal_places=2) 
    selling_price = models.DecimalField(max_digits=10, decimal_places=2) 

    compatible_vehicles = models.ManyToManyField(Vehicle, blank=True)

    def __str__(self):
        return f"{self.name} ({self.part_number})"

# 5. The Stock (Where is it?)
class Stock(models.Model):
    part = models.ForeignKey(Part, on_delete=models.CASCADE)
    branch = models.ForeignKey(Branch, on_delete=models.CASCADE)
    quantity = models.IntegerField(default=0)
    location_in_warehouse = models.CharField(max_length=50, blank=True)

    class Meta:
        unique_together = ('part', 'branch')

# 6. THE NEW LEDGER (Sales History)
class Order(models.Model):
    # We use a UUID so people can't guess order numbers (e.g., 550e8400-e29b...)
    order_id = models.CharField(max_length=20, unique=True, editable=False) 
    created_at = models.DateTimeField(default=timezone.now)
    seller = models.ForeignKey(User, on_delete=models.CASCADE)
    customer = models.ForeignKey(Customer, on_delete=models.SET_NULL, null=True, blank=True)
    # Customer Info
    customer_email = models.EmailField(blank=True, null=True)
    
    # Money Totals
    subtotal = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    vat_amount = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    discount_amount = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    grand_total = models.DecimalField(max_digits=12, decimal_places=2, default=0)

    def save(self, *args, **kwargs):
        if not self.order_id:
            # Generate a short, unique ID like "ORD-93821"
            self.order_id = "ORD-" + str(uuid.uuid4().int)[:8]
        super().save(*args, **kwargs)

    def __str__(self):
        return f"{self.order_id} - {self.grand_total}"

# 2. UPDATED: The Sale (Now linked to an Order)
class Sale(models.Model):
    order = models.ForeignKey(Order, on_delete=models.CASCADE, null=True, related_name='items') # <--- LINKED
    part = models.ForeignKey(Part, on_delete=models.SET_NULL, null=True)
    branch = models.ForeignKey(Branch, on_delete=models.SET_NULL, null=True)
    seller = models.ForeignKey(User, on_delete=models.CASCADE) 
    quantity = models.PositiveIntegerField(default=1)
    
    price_at_sale = models.DecimalField(max_digits=10, decimal_places=2) 
    cost_at_sale = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    
    date_sold = models.DateTimeField(default=timezone.now) 

    # Status for Refunds
    is_refunded = models.BooleanField(default=False)

    def __str__(self):
        return f"{self.quantity} x {self.part.name}"

    @property
    def total_revenue(self):
        return self.price_at_sale * self.quantity

    @property
    def total_profit(self):
        # If refunded, profit is 0 (or negative depending on how you want to track it)
        if self.is_refunded:
            return 0
        return (self.price_at_sale - self.cost_at_sale) * self.quantity

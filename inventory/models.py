import uuid
from decimal import Decimal
from django.core.exceptions import ValidationError
from django.contrib.auth.models import User
from django.core.validators import MinValueValidator, RegexValidator
from django.db import models, transaction
from django.db.models import F, Q, Sum, Value
from django.db.models.functions import Coalesce
from django.utils import timezone

REQUIRED_BRANCH_NAMES = (
    "الصناعية القديمة",
    "مخرج 18",
    "شارع الجمعية",
)

REQUIRED_BRANCH_CODES = {
    "الصناعية القديمة": "OLDIND",
    "مخرج 18": "EX18",
    "شارع الجمعية": "ASSN",
}


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

    @staticmethod
    def normalize_name(value: str) -> str:
        return " ".join((value or "").split())

    def clean(self):
        self.name = self.normalize_name(self.name)
        self.code = (self.code or "").strip().upper()
        if not self.name:
            raise ValidationError({"name": "Branch name cannot be blank."})
        normalized_self = self.name.casefold()
        conflict = (
            Branch.objects.exclude(pk=self.pk)
            .values_list("name", flat=True)
        )
        for existing_name in conflict:
            if self.normalize_name(existing_name).casefold() == normalized_self:
                raise ValidationError({"name": "Branch name must be unique (case/space-insensitive)."})

    def save(self, *args, **kwargs):
        self.full_clean()
        super().save(*args, **kwargs)

    def __str__(self):
        return self.name


def required_branch_queryset():
    return Branch.objects.filter(name__in=REQUIRED_BRANCH_NAMES)


DEFAULT_LOCATION_CODE = "UNASSIGNED"


class Location(models.Model):
    branch = models.ForeignKey(Branch, on_delete=models.CASCADE, related_name="locations")
    code = models.CharField(max_length=30)
    name_ar = models.CharField(max_length=100, blank=True, default="")
    name_en = models.CharField(max_length=100, blank=True, default="")

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["branch", "code"], name="location_branch_code_unique"),
        ]
        indexes = [
            models.Index(fields=["branch", "code"]),
        ]
        ordering = ["branch__name", "code"]

    def save(self, *args, **kwargs):
        self.code = (self.code or "").strip().upper()
        self.name_ar = (self.name_ar or "").strip()
        self.name_en = (self.name_en or "").strip()
        super().save(*args, **kwargs)

    def __str__(self):
        label = self.name_en or self.name_ar or self.code
        return f"{self.branch.code}-{self.code} ({label})"


class UserProfile(models.Model):
    class Roles(models.TextChoices):
        ADMIN = "admin", "Admin"
        MANAGER = "manager", "Manager"
        CASHIER = "cashier", "Cashier"

    user = models.OneToOneField(User, on_delete=models.CASCADE, related_name="profile")
    employee_id = models.CharField(max_length=50, unique=True)
    role = models.CharField(max_length=20, choices=Roles.choices, default=Roles.CASHIER)
    branch = models.ForeignKey(
        Branch,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="staff",
        help_text="Default branch scope for non-manager users.",
    )

    class Meta:
        constraints = [
            models.CheckConstraint(
                condition=~Q(employee_id=""),
                name="userprofile_employee_id_not_blank",
            ),
        ]

    @classmethod
    def generate_auto_employee_id(cls, user_id=None, exclude_pk=None):
        base = f"AUTO-{int(user_id):05d}" if user_id else f"AUTO-{uuid.uuid4().hex[:10].upper()}"
        candidate = base
        suffix = 1

        queryset = cls.objects.all()
        if exclude_pk:
            queryset = queryset.exclude(pk=exclude_pk)

        while queryset.filter(employee_id=candidate).exists():
            token = f"-{suffix}"
            candidate = f"{base[:50 - len(token)]}{token}"
            suffix += 1
        return candidate

    def save(self, *args, **kwargs):
        self.employee_id = (self.employee_id or "").strip()
        if not self.employee_id:
            self.employee_id = self.generate_auto_employee_id(
                user_id=self.user_id,
                exclude_pk=self.pk,
            )
        super().save(*args, **kwargs)

    def __str__(self):
        branch_name = self.branch.name if self.branch else "No branch"
        emp = f" [{self.employee_id}]" if self.employee_id else ""
        return f"{self.user.username}{emp} ({self.role}) - {branch_name}"


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
    
    cost_price = models.DecimalField(
        max_digits=10,
        decimal_places=2,
        validators=[MinValueValidator(Decimal("0.00"))],
    )
    selling_price = models.DecimalField(
        max_digits=10,
        decimal_places=2,
        validators=[MinValueValidator(Decimal("0.00"))],
    )

    compatible_vehicles = models.ManyToManyField(Vehicle, blank=True)

    def __str__(self):
        return f"{self.name} ({self.part_number})"

# 5. The Stock (Where is it?)
class Stock(models.Model):
    part = models.ForeignKey(Part, on_delete=models.CASCADE)
    branch = models.ForeignKey(Branch, on_delete=models.CASCADE)
    quantity = models.PositiveIntegerField(default=0)
    min_stock_level = models.PositiveIntegerField(default=0)
    location_in_warehouse = models.CharField(max_length=50, blank=True)

    class Meta:
        unique_together = ('part', 'branch')
        indexes = [
            models.Index(fields=["branch", "quantity"]),
        ]

    def __str__(self):
        return f"{self.part} @ {self.branch} ({self.quantity})"


class StockLocation(models.Model):
    part = models.ForeignKey(Part, on_delete=models.CASCADE, related_name="stock_locations")
    branch = models.ForeignKey(Branch, on_delete=models.CASCADE, related_name="stock_locations")
    location = models.ForeignKey(Location, on_delete=models.CASCADE, related_name="stock_locations")
    quantity = models.PositiveIntegerField(default=0)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["part", "branch", "location"],
                name="stocklocation_part_branch_location_unique",
            ),
        ]
        indexes = [
            models.Index(fields=["branch", "part"]),
            models.Index(fields=["location", "part"]),
        ]

    def clean(self):
        if self.location_id and self.branch_id and self.location.branch_id != self.branch_id:
            raise ValidationError("Location branch must match stock location branch.")

    def save(self, *args, **kwargs):
        self.full_clean()
        super().save(*args, **kwargs)

    def __str__(self):
        return f"{self.part.part_number} @ {self.branch.code}/{self.location.code} ({self.quantity})"


class StockMovement(models.Model):
    part = models.ForeignKey(Part, on_delete=models.PROTECT, related_name="stock_movements")
    branch = models.ForeignKey(Branch, on_delete=models.CASCADE, related_name="stock_movements")
    qty = models.PositiveIntegerField(validators=[MinValueValidator(1)])
    action = models.CharField(max_length=40)
    from_location = models.ForeignKey(
        Location,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="movements_from",
    )
    to_location = models.ForeignKey(
        Location,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="movements_to",
    )
    reason = models.CharField(max_length=255, default="system")
    actor = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="stock_movements",
    )
    created_at = models.DateTimeField(default=timezone.now, db_index=True)

    class Meta:
        indexes = [
            models.Index(fields=["branch", "created_at"]),
            models.Index(fields=["part", "created_at"]),
            models.Index(fields=["action", "created_at"]),
        ]
        ordering = ["-created_at"]

    def clean(self):
        if self.from_location_id and self.from_location.branch_id != self.branch_id:
            raise ValidationError("from_location must belong to the same branch.")
        if self.to_location_id and self.to_location.branch_id != self.branch_id:
            raise ValidationError("to_location must belong to the same branch.")

    def save(self, *args, **kwargs):
        self.reason = (self.reason or "").strip() or "system"
        self.full_clean()
        super().save(*args, **kwargs)

    def __str__(self):
        return f"{self.action} {self.part.part_number} x{self.qty} @ {self.branch.code}"


def get_or_create_default_location(branch: Branch) -> Location:
    location, _ = Location.objects.get_or_create(
        branch=branch,
        code=DEFAULT_LOCATION_CODE,
        defaults={
            "name_ar": "غير محدد",
            "name_en": "Unassigned",
        },
    )
    return location


def sync_stock_total_from_locations(*, part_id: int, branch_id: int) -> int:
    total = int(
        StockLocation.objects.filter(part_id=part_id, branch_id=branch_id).aggregate(
            total=Coalesce(Sum("quantity"), Value(0))
        )["total"]
        or 0
    )
    stock = Stock.objects.filter(part_id=part_id, branch_id=branch_id).first()
    if stock:
        if stock.quantity != total:
            Stock.objects.filter(id=stock.id).update(quantity=total)
        return total
    if total > 0:
        Stock.objects.create(part_id=part_id, branch_id=branch_id, quantity=total)
    return total


def ensure_stock_locations_seeded_from_branch_stock(stock: Stock) -> None:
    if stock.quantity <= 0:
        return
    if StockLocation.objects.filter(part=stock.part, branch=stock.branch).exists():
        return
    default_location = get_or_create_default_location(stock.branch)
    StockLocation.objects.create(
        part=stock.part,
        branch=stock.branch,
        location=default_location,
        quantity=stock.quantity,
    )


def add_stock_to_location(
    *,
    part: Part,
    branch: Branch,
    quantity: int,
    reason: str,
    actor: User | None = None,
    location: Location | None = None,
    action: str = "add",
) -> StockMovement:
    qty = int(quantity or 0)
    if qty <= 0:
        raise ValueError("Quantity must be greater than zero.")

    target_location = location or get_or_create_default_location(branch)
    if target_location.branch_id != branch.id:
        raise ValueError("Target location does not belong to the selected branch.")

    with transaction.atomic():
        stock_row = (
            Stock.objects.select_for_update()
            .filter(part=part, branch=branch)
            .first()
        )
        if stock_row:
            ensure_stock_locations_seeded_from_branch_stock(stock_row)

        stock_location, _ = StockLocation.objects.select_for_update().get_or_create(
            part=part,
            branch=branch,
            location=target_location,
            defaults={"quantity": 0},
        )
        stock_location.quantity += qty
        stock_location.save(update_fields=["quantity"])

        movement = StockMovement.objects.create(
            part=part,
            branch=branch,
            qty=qty,
            action=action,
            from_location=None,
            to_location=target_location,
            reason=reason,
            actor=actor,
        )
        sync_stock_total_from_locations(part_id=part.id, branch_id=branch.id)
    return movement


def remove_stock_from_locations(
    *,
    part: Part,
    branch: Branch,
    quantity: int,
    reason: str,
    actor: User | None = None,
    from_location: Location | None = None,
    action: str = "remove",
) -> list[StockMovement]:
    qty = int(quantity or 0)
    if qty <= 0:
        raise ValueError("Quantity must be greater than zero.")
    if from_location and from_location.branch_id != branch.id:
        raise ValueError("Source location does not belong to the selected branch.")

    with transaction.atomic():
        stock_row = (
            Stock.objects.select_for_update()
            .filter(part=part, branch=branch)
            .first()
        )
        if stock_row:
            ensure_stock_locations_seeded_from_branch_stock(stock_row)

        base_qs = (
            StockLocation.objects.select_for_update()
            .select_related("location")
            .filter(part=part, branch=branch, quantity__gt=0)
        )
        if from_location:
            stock_rows = list(base_qs.filter(location=from_location).order_by("id"))
        else:
            stock_rows = list(base_qs.order_by("-quantity", "location__code", "id"))

        remaining = qty
        movements: list[StockMovement] = []
        for stock_row in stock_rows:
            if remaining <= 0:
                break
            taken = min(stock_row.quantity, remaining)
            stock_row.quantity -= taken
            stock_row.save(update_fields=["quantity"])
            movements.append(
                StockMovement.objects.create(
                    part=part,
                    branch=branch,
                    qty=taken,
                    action=action,
                    from_location=stock_row.location,
                    to_location=None,
                    reason=reason,
                    actor=actor,
                )
            )
            remaining -= taken

        if remaining > 0:
            raise ValueError("Insufficient stock in selected locations.")

        sync_stock_total_from_locations(part_id=part.id, branch_id=branch.id)
    return movements


def move_stock_between_locations(
    *,
    part: Part,
    branch: Branch,
    quantity: int,
    from_location: Location,
    to_location: Location,
    reason: str,
    actor: User | None = None,
    action: str = "move",
) -> StockMovement:
    qty = int(quantity or 0)
    if qty <= 0:
        raise ValueError("Quantity must be greater than zero.")
    if from_location.id == to_location.id:
        raise ValueError("Source and destination locations must be different.")
    if from_location.branch_id != branch.id or to_location.branch_id != branch.id:
        raise ValueError("Both locations must belong to the selected branch.")

    with transaction.atomic():
        stock_row = (
            Stock.objects.select_for_update()
            .filter(part=part, branch=branch)
            .first()
        )
        if stock_row:
            ensure_stock_locations_seeded_from_branch_stock(stock_row)

        source = (
            StockLocation.objects.select_for_update()
            .filter(part=part, branch=branch, location=from_location)
            .first()
        )
        if not source or source.quantity < qty:
            raise ValueError("Insufficient stock in source location.")

        destination, _ = StockLocation.objects.select_for_update().get_or_create(
            part=part,
            branch=branch,
            location=to_location,
            defaults={"quantity": 0},
        )
        source.quantity -= qty
        source.save(update_fields=["quantity"])
        destination.quantity += qty
        destination.save(update_fields=["quantity"])

        movement = StockMovement.objects.create(
            part=part,
            branch=branch,
            qty=qty,
            action=action,
            from_location=from_location,
            to_location=to_location,
            reason=reason,
            actor=actor,
        )
        sync_stock_total_from_locations(part_id=part.id, branch_id=branch.id)
    return movement


# 1. New Customer Model
class Customer(models.Model):
    phone_number = models.CharField(
        max_length=20,
        unique=True,
        validators=[RegexValidator(regex=r"^\+?\d{7,20}$", message="Enter a valid phone number.")],
    )
    name = models.CharField(max_length=100)
    car_model = models.CharField(max_length=100, blank=True, help_text="e.g. 2018 Camry")

    def __str__(self):
        return f"{self.name} ({self.phone_number})"

# 6. THE NEW LEDGER (Sales History)
class Order(models.Model):
    # We use a UUID so people can't guess order numbers (e.g., 550e8400-e29b...)
    order_id = models.CharField(max_length=20, unique=True, editable=False)
    created_at = models.DateTimeField(default=timezone.now)
    seller = models.ForeignKey(User, on_delete=models.CASCADE)
    branch = models.ForeignKey(Branch, on_delete=models.SET_NULL, null=True, blank=True, related_name="orders")
    customer = models.ForeignKey(Customer, on_delete=models.SET_NULL, null=True, blank=True)
    # Customer Info
    customer_email = models.EmailField(blank=True, null=True)
    
    # Money Totals
    subtotal = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    vat_amount = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    discount_amount = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    grand_total = models.DecimalField(max_digits=12, decimal_places=2, default=0)

    class Meta:
        indexes = [
            models.Index(fields=["created_at"]),
            models.Index(fields=["branch", "created_at"]),
        ]
        constraints = [
            models.CheckConstraint(condition=Q(subtotal__gte=0), name="order_subtotal_gte_0"),
            models.CheckConstraint(condition=Q(vat_amount__gte=0), name="order_vat_gte_0"),
            models.CheckConstraint(condition=Q(discount_amount__gte=0), name="order_discount_gte_0"),
            models.CheckConstraint(condition=Q(grand_total__gte=0), name="order_grand_total_gte_0"),
        ]

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

    price_at_sale = models.DecimalField(
        max_digits=10,
        decimal_places=2,
        validators=[MinValueValidator(Decimal("0.00"))],
    )
    cost_at_sale = models.DecimalField(
        max_digits=10,
        decimal_places=2,
        default=0,
        validators=[MinValueValidator(Decimal("0.00"))],
    )

    date_sold = models.DateTimeField(default=timezone.now)

    # Status for Refunds
    is_refunded = models.BooleanField(default=False)

    class Meta:
        indexes = [
            models.Index(fields=["date_sold"]),
            models.Index(fields=["branch", "date_sold"]),
        ]
        constraints = [
            models.CheckConstraint(condition=Q(quantity__gt=0), name="sale_qty_gt_0"),
            models.CheckConstraint(condition=Q(price_at_sale__gte=0), name="sale_price_gte_0"),
            models.CheckConstraint(condition=Q(cost_at_sale__gte=0), name="sale_cost_gte_0"),
        ]

    def __str__(self):
        part_name = self.part.name if self.part else "Deleted part"
        return f"{self.quantity} x {part_name}"

    @property
    def total_revenue(self):
        return self.price_at_sale * self.quantity

    @property
    def total_profit(self):
        # If refunded, profit is 0 (or negative depending on how you want to track it)
        if self.is_refunded:
            return Decimal("0.00")
        return (self.price_at_sale - self.cost_at_sale) * self.quantity


class TransferRequest(models.Model):
    class Status(models.TextChoices):
        REQUESTED = "requested", "Requested"
        APPROVED = "approved", "Approved"
        REJECTED = "rejected", "Rejected"
        PICKED_UP = "picked_up", "Picked Up"
        DELIVERED = "delivered", "Delivered"
        RECEIVED = "received", "Received"

    part = models.ForeignKey(Part, on_delete=models.PROTECT, related_name="transfer_requests")
    quantity = models.PositiveIntegerField(validators=[MinValueValidator(1)])

    source_branch = models.ForeignKey(
        Branch,
        on_delete=models.CASCADE,
        related_name="outgoing_transfer_requests",
        help_text="Branch that will send stock (Branch B).",
    )
    destination_branch = models.ForeignKey(
        Branch,
        on_delete=models.CASCADE,
        related_name="incoming_transfer_requests",
        help_text="Branch that requested stock (Branch A).",
    )

    requested_by = models.ForeignKey(
        User,
        on_delete=models.CASCADE,
        related_name="transfer_requests_created",
    )
    approved_by = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="transfer_requests_approved",
    )
    rejected_by = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="transfer_requests_rejected",
    )
    driver = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="transfer_requests_driving",
    )
    received_by = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="transfer_requests_received",
    )

    status = models.CharField(max_length=20, choices=Status.choices, default=Status.REQUESTED)
    reserved_quantity = models.PositiveIntegerField(default=0)

    notes = models.TextField(blank=True)
    rejection_reason = models.CharField(max_length=255, blank=True)

    created_at = models.DateTimeField(default=timezone.now)
    approved_at = models.DateTimeField(null=True, blank=True)
    rejected_at = models.DateTimeField(null=True, blank=True)
    picked_up_at = models.DateTimeField(null=True, blank=True)
    delivered_at = models.DateTimeField(null=True, blank=True)
    received_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        indexes = [
            models.Index(fields=["status", "created_at"]),
            models.Index(fields=["source_branch", "status"]),
            models.Index(fields=["destination_branch", "status"]),
        ]
        constraints = [
            models.CheckConstraint(condition=Q(quantity__gt=0), name="transfer_qty_gt_0"),
            models.CheckConstraint(
                condition=Q(source_branch__isnull=False) & Q(destination_branch__isnull=False),
                name="transfer_source_dest_not_null",
            ),
            models.CheckConstraint(
                condition=~Q(source_branch=F("destination_branch")),
                name="transfer_source_ne_destination",
            ),
            models.CheckConstraint(
                condition=Q(reserved_quantity__gte=0),
                name="transfer_reserved_qty_gte_0",
            ),
            models.CheckConstraint(
                condition=Q(reserved_quantity__lte=F("quantity")),
                name="transfer_reserved_qty_lte_qty",
            ),
        ]

    def __str__(self):
        return (
            f"Transfer #{self.pk} {self.part.part_number} x{self.quantity} "
            f"{self.source_branch.code}->{self.destination_branch.code} ({self.status})"
        )


class Ticket(models.Model):
    class Priority(models.TextChoices):
        LOW = "low", "Low"
        MEDIUM = "medium", "Medium"
        HIGH = "high", "High"

    class Status(models.TextChoices):
        NEW = "new", "New"
        OPEN = "open", "Open"
        IN_PROGRESS = "in_progress", "In Progress"
        FIXED = "fixed", "Fixed"

    title = models.CharField(max_length=200)
    description = models.TextField()
    branch = models.ForeignKey(
        Branch,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="tickets",
    )
    priority = models.CharField(max_length=20, choices=Priority.choices, default=Priority.MEDIUM)
    screenshot = models.FileField(upload_to="tickets/screenshots/", blank=True, null=True)
    reporter = models.ForeignKey(
        User,
        on_delete=models.CASCADE,
        related_name="reported_tickets",
    )
    assignee = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="assigned_tickets",
    )
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.NEW)
    internal_notes = models.TextField(blank=True, default="")
    created_at = models.DateTimeField(default=timezone.now, db_index=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["status", "created_at"]),
            models.Index(fields=["reporter", "created_at"]),
            models.Index(fields=["assignee", "created_at"]),
            models.Index(fields=["branch", "status", "created_at"]),
        ]

    def can_transition_to(self, next_status: str) -> bool:
        allowed = {
            self.Status.NEW: {self.Status.OPEN},
            self.Status.OPEN: {self.Status.IN_PROGRESS},
            self.Status.IN_PROGRESS: {self.Status.FIXED},
            self.Status.FIXED: set(),
        }
        if next_status == self.status:
            return True
        return next_status in allowed.get(self.status, set())

    def __str__(self):
        return f"Ticket #{self.pk} [{self.get_status_display()}] {self.title}"


class AuditLog(models.Model):
    actor = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="audit_logs",
    )
    actor_username = models.CharField(max_length=150, default="SYSTEM")
    actor_employee_id = models.CharField(max_length=50, blank=True, default="", db_index=True)

    timestamp = models.DateTimeField(default=timezone.now, db_index=True)
    action = models.CharField(max_length=64, db_index=True)
    reason = models.CharField(max_length=255, default="system")
    object_type = models.CharField(max_length=64)
    object_id = models.CharField(max_length=64, blank=True, default="")

    branch = models.ForeignKey(
        Branch,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="audit_logs",
    )

    before_data = models.JSONField(default=dict, blank=True)
    after_data = models.JSONField(default=dict, blank=True)

    class Meta:
        ordering = ["-timestamp"]
        indexes = [
            models.Index(fields=["action", "timestamp"]),
            models.Index(fields=["reason", "timestamp"]),
            models.Index(fields=["actor_employee_id", "timestamp"]),
            models.Index(fields=["branch", "timestamp"]),
        ]
        constraints = [
            models.CheckConstraint(
                condition=~Q(reason=""),
                name="auditlog_reason_not_blank",
            ),
        ]

    def __str__(self):
        return f"[{self.timestamp:%Y-%m-%d %H:%M}] {self.action} {self.object_type}:{self.object_id}"

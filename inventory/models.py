import uuid
import hashlib
import hmac
import secrets
from datetime import timedelta
from decimal import Decimal
from django.conf import settings
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
    address = models.CharField(max_length=255, blank=True, default="")
    phone_primary = models.CharField(max_length=30, blank=True, default="")
    phone_secondary = models.CharField(max_length=30, blank=True, default="")
    email = models.EmailField(blank=True, default="")
    website = models.URLField(blank=True, default="")
    commercial_registration_number = models.CharField(max_length=50, blank=True, default="")
    vat_registration_number = models.CharField(max_length=50, blank=True, default="")

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
        ADMIN = "admin", "مدير النظام"
        MANAGER = "manager", "مدير"
        CASHIER = "cashier", "كاشير"
        TECH = "tech", "فني"

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
    phone_number = models.CharField(
        max_length=20,
        blank=True,
        default="",
        validators=[RegexValidator(regex=r"^\+?\d{7,20}$", message="Enter a valid phone number.")],
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


class PasswordResetOtp(models.Model):
    class Channels(models.TextChoices):
        EMAIL = "email", "البريد الإلكتروني"
        PHONE = "phone", "الجوال"

    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name="password_reset_otps")
    channel = models.CharField(max_length=10, choices=Channels.choices, db_index=True)
    destination = models.CharField(max_length=255)
    code_hash = models.CharField(max_length=64)
    expires_at = models.DateTimeField(db_index=True)
    attempts = models.PositiveSmallIntegerField(default=0)
    verified_at = models.DateTimeField(null=True, blank=True)
    used_at = models.DateTimeField(null=True, blank=True)
    request_ip = models.GenericIPAddressField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["user", "channel", "created_at"]),
            models.Index(fields=["expires_at", "used_at"]),
        ]

    @staticmethod
    def _hash_code(raw_code: str) -> str:
        payload = f"{settings.SECRET_KEY}:{raw_code}".encode("utf-8")
        return hashlib.sha256(payload).hexdigest()

    @classmethod
    def issue_code(cls, user: User, channel: str, destination: str, request_ip: str | None = None):
        code = f"{secrets.randbelow(1000000):06d}"
        now = timezone.now()
        cls.objects.filter(
            user=user,
            channel=channel,
            used_at__isnull=True,
            verified_at__isnull=True,
            expires_at__gt=now,
        ).update(used_at=now)
        record = cls.objects.create(
            user=user,
            channel=channel,
            destination=destination,
            code_hash=cls._hash_code(code),
            expires_at=now + timedelta(minutes=15),
            request_ip=request_ip,
        )
        return record, code

    def is_expired(self) -> bool:
        return timezone.now() >= self.expires_at

    def verify_code(self, raw_code: str) -> bool:
        if self.used_at is not None or self.verified_at is not None or self.is_expired():
            return False
        expected = self.code_hash
        actual = self._hash_code((raw_code or "").strip())
        return hmac.compare_digest(expected, actual)


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
    sku = models.CharField(max_length=100, blank=True, default="", db_index=True)
    manufacturer_part_number = models.CharField(max_length=120, blank=True, default="", db_index=True)
    
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

    class Meta:
        indexes = [
            models.Index(fields=["name"]),
            models.Index(fields=["barcode"]),
            models.Index(fields=["sku"]),
            models.Index(fields=["manufacturer_part_number"]),
        ]

    def __str__(self):
        return f"{self.name} ({self.part_number})"


class PartBarcode(models.Model):
    part = models.ForeignKey(Part, on_delete=models.CASCADE, related_name="barcodes")
    barcode = models.CharField(max_length=100, unique=True)
    note = models.CharField(max_length=120, blank=True, default="")
    created_at = models.DateTimeField(default=timezone.now, db_index=True)

    class Meta:
        indexes = [
            models.Index(fields=["barcode"]),
            models.Index(fields=["part", "created_at"]),
        ]
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.barcode} -> {self.part.part_number}"


class PartBranchCost(models.Model):
    part = models.ForeignKey(Part, on_delete=models.CASCADE, related_name="branch_costs")
    branch = models.ForeignKey(Branch, on_delete=models.CASCADE, related_name="part_costs")
    avg_cost = models.DecimalField(max_digits=12, decimal_places=4, default=Decimal("0.0000"))
    last_cost = models.DecimalField(max_digits=12, decimal_places=4, default=Decimal("0.0000"))
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["part", "branch"], name="partbranchcost_part_branch_unique"),
        ]
        indexes = [
            models.Index(fields=["branch", "part"]),
        ]

    def __str__(self):
        return f"{self.part.part_number}@{self.branch.code} avg={self.avg_cost}"


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


def update_branch_average_cost(*, part: Part, branch: Branch, received_qty: int, received_unit_cost: Decimal) -> PartBranchCost:
    qty = int(received_qty or 0)
    if qty <= 0:
        raise ValueError("Received quantity must be greater than zero.")
    incoming_cost = Decimal(received_unit_cost or 0)
    if incoming_cost < 0:
        raise ValueError("Received unit cost cannot be negative.")

    with transaction.atomic():
        cost_row, _ = PartBranchCost.objects.select_for_update().get_or_create(
            part=part,
            branch=branch,
            defaults={"avg_cost": Decimal("0.0000"), "last_cost": Decimal("0.0000")},
        )
        stock_qty_after = int(
            Stock.objects.filter(part=part, branch=branch).values_list("quantity", flat=True).first()
            or 0
        )
        old_qty = max(stock_qty_after - qty, 0)
        old_avg = Decimal(cost_row.avg_cost or 0)
        denominator = old_qty + qty
        if denominator <= 0:
            new_avg = incoming_cost
        else:
            new_avg = ((Decimal(old_qty) * old_avg) + (Decimal(qty) * incoming_cost)) / Decimal(denominator)
        cost_row.avg_cost = new_avg.quantize(Decimal("0.0001"))
        cost_row.last_cost = incoming_cost.quantize(Decimal("0.0001"))
        cost_row.save(update_fields=["avg_cost", "last_cost", "updated_at"])
    return cost_row


class Vendor(models.Model):
    vendor_code = models.CharField(max_length=30, unique=True)
    name_ar = models.CharField(max_length=200, blank=True, default="")
    name_en = models.CharField(max_length=200, blank=True, null=True, unique=True)
    phone = models.CharField(max_length=30, blank=True, default="")
    email = models.EmailField(blank=True, default="")
    vat_number = models.CharField(max_length=50, blank=True, default="")
    cr_number = models.CharField(max_length=50, blank=True, default="")
    address = models.CharField(max_length=255, blank=True, default="")
    notes = models.TextField(blank=True, default="")
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(default=timezone.now, db_index=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        indexes = [
            models.Index(fields=["is_active", "name_ar"]),
            models.Index(fields=["vendor_code"]),
        ]
        ordering = ["name_ar", "name_en"]

    def save(self, *args, **kwargs):
        self.vendor_code = (self.vendor_code or "").strip().upper()
        if not self.vendor_code:
            self.vendor_code = f"VND-{uuid.uuid4().hex[:8].upper()}"
        if self.name_en == "":
            self.name_en = None
        super().save(*args, **kwargs)

    def __str__(self):
        return self.name_ar or self.name_en or self.vendor_code


class PurchaseOrder(models.Model):
    class Status(models.TextChoices):
        DRAFT = "draft", "مسودة"
        SENT = "sent", "مرسل"
        PARTIAL_RECEIVED = "partial_received", "استلام جزئي"
        RECEIVED = "received", "مستلم بالكامل"
        CANCELLED = "cancelled", "ملغي"

    po_number = models.CharField(max_length=40, unique=True)
    vendor = models.ForeignKey(Vendor, on_delete=models.PROTECT, related_name="purchase_orders")
    branch = models.ForeignKey(Branch, on_delete=models.PROTECT, related_name="purchase_orders")
    created_by = models.ForeignKey(User, on_delete=models.PROTECT, related_name="purchase_orders_created")
    status = models.CharField(max_length=30, choices=Status.choices, default=Status.DRAFT, db_index=True)
    created_at = models.DateTimeField(default=timezone.now, db_index=True)
    expected_date = models.DateField(null=True, blank=True)
    notes = models.TextField(blank=True, default="")

    class Meta:
        indexes = [
            models.Index(fields=["branch", "status", "created_at"]),
            models.Index(fields=["vendor", "created_at"]),
        ]
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.po_number} ({self.get_status_display()})"

    @property
    def is_closed(self) -> bool:
        return self.status in {self.Status.RECEIVED, self.Status.CANCELLED}

    def refresh_status_from_lines(self) -> None:
        if self.status == self.Status.CANCELLED:
            return
        line_rows = list(self.lines.values_list("qty_ordered", "qty_received"))
        if not line_rows:
            return
        total_ordered = sum(int(row[0] or 0) for row in line_rows)
        total_received = sum(int(row[1] or 0) for row in line_rows)
        if total_received <= 0:
            next_status = self.Status.SENT if self.status != self.Status.DRAFT else self.Status.DRAFT
        elif total_received < total_ordered:
            next_status = self.Status.PARTIAL_RECEIVED
        else:
            next_status = self.Status.RECEIVED
        if next_status != self.status:
            self.status = next_status
            self.save(update_fields=["status"])


class PurchaseOrderLine(models.Model):
    po = models.ForeignKey(PurchaseOrder, on_delete=models.CASCADE, related_name="lines")
    part = models.ForeignKey(Part, on_delete=models.PROTECT, related_name="purchase_order_lines")
    qty_ordered = models.PositiveIntegerField(validators=[MinValueValidator(1)])
    qty_received = models.PositiveIntegerField(default=0)
    unit_cost = models.DecimalField(max_digits=12, decimal_places=4, validators=[MinValueValidator(Decimal("0.0000"))])
    tax_rate = models.DecimalField(max_digits=5, decimal_places=2, default=Decimal("15.00"))
    discount = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal("0.00"))
    line_total = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal("0.00"))

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["po", "part"], name="po_line_unique_part_per_po"),
            models.CheckConstraint(condition=Q(qty_received__gte=0), name="po_line_qty_received_gte_0"),
            models.CheckConstraint(condition=Q(qty_received__lte=F("qty_ordered")), name="po_line_qty_received_lte_ordered"),
        ]
        indexes = [
            models.Index(fields=["po", "part"]),
        ]

    def save(self, *args, **kwargs):
        qty = Decimal(self.qty_ordered or 0)
        taxable = (qty * Decimal(self.unit_cost or 0)) - Decimal(self.discount or 0)
        if taxable < 0:
            taxable = Decimal("0.00")
        vat_amount = taxable * (Decimal(self.tax_rate or 0) / Decimal("100"))
        self.line_total = (taxable + vat_amount).quantize(Decimal("0.01"))
        super().save(*args, **kwargs)

    @property
    def remaining_qty(self) -> int:
        return max(int(self.qty_ordered or 0) - int(self.qty_received or 0), 0)


class PurchaseReceipt(models.Model):
    class Status(models.TextChoices):
        DRAFT = "draft", "مسودة"
        POSTED = "posted", "مرحّل"

    po = models.ForeignKey(PurchaseOrder, on_delete=models.PROTECT, related_name="receipts")
    branch = models.ForeignKey(Branch, on_delete=models.PROTECT, related_name="purchase_receipts")
    received_by = models.ForeignKey(User, on_delete=models.PROTECT, related_name="purchase_receipts_received")
    received_at = models.DateTimeField(default=timezone.now, db_index=True)
    invoice_ref = models.CharField(max_length=100, blank=True, default="")
    notes = models.TextField(blank=True, default="")
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.DRAFT, db_index=True)
    posted_at = models.DateTimeField(null=True, blank=True, db_index=True)

    class Meta:
        indexes = [
            models.Index(fields=["branch", "status", "received_at"]),
            models.Index(fields=["po", "received_at"]),
        ]
        ordering = ["-received_at"]

    def __str__(self):
        return f"GRN-{self.id} for {self.po.po_number}"


class PurchaseReceiptLine(models.Model):
    receipt = models.ForeignKey(PurchaseReceipt, on_delete=models.CASCADE, related_name="lines")
    po_line = models.ForeignKey(PurchaseOrderLine, on_delete=models.PROTECT, related_name="receipt_lines")
    qty_received = models.PositiveIntegerField(validators=[MinValueValidator(1)])
    unit_cost = models.DecimalField(max_digits=12, decimal_places=4, validators=[MinValueValidator(Decimal("0.0000"))])
    location = models.ForeignKey(Location, on_delete=models.SET_NULL, null=True, blank=True, related_name="purchase_receipt_lines")
    batch_lot = models.CharField(max_length=80, blank=True, default="")
    expiry = models.DateField(null=True, blank=True)
    line_total = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal("0.00"))

    class Meta:
        indexes = [
            models.Index(fields=["receipt", "po_line"]),
            models.Index(fields=["location"]),
        ]

    def clean(self):
        if self.location_id and self.receipt_id and self.location.branch_id != self.receipt.branch_id:
            raise ValidationError("Receipt location must belong to the same branch.")

    def save(self, *args, **kwargs):
        self.full_clean()
        taxable = Decimal(self.qty_received or 0) * Decimal(self.unit_cost or 0)
        self.line_total = taxable.quantize(Decimal("0.01"))
        super().save(*args, **kwargs)


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

    @property
    def ledger_balance(self) -> Decimal:
        totals = self.ledger_entries.aggregate(
            invoices=Coalesce(
                Sum("amount", filter=Q(entry_type=CustomerLedgerEntry.EntryType.INVOICE)),
                Value(Decimal("0.00")),
            ),
            payments=Coalesce(
                Sum("amount", filter=Q(entry_type=CustomerLedgerEntry.EntryType.PAYMENT)),
                Value(Decimal("0.00")),
            ),
            credits=Coalesce(
                Sum(
                    "amount",
                    filter=Q(entry_type__in=[CustomerLedgerEntry.EntryType.REFUND, CustomerLedgerEntry.EntryType.CREDIT_NOTE]),
                ),
                Value(Decimal("0.00")),
            ),
        )
        return (
            Decimal(totals["invoices"] or 0)
            - Decimal(totals["payments"] or 0)
            - Decimal(totals["credits"] or 0)
        ).quantize(Decimal("0.01"))

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


class CustomerLedgerEntry(models.Model):
    class EntryType(models.TextChoices):
        INVOICE = "invoice", "فاتورة"
        PAYMENT = "payment", "دفعة"
        REFUND = "refund", "استرجاع"
        CREDIT_NOTE = "credit_note", "إشعار دائن"

    customer = models.ForeignKey(Customer, on_delete=models.CASCADE, related_name="ledger_entries")
    order = models.ForeignKey(Order, on_delete=models.SET_NULL, null=True, blank=True, related_name="ledger_entries")
    entry_type = models.CharField(max_length=20, choices=EntryType.choices, db_index=True)
    amount = models.DecimalField(max_digits=12, decimal_places=2, validators=[MinValueValidator(Decimal("0.00"))])
    created_at = models.DateTimeField(default=timezone.now, db_index=True)
    created_by = models.ForeignKey(User, on_delete=models.PROTECT, related_name="customer_ledger_entries")
    notes = models.CharField(max_length=255, blank=True, default="")

    class Meta:
        indexes = [
            models.Index(fields=["customer", "created_at"]),
            models.Index(fields=["entry_type", "created_at"]),
        ]
        ordering = ["-created_at", "-id"]

    def __str__(self):
        return f"{self.customer_id}:{self.entry_type}:{self.amount}"


class Payment(models.Model):
    class Method(models.TextChoices):
        CASH = "cash", "نقدي"
        CARD = "card", "بطاقة"
        BANK = "bank", "تحويل بنكي"
        CREDIT = "credit", "آجل"

    customer = models.ForeignKey(Customer, on_delete=models.PROTECT, related_name="payments")
    branch = models.ForeignKey(Branch, on_delete=models.PROTECT, related_name="payments")
    method = models.CharField(max_length=20, choices=Method.choices, default=Method.CASH)
    amount = models.DecimalField(max_digits=12, decimal_places=2, validators=[MinValueValidator(Decimal("0.01"))])
    reference = models.CharField(max_length=120, blank=True, default="")
    order = models.ForeignKey(Order, on_delete=models.SET_NULL, null=True, blank=True, related_name="payments")
    created_by = models.ForeignKey(User, on_delete=models.PROTECT, related_name="payments_created")
    created_at = models.DateTimeField(default=timezone.now, db_index=True)

    class Meta:
        indexes = [
            models.Index(fields=["customer", "created_at"]),
            models.Index(fields=["branch", "created_at"]),
        ]
        ordering = ["-created_at", "-id"]

    def __str__(self):
        return f"PAY-{self.id} {self.amount}"


class BranchInvoiceSequence(models.Model):
    branch = models.OneToOneField(Branch, on_delete=models.CASCADE, related_name="invoice_sequence")
    next_number = models.PositiveIntegerField(default=1)
    last_issued_number = models.PositiveIntegerField(default=0)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"{self.branch.code}: next={self.next_number}"


class TaxInvoice(models.Model):
    class State(models.TextChoices):
        DRAFT = "draft", "مسودة"
        POSTED = "posted", "مرحّلة"

    class PaymentMethod(models.TextChoices):
        CASH = "cash", "نقدي"
        CARD = "card", "بطاقة"
        TRANSFER = "transfer", "تحويل"

    order = models.OneToOneField(Order, on_delete=models.PROTECT, related_name="invoice")
    branch = models.ForeignKey(Branch, on_delete=models.PROTECT, related_name="invoices")

    state = models.CharField(max_length=20, choices=State.choices, default=State.DRAFT, db_index=True)
    invoice_number = models.PositiveIntegerField()
    invoice_uuid = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)

    issue_date = models.DateField(default=timezone.localdate, db_index=True)
    issue_time = models.TimeField(default=timezone.localtime)
    posted_at = models.DateTimeField(null=True, blank=True, db_index=True)
    due_date = models.DateField(null=True, blank=True)

    payment_method = models.CharField(max_length=20, choices=PaymentMethod.choices, default=PaymentMethod.CASH)
    customer_name = models.CharField(max_length=200, blank=True, default="")
    customer_vat_number = models.CharField(max_length=50, blank=True, default="")

    subtotal_before_vat = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    discount_amount = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    advance_payment = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    vat_amount = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    net_amount = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    currency = models.CharField(max_length=3, default="SAR")
    total_in_words_ar = models.TextField(blank=True, default="")
    qr_payload = models.TextField(blank=True, default="")

    created_at = models.DateTimeField(default=timezone.now, db_index=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["branch", "invoice_number"], name="invoice_branch_number_unique"),
        ]
        indexes = [
            models.Index(fields=["branch", "issue_date"]),
            models.Index(fields=["state", "created_at"]),
            models.Index(fields=["invoice_uuid"]),
        ]

    def clean(self):
        existing = TaxInvoice.objects.filter(pk=self.pk).first() if self.pk else None
        if existing and existing.state == self.State.POSTED:
            allowed_changes = {"posted_at"}
            for field in self._meta.fields:
                name = field.name
                if name in {"id", "updated_at"} or name in allowed_changes:
                    continue
                if getattr(existing, name) != getattr(self, name):
                    raise ValidationError("Invoice is immutable after posting. Issue a credit note instead.")

    def save(self, *args, **kwargs):
        if self.state == self.State.POSTED and not self.posted_at:
            self.posted_at = timezone.now()
        self.full_clean()
        super().save(*args, **kwargs)

    def __str__(self):
        return f"INV-{self.branch.code}-{self.invoice_number:06d}"


class TaxInvoiceLine(models.Model):
    invoice = models.ForeignKey(TaxInvoice, on_delete=models.CASCADE, related_name="lines")
    serial_no = models.PositiveIntegerField()
    item_code = models.CharField(max_length=100, blank=True, default="")
    part_number = models.CharField(max_length=100, blank=True, default="")
    item_name = models.CharField(max_length=255)
    unit = models.CharField(max_length=30, default="PCS")
    quantity = models.PositiveIntegerField(default=1)
    unit_price = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    total_before_vat = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    vat_amount = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    total_after_vat = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    warehouse_location = models.CharField(max_length=120, blank=True, default="")

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["invoice", "serial_no"], name="invoice_line_serial_unique"),
        ]
        indexes = [
            models.Index(fields=["invoice", "serial_no"]),
        ]
        ordering = ["serial_no"]

    def clean(self):
        if self.invoice_id and self.invoice.state == TaxInvoice.State.POSTED:
            old = TaxInvoiceLine.objects.filter(pk=self.pk).first() if self.pk else None
            if old is None:
                raise ValidationError("Cannot add lines to a posted invoice.")
            for field in self._meta.fields:
                name = field.name
                if name in {"id"}:
                    continue
                if getattr(old, name) != getattr(self, name):
                    raise ValidationError("Invoice lines are immutable after posting.")

    def save(self, *args, **kwargs):
        self.full_clean()
        super().save(*args, **kwargs)


class CreditNote(models.Model):
    invoice = models.ForeignKey(TaxInvoice, on_delete=models.PROTECT, related_name="credit_notes")
    branch = models.ForeignKey(Branch, on_delete=models.PROTECT, related_name="credit_notes")
    order = models.ForeignKey(Order, on_delete=models.PROTECT, null=True, blank=True, related_name="credit_notes")
    customer = models.ForeignKey(Customer, on_delete=models.SET_NULL, null=True, blank=True, related_name="credit_notes")
    reason = models.CharField(max_length=255)
    amount_before_vat = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    vat_amount = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    amount_after_vat = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    return_to_stock = models.BooleanField(default=True)
    state = models.CharField(max_length=20, default="posted", db_index=True)
    created_by = models.ForeignKey(User, on_delete=models.PROTECT, related_name="credit_notes_created")
    created_at = models.DateTimeField(default=timezone.now, db_index=True)

    class Meta:
        indexes = [
            models.Index(fields=["branch", "created_at"]),
            models.Index(fields=["customer", "created_at"]),
        ]

    def __str__(self):
        return f"CN-{self.id} for {self.invoice}"


class CreditNoteLine(models.Model):
    credit_note = models.ForeignKey(CreditNote, on_delete=models.CASCADE, related_name="lines")
    sale = models.ForeignKey(Sale, on_delete=models.SET_NULL, null=True, blank=True, related_name="credit_note_lines")
    part = models.ForeignKey(Part, on_delete=models.PROTECT, related_name="credit_note_lines")
    quantity = models.PositiveIntegerField(validators=[MinValueValidator(1)])
    unit_price = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    vat_rate = models.DecimalField(max_digits=5, decimal_places=2, default=Decimal("15.00"))
    line_subtotal = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    line_vat = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    line_total = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    reason = models.CharField(max_length=255, blank=True, default="")
    return_to_stock = models.BooleanField(default=True)

    class Meta:
        indexes = [
            models.Index(fields=["credit_note", "part"]),
        ]

    def save(self, *args, **kwargs):
        subtotal = (Decimal(self.quantity or 0) * Decimal(self.unit_price or 0)).quantize(Decimal("0.01"))
        vat_amount = (subtotal * (Decimal(self.vat_rate or 0) / Decimal("100"))).quantize(Decimal("0.01"))
        self.line_subtotal = subtotal
        self.line_vat = vat_amount
        self.line_total = (subtotal + vat_amount).quantize(Decimal("0.01"))
        super().save(*args, **kwargs)


class CycleCountSession(models.Model):
    class Status(models.TextChoices):
        DRAFT = "draft", "مسودة"
        IN_PROGRESS = "in_progress", "قيد التنفيذ"
        SUBMITTED = "submitted", "مرفوع"
        APPROVED = "approved", "معتمد"
        REJECTED = "rejected", "مرفوض"

    branch = models.ForeignKey(Branch, on_delete=models.PROTECT, related_name="cycle_count_sessions")
    location = models.ForeignKey(
        Location,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="cycle_count_sessions",
    )
    created_by = models.ForeignKey(User, on_delete=models.PROTECT, related_name="cycle_count_sessions_created")
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.DRAFT, db_index=True)
    started_at = models.DateTimeField(default=timezone.now, db_index=True)
    submitted_at = models.DateTimeField(null=True, blank=True)
    approved_by = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="cycle_count_sessions_approved",
    )
    approved_at = models.DateTimeField(null=True, blank=True)
    notes = models.TextField(blank=True, default="")

    class Meta:
        indexes = [
            models.Index(fields=["branch", "status", "started_at"]),
        ]
        ordering = ["-started_at", "-id"]

    def __str__(self):
        return f"CC-{self.id} {self.branch.code} ({self.status})"


class CycleCountLine(models.Model):
    session = models.ForeignKey(CycleCountSession, on_delete=models.CASCADE, related_name="lines")
    part = models.ForeignKey(Part, on_delete=models.PROTECT, related_name="cycle_count_lines")
    counted_qty = models.IntegerField(default=0)
    system_qty_snapshot = models.IntegerField(default=0)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["session", "part"], name="cycle_count_line_unique_part_per_session"),
        ]
        indexes = [
            models.Index(fields=["session", "part"]),
        ]

    @property
    def variance(self) -> int:
        return int(self.counted_qty or 0) - int(self.system_qty_snapshot or 0)


class SmaccToken(models.Model):
    access_token = models.TextField()
    expires_at = models.DateTimeField(db_index=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"SMACC token expiring {self.expires_at:%Y-%m-%d %H:%M:%S}"


class SmaccSyncQueue(models.Model):
    class ObjectType(models.TextChoices):
        SALE_INVOICE = "SALE_INVOICE", "فاتورة بيع"
        CREDIT_NOTE = "CREDIT_NOTE", "إشعار دائن"
        CREDITOR = "CREDITOR", "دائن"

    class Status(models.TextChoices):
        PENDING = "PENDING", "بانتظار المعالجة"
        PROCESSING = "PROCESSING", "قيد المعالجة"
        SYNCED = "SYNCED", "تمت المزامنة"
        FAILED = "FAILED", "فشل"

    object_type = models.CharField(max_length=30, choices=ObjectType.choices)
    object_id = models.CharField(max_length=64)
    idempotency_key = models.CharField(max_length=128, unique=True)
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.PENDING, db_index=True)
    attempts = models.PositiveIntegerField(default=0)
    smacc_job_id = models.CharField(max_length=100, blank=True, default="")
    smacc_document_id = models.CharField(max_length=100, blank=True, default="", db_index=True)
    last_error = models.TextField(blank=True, default="")
    payload_json = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(default=timezone.now, db_index=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        indexes = [
            models.Index(fields=["status", "updated_at"]),
            models.Index(fields=["object_type", "object_id"]),
            models.Index(fields=["created_at"]),
        ]

    def __str__(self):
        return f"{self.object_type}:{self.object_id} [{self.status}]"


class SmaccSyncLog(models.Model):
    queue_item = models.ForeignKey(SmaccSyncQueue, on_delete=models.CASCADE, related_name="logs")
    request_payload = models.JSONField(default=dict, blank=True)
    response_payload = models.JSONField(default=dict, blank=True)
    http_status = models.IntegerField(null=True, blank=True)
    created_at = models.DateTimeField(default=timezone.now, db_index=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"SyncLog #{self.id} queue={self.queue_item_id} status={self.http_status}"


class TransferRequest(models.Model):
    class Status(models.TextChoices):
        REQUESTED = "requested", "مطلوب"
        APPROVED = "approved", "معتمد"
        REJECTED = "rejected", "مرفوض"
        PICKED_UP = "picked_up", "تم التحميل"
        DELIVERED = "delivered", "تم التسليم"
        RECEIVED = "received", "تم الاستلام"

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
    received_quantity = models.PositiveIntegerField(default=0)

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
            models.CheckConstraint(
                condition=Q(received_quantity__gte=0),
                name="transfer_received_qty_gte_0",
            ),
            models.CheckConstraint(
                condition=Q(received_quantity__lte=F("quantity")),
                name="transfer_received_qty_lte_qty",
            ),
        ]

    def __str__(self):
        return (
            f"Transfer #{self.pk} {self.part.part_number} x{self.quantity} "
            f"{self.source_branch.code}->{self.destination_branch.code} ({self.status})"
        )

    @property
    def remaining_quantity(self) -> int:
        return max(int(self.quantity or 0) - int(self.received_quantity or 0), 0)


class Ticket(models.Model):
    class Priority(models.TextChoices):
        LOW = "low", "منخفضة"
        MEDIUM = "medium", "متوسطة"
        HIGH = "high", "مرتفعة"

    class Status(models.TextChoices):
        NEW = "new", "جديدة"
        OPEN = "open", "مفتوحة"
        IN_PROGRESS = "in_progress", "قيد المعالجة"
        FIXED = "fixed", "تم الإصلاح"

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
    model_name = models.CharField(max_length=64, blank=True, default="", db_index=True)
    object_id = models.CharField(max_length=64, blank=True, default="")
    ip_address = models.GenericIPAddressField(null=True, blank=True)

    branch = models.ForeignKey(
        Branch,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="audit_logs",
    )

    before_data = models.JSONField(default=dict, blank=True)
    after_data = models.JSONField(default=dict, blank=True)
    old_values = models.JSONField(default=dict, blank=True)
    new_values = models.JSONField(default=dict, blank=True)

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

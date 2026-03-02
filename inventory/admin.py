from django import forms
from django.contrib import messages
from django.contrib import admin
from django.contrib.admin.helpers import ActionForm

from .audit import log_audit_event
from .models import (
    AuditLog,
    Branch,
    CreditNote,
    CreditNoteLine,
    Category,
    CustomerLedgerEntry,
    CycleCountLine,
    CycleCountSession,
    Customer,
    Location,
    Order,
    PartBarcode,
    PartBranchCost,
    Part,
    Payment,
    PurchaseOrder,
    PurchaseOrderLine,
    PurchaseReceipt,
    PurchaseReceiptLine,
    Sale,
    SmaccSyncLog,
    SmaccSyncQueue,
    SmaccToken,
    Stock,
    StockLocation,
    StockMovement,
    TaxInvoice,
    TaxInvoiceLine,
    Ticket,
    TransferRequest,
    UserProfile,
    Vendor,
    Vehicle,
)


class UserProfileBulkActionForm(ActionForm):
    role = forms.ChoiceField(
        required=False,
        choices=[("", "---------")] + list(UserProfile.Roles.choices),
        label="Set role",
    )
    branch = forms.ModelChoiceField(
        required=False,
        queryset=Branch.objects.none(),
        label="Set branch",
    )
    clear_branch = forms.BooleanField(required=False, label="Clear branch")

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["branch"].queryset = Branch.objects.all().order_by("name")


@admin.register(Part)
class PartAdmin(admin.ModelAdmin):
    list_display = ("name", "part_number", "selling_price", "cost_price", "category")
    list_filter = ("category",)
    search_fields = ("name", "part_number", "barcode", "sku", "manufacturer_part_number")
    filter_horizontal = ("compatible_vehicles",)

    def save_model(self, request, obj, form, change):
        old_obj = None
        if change and obj.pk:
            old_obj = Part.objects.filter(pk=obj.pk).first()

        super().save_model(request, obj, form, change)

        if old_obj and (
            old_obj.selling_price != obj.selling_price or old_obj.cost_price != obj.cost_price
        ):
            log_audit_event(
                actor=request.user,
                action="price.change",
                reason="admin_price_update",
                object_type="Part",
                object_id=obj.id,
                before={
                    "selling_price": old_obj.selling_price,
                    "cost_price": old_obj.cost_price,
                    "part_number": old_obj.part_number,
                },
                after={
                    "selling_price": obj.selling_price,
                    "cost_price": obj.cost_price,
                    "part_number": obj.part_number,
                },
            )


@admin.register(Stock)
class StockAdmin(admin.ModelAdmin):
    list_display = ("part", "branch", "quantity", "min_stock_level", "location_in_warehouse")
    list_filter = ("branch",)
    search_fields = ("part__name", "part__part_number", "branch__name")
    list_editable = ("min_stock_level",)
    readonly_fields = ("quantity",)

    def get_readonly_fields(self, request, obj=None):
        readonly = list(super().get_readonly_fields(request, obj))
        if obj is not None:
            # Prevent changing stock identity via admin once created.
            readonly.extend(["part", "branch"])
        return tuple(readonly)

    def save_model(self, request, obj, form, change):
        old_obj = None
        if change and obj.pk:
            old_obj = Stock.objects.select_related("part", "branch").filter(pk=obj.pk).first()

        if old_obj and old_obj.quantity != obj.quantity:
            # Safety: stock quantity must be mutated via operational flows (POS, receiving, stock locations, cycle count).
            obj.quantity = old_obj.quantity
            self.message_user(
                request,
                "تم منع تعديل الكمية من لوحة الإدارة. استخدم صفحات الموقع: المخزون حسب الموقع / الاستلام / الجرد.",
                level=messages.WARNING,
            )

        super().save_model(request, obj, form, change)

        if old_obj and (old_obj.min_stock_level != obj.min_stock_level):
            log_audit_event(
                actor=request.user,
                action="stock.adjustment",
                reason="admin_stock_edit",
                object_type="Stock",
                object_id=obj.id,
                branch=obj.branch,
                before={
                    "min_stock_level": old_obj.min_stock_level,
                    "part_number": old_obj.part.part_number,
                    "reason": "admin_edit",
                },
                after={
                    "min_stock_level": obj.min_stock_level,
                    "part_number": obj.part.part_number,
                    "reason": "admin_edit",
                },
                )


@admin.register(Location)
class LocationAdmin(admin.ModelAdmin):
    list_display = ("branch", "code", "name_en", "name_ar")
    list_filter = ("branch",)
    search_fields = ("code", "name_en", "name_ar", "branch__name", "branch__code")
    ordering = ("branch__name", "code")


@admin.register(StockLocation)
class StockLocationAdmin(admin.ModelAdmin):
    list_display = ("part", "branch", "location", "quantity")
    list_filter = ("branch", "location")
    search_fields = ("part__name", "part__part_number", "location__code", "branch__name")
    list_select_related = ("part", "branch", "location")


@admin.register(StockMovement)
class StockMovementAdmin(admin.ModelAdmin):
    list_display = ("created_at", "action", "part", "branch", "qty", "from_location", "to_location", "actor")
    list_filter = ("action", "branch", "created_at")
    search_fields = ("part__name", "part__part_number", "reason", "actor__username", "branch__name")
    list_select_related = ("part", "branch", "from_location", "to_location", "actor")
    readonly_fields = (
        "created_at",
        "part",
        "branch",
        "qty",
        "action",
        "from_location",
        "to_location",
        "reason",
        "actor",
    )

    def has_add_permission(self, request):
        return False


@admin.register(Sale)
class SaleAdmin(admin.ModelAdmin):
    list_display = (
        "date_sold",
        "order",
        "part",
        "branch",
        "seller",
        "seller_employee_id",
        "quantity",
        "price_at_sale",
        "is_refunded",
    )
    list_filter = ("branch", "is_refunded", "date_sold")
    search_fields = ("order__order_id", "part__name", "part__part_number", "seller__username", "seller__profile__employee_id")
    list_select_related = ("order", "part", "branch", "seller", "seller__profile")

    @admin.display(description="Employee ID", ordering="seller__profile__employee_id")
    def seller_employee_id(self, obj):
        profile = getattr(obj.seller, "profile", None)
        return profile.employee_id if profile else "-"


@admin.register(Order)
class OrderAdmin(admin.ModelAdmin):
    list_display = ("order_id", "created_at", "seller", "seller_employee_id", "branch", "grand_total")
    list_filter = ("branch", "created_at")
    search_fields = ("order_id", "seller__username", "seller__profile__employee_id", "customer__name", "customer__phone_number")
    list_select_related = ("seller", "seller__profile", "branch", "customer")

    @admin.display(description="Employee ID", ordering="seller__profile__employee_id")
    def seller_employee_id(self, obj):
        profile = getattr(obj.seller, "profile", None)
        return profile.employee_id if profile else "-"


@admin.register(TransferRequest)
class TransferRequestAdmin(admin.ModelAdmin):
    list_display = (
        "id",
        "created_at",
        "part",
        "quantity",
        "source_branch",
        "destination_branch",
        "status",
        "reserved_quantity",
        "received_quantity",
        "requested_by",
        "driver",
    )
    list_filter = ("status", "source_branch", "destination_branch", "created_at")
    search_fields = (
        "part__name",
        "part__part_number",
        "requested_by__username",
        "approved_by__username",
        "driver__username",
    )
    readonly_fields = (
        "created_at",
        "approved_at",
        "rejected_at",
        "picked_up_at",
        "delivered_at",
        "received_at",
    )
    list_select_related = (
        "part",
        "source_branch",
        "destination_branch",
        "requested_by",
        "approved_by",
        "rejected_by",
        "driver",
        "received_by",
    )


@admin.register(Ticket)
class TicketAdmin(admin.ModelAdmin):
    list_display = ("id", "created_at", "title", "status", "priority", "branch", "reporter", "assignee", "updated_at")
    list_filter = ("status", "priority", "branch", "created_at", "assignee")
    search_fields = ("title", "description", "reporter__username", "assignee__username", "branch__name")
    list_select_related = ("branch", "reporter", "assignee")


@admin.register(UserProfile)
class UserProfileAdmin(admin.ModelAdmin):
    list_display = ("user", "employee_id", "role", "branch", "phone_number")
    list_filter = ("role", "branch")
    search_fields = ("user__username", "employee_id", "phone_number")
    list_select_related = ("user", "branch")
    list_editable = ("role", "branch", "phone_number")
    action_form = UserProfileBulkActionForm
    actions = (
        "apply_bulk_role_branch",
        "set_role_admin",
        "set_role_manager",
        "set_role_cashier",
        "clear_branch_assignment",
    )

    def _log_role_change(self, request, profile, old_role, old_branch_id):
        log_audit_event(
            actor=request.user,
            action="role.change",
            reason="admin_role_update",
            object_type="UserProfile",
            object_id=profile.id,
            branch=profile.branch,
            before={
                "role": old_role,
                "branch_id": old_branch_id,
                "employee_id": profile.employee_id,
                "username": profile.user.username,
            },
            after={
                "role": profile.role,
                "branch_id": profile.branch_id,
                "employee_id": profile.employee_id,
                "username": profile.user.username,
            },
        )

    def _set_role_for_queryset(self, request, queryset, role_value, label):
        updated = 0
        for profile in queryset.select_related("branch", "user"):
            if profile.role == role_value:
                continue
            old_role = profile.role
            old_branch_id = profile.branch_id
            profile.role = role_value
            profile.save(update_fields=["role"])
            self._log_role_change(request, profile, old_role, old_branch_id)
            updated += 1
        self.message_user(request, f"{updated} profile(s) updated to {label}.")

    def save_model(self, request, obj, form, change):
        old_obj = None
        if change and obj.pk:
            old_obj = UserProfile.objects.filter(pk=obj.pk).first()

        super().save_model(request, obj, form, change)

        if old_obj and old_obj.role != obj.role:
            self._log_role_change(request, obj, old_obj.role, old_obj.branch_id)

    @admin.action(description="Apply selected role/branch from action controls")
    def apply_bulk_role_branch(self, request, queryset):
        role = request.POST.get("role")
        branch_id = request.POST.get("branch")
        clear_branch = request.POST.get("clear_branch") in {"1", "on", "true", "True"}
        selected_branch = Branch.objects.filter(pk=branch_id).first() if branch_id else None

        updates = 0
        for profile in queryset.select_related("branch", "user"):
            old_role = profile.role
            old_branch_id = profile.branch_id
            changed_fields = []

            if role in {choice for choice, _ in UserProfile.Roles.choices} and profile.role != role:
                profile.role = role
                changed_fields.append("role")

            if clear_branch and profile.branch_id is not None:
                profile.branch = None
                changed_fields.append("branch")
            elif selected_branch and profile.branch_id != selected_branch.id:
                profile.branch = selected_branch
                changed_fields.append("branch")

            if not changed_fields:
                continue

            profile.save(update_fields=changed_fields)
            updates += 1

            if old_role != profile.role:
                self._log_role_change(request, profile, old_role, old_branch_id)

        if updates == 0:
            self.message_user(
                request,
                "No bulk changes were applied. Choose role or branch before running this action.",
                level=messages.WARNING,
            )
            return

        self.message_user(request, f"Bulk update applied to {updates} profile(s).")

    @admin.action(description="Set selected profiles to role: admin")
    def set_role_admin(self, request, queryset):
        self._set_role_for_queryset(request, queryset, UserProfile.Roles.ADMIN, "admin")

    @admin.action(description="Set selected profiles to role: manager")
    def set_role_manager(self, request, queryset):
        self._set_role_for_queryset(request, queryset, UserProfile.Roles.MANAGER, "manager")

    @admin.action(description="Set selected profiles to role: cashier")
    def set_role_cashier(self, request, queryset):
        self._set_role_for_queryset(request, queryset, UserProfile.Roles.CASHIER, "cashier")

    @admin.action(description="Clear branch assignment for selected profiles")
    def clear_branch_assignment(self, request, queryset):
        updated = queryset.update(branch=None)
        self.message_user(request, f"Cleared branch for {updated} profile(s).")


@admin.register(AuditLog)
class AuditLogAdmin(admin.ModelAdmin):
    list_display = (
        "timestamp",
        "actor_username",
        "actor_employee_id",
        "ip_address",
        "branch",
        "action",
        "model_name",
        "reason",
        "object_type",
        "object_id",
    )
    list_filter = ("action", "model_name", "reason", "branch", "timestamp")
    search_fields = ("actor_username", "actor_employee_id", "ip_address", "action", "reason", "object_type", "object_id")
    readonly_fields = (
        "timestamp",
        "actor",
        "actor_username",
        "actor_employee_id",
        "ip_address",
        "branch",
        "action",
        "model_name",
        "reason",
        "object_type",
        "object_id",
        "before_data",
        "after_data",
        "old_values",
        "new_values",
    )
    list_select_related = ("actor", "branch")

    def has_add_permission(self, request):
        return False


@admin.register(Branch)
class BranchAdmin(admin.ModelAdmin):
    list_display = ("name", "code")
    search_fields = ("name", "code")

    def has_add_permission(self, request):
        return bool(request.user and request.user.is_superuser)

    def has_change_permission(self, request, obj=None):
        if request.method in {"GET", "HEAD", "OPTIONS"}:
            return True
        return bool(request.user and request.user.is_superuser)

    def has_delete_permission(self, request, obj=None):
        return bool(request.user and request.user.is_superuser)


admin.site.register(Vehicle)
admin.site.register(Category)
admin.site.register(Customer)
admin.site.register(PartBarcode)
admin.site.register(PartBranchCost)


@admin.register(Vendor)
class VendorAdmin(admin.ModelAdmin):
    list_display = ("vendor_code", "name_ar", "name_en", "phone", "is_active")
    list_filter = ("is_active",)
    search_fields = ("vendor_code", "name_ar", "name_en", "phone", "email")


class PurchaseOrderLineInline(admin.TabularInline):
    model = PurchaseOrderLine
    extra = 0


@admin.register(PurchaseOrder)
class PurchaseOrderAdmin(admin.ModelAdmin):
    list_display = ("po_number", "vendor", "branch", "status", "created_by", "created_at")
    list_filter = ("status", "branch", "created_at")
    search_fields = ("po_number", "vendor__name_ar", "vendor__name_en")
    inlines = [PurchaseOrderLineInline]


class PurchaseReceiptLineInline(admin.TabularInline):
    model = PurchaseReceiptLine
    extra = 0


@admin.register(PurchaseReceipt)
class PurchaseReceiptAdmin(admin.ModelAdmin):
    list_display = ("id", "po", "branch", "status", "received_by", "received_at", "posted_at")
    list_filter = ("status", "branch", "received_at")
    search_fields = ("po__po_number", "invoice_ref")
    inlines = [PurchaseReceiptLineInline]


@admin.register(CustomerLedgerEntry)
class CustomerLedgerEntryAdmin(admin.ModelAdmin):
    list_display = ("created_at", "customer", "entry_type", "amount", "order", "created_by")
    list_filter = ("entry_type", "created_at")
    search_fields = ("customer__name", "customer__phone_number", "order__order_id", "notes")


@admin.register(Payment)
class PaymentAdmin(admin.ModelAdmin):
    list_display = ("created_at", "customer", "branch", "method", "amount", "reference", "created_by")
    list_filter = ("method", "branch", "created_at")
    search_fields = ("customer__name", "customer__phone_number", "reference", "order__order_id")


@admin.register(CreditNoteLine)
class CreditNoteLineAdmin(admin.ModelAdmin):
    list_display = ("credit_note", "part", "quantity", "line_total", "return_to_stock")
    list_filter = ("return_to_stock",)
    search_fields = ("credit_note__id", "part__part_number", "part__name")


@admin.register(CycleCountSession)
class CycleCountSessionAdmin(admin.ModelAdmin):
    list_display = ("id", "branch", "location", "status", "created_by", "started_at", "approved_by")
    list_filter = ("status", "branch", "started_at")
    search_fields = ("id", "branch__name", "notes")


@admin.register(CycleCountLine)
class CycleCountLineAdmin(admin.ModelAdmin):
    list_display = ("session", "part", "counted_qty", "system_qty_snapshot")
    list_filter = ("session__branch",)
    search_fields = ("part__part_number", "part__name")


class TaxInvoiceLineInline(admin.TabularInline):
    model = TaxInvoiceLine
    extra = 0
    can_delete = False
    readonly_fields = (
        "serial_no",
        "item_code",
        "part_number",
        "item_name",
        "unit",
        "quantity",
        "unit_price",
        "total_before_vat",
        "vat_amount",
        "total_after_vat",
        "warehouse_location",
    )


@admin.register(TaxInvoice)
class TaxInvoiceAdmin(admin.ModelAdmin):
    list_display = (
        "id",
        "invoice_number",
        "invoice_uuid",
        "branch",
        "state",
        "payment_method",
        "issue_date",
        "customer_name",
        "net_amount",
    )
    list_filter = ("state", "branch", "payment_method", "issue_date")
    search_fields = ("invoice_uuid", "invoice_number", "customer_name", "customer_vat_number", "order__order_id")
    readonly_fields = (
        "invoice_uuid",
        "invoice_number",
        "created_at",
        "updated_at",
        "posted_at",
        "qr_payload",
        "total_in_words_ar",
    )
    inlines = [TaxInvoiceLineInline]


@admin.register(CreditNote)
class CreditNoteAdmin(admin.ModelAdmin):
    list_display = ("id", "invoice", "order", "customer", "branch", "amount_after_vat", "created_by", "created_at")
    list_filter = ("branch", "created_at", "return_to_stock")
    search_fields = ("invoice__invoice_uuid", "order__order_id", "customer__name", "reason")


@admin.register(SmaccSyncQueue)
class SmaccSyncQueueAdmin(admin.ModelAdmin):
    list_display = ("id", "object_type", "object_id", "status", "attempts", "smacc_job_id", "smacc_document_id", "updated_at")
    list_filter = ("status", "object_type", "created_at")
    search_fields = ("object_id", "idempotency_key", "smacc_job_id", "smacc_document_id", "last_error")
    readonly_fields = ("created_at", "updated_at")


@admin.register(SmaccSyncLog)
class SmaccSyncLogAdmin(admin.ModelAdmin):
    list_display = ("id", "queue_item", "http_status", "created_at")
    list_filter = ("http_status", "created_at")
    search_fields = ("queue_item__object_id", "queue_item__smacc_document_id")
    readonly_fields = ("created_at",)


@admin.register(SmaccToken)
class SmaccTokenAdmin(admin.ModelAdmin):
    list_display = ("id", "expires_at", "updated_at")
    readonly_fields = ("updated_at",)

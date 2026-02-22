from django import forms
from django.contrib import messages
from django.contrib import admin
from django.contrib.admin.helpers import ActionForm

from .models import (
    Branch,
    Category,
    Customer,
    Order,
    Part,
    Sale,
    Stock,
    UserProfile,
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
    search_fields = ("name", "part_number", "barcode")
    filter_horizontal = ("compatible_vehicles",)


@admin.register(Stock)
class StockAdmin(admin.ModelAdmin):
    list_display = ("part", "branch", "quantity", "location_in_warehouse")
    list_filter = ("branch",)
    search_fields = ("part__name", "part__part_number", "branch__name")


@admin.register(Sale)
class SaleAdmin(admin.ModelAdmin):
    list_display = ("date_sold", "order", "part", "branch", "seller", "quantity", "price_at_sale", "is_refunded")
    list_filter = ("branch", "is_refunded", "date_sold")
    search_fields = ("order__order_id", "part__name", "part__part_number", "seller__username")
    list_select_related = ("order", "part", "branch", "seller")


@admin.register(Order)
class OrderAdmin(admin.ModelAdmin):
    list_display = ("order_id", "created_at", "seller", "branch", "grand_total")
    list_filter = ("branch", "created_at")
    search_fields = ("order_id", "seller__username", "customer__name", "customer__phone_number")
    list_select_related = ("seller", "branch", "customer")


@admin.register(UserProfile)
class UserProfileAdmin(admin.ModelAdmin):
    list_display = ("user", "role", "branch")
    list_filter = ("role", "branch")
    search_fields = ("user__username",)
    list_select_related = ("user", "branch")
    list_editable = ("role", "branch")
    action_form = UserProfileBulkActionForm
    actions = (
        "apply_bulk_role_branch",
        "set_role_admin",
        "set_role_manager",
        "set_role_cashier",
        "clear_branch_assignment",
    )

    @admin.action(description="Apply selected role/branch from action controls")
    def apply_bulk_role_branch(self, request, queryset):
        role = request.POST.get("role")
        branch_id = request.POST.get("branch")
        clear_branch = request.POST.get("clear_branch") in {"1", "on", "true", "True"}

        updates = 0
        if role in {choice for choice, _ in UserProfile.Roles.choices}:
            updates += queryset.update(role=role)

        if clear_branch:
            updates += queryset.update(branch=None)
        elif branch_id:
            branch = Branch.objects.filter(pk=branch_id).first()
            if branch:
                updates += queryset.update(branch=branch)

        if updates == 0:
            self.message_user(
                request,
                "No bulk changes were applied. Choose role or branch before running this action.",
                level=messages.WARNING,
            )
            return

        self.message_user(request, f"Bulk update applied to {queryset.count()} profile(s).")

    @admin.action(description="Set selected profiles to role: admin")
    def set_role_admin(self, request, queryset):
        updated = queryset.update(role=UserProfile.Roles.ADMIN)
        self.message_user(request, f"{updated} profile(s) updated to admin.")

    @admin.action(description="Set selected profiles to role: manager")
    def set_role_manager(self, request, queryset):
        updated = queryset.update(role=UserProfile.Roles.MANAGER)
        self.message_user(request, f"{updated} profile(s) updated to manager.")

    @admin.action(description="Set selected profiles to role: cashier")
    def set_role_cashier(self, request, queryset):
        updated = queryset.update(role=UserProfile.Roles.CASHIER)
        self.message_user(request, f"{updated} profile(s) updated to cashier.")

    @admin.action(description="Clear branch assignment for selected profiles")
    def clear_branch_assignment(self, request, queryset):
        updated = queryset.update(branch=None)
        self.message_user(request, f"Cleared branch for {updated} profile(s).")


admin.site.register(Vehicle)
admin.site.register(Branch)
admin.site.register(Category)
admin.site.register(Customer)

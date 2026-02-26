from django.db.models import Sum, Value
from django.db.models.functions import Coalesce

from .models import Branch, REQUIRED_BRANCH_NAMES, Stock, TransferRequest, UserProfile


ACTIVE_BRANCH_SESSION_KEY = "active_branch_id"


def nav_context(request):
    if not request.user.is_authenticated:
        return {
            "nav_is_manager": False,
            "nav_is_admin": False,
            "nav_active_branch": None,
            "nav_branch_options": [],
        }

    default_role = UserProfile.Roles.ADMIN if request.user.is_superuser else UserProfile.Roles.CASHIER
    profile, _ = UserProfile.objects.get_or_create(
        user=request.user,
        defaults={"role": default_role},
    )

    nav_is_manager = (
        request.user.is_superuser
        or profile.role in {UserProfile.Roles.MANAGER, UserProfile.Roles.ADMIN}
        or request.user.groups.filter(name__in=["manager", "admin"]).exists()
    )
    nav_is_admin = (
        request.user.is_superuser
        or profile.role == UserProfile.Roles.ADMIN
        or request.user.groups.filter(name="admin").exists()
    )
    if nav_is_admin:
        branch_raw = request.session.get(ACTIVE_BRANCH_SESSION_KEY)
        try:
            branch_id = int(branch_raw)
        except (TypeError, ValueError):
            branch_id = None
        nav_active_branch = (
            Branch.objects.filter(id=branch_id, name__in=REQUIRED_BRANCH_NAMES).first()
            if branch_id
            else None
        )
        nav_branch_options = Branch.objects.filter(name__in=REQUIRED_BRANCH_NAMES).order_by("name")
    else:
        nav_active_branch = profile.branch
        nav_branch_options = []

    nav_low_stock_count = 0
    if nav_is_manager:
        stock_qs = Stock.objects.all()
        if nav_is_admin and nav_active_branch is not None:
            stock_qs = stock_qs.filter(branch=nav_active_branch)
        elif not nav_is_admin:
            if profile.branch is None:
                stock_qs = Stock.objects.none()
            else:
                stock_qs = stock_qs.filter(branch=profile.branch)

        pairs = list(stock_qs.values_list("part_id", "branch_id", "quantity", "min_stock_level"))
        if pairs:
            part_ids = {part_id for part_id, _, _, _ in pairs}
            branch_ids = {branch_id for _, branch_id, _, _ in pairs}
            reserved_rows = (
                TransferRequest.objects.filter(
                    status__in=[
                        TransferRequest.Status.APPROVED,
                        TransferRequest.Status.PICKED_UP,
                        TransferRequest.Status.DELIVERED,
                    ],
                    part_id__in=part_ids,
                    source_branch_id__in=branch_ids,
                )
                .values("part_id", "source_branch_id")
                .annotate(total_reserved=Coalesce(Sum("reserved_quantity"), Value(0)))
            )
            reserved_map = {
                (row["part_id"], row["source_branch_id"]): int(row["total_reserved"] or 0)
                for row in reserved_rows
            }
            nav_low_stock_count = sum(
                1
                for part_id, branch_id, quantity, min_level in pairs
                if int(min_level) > 0
                and max(int(quantity) - reserved_map.get((part_id, branch_id), 0), 0) <= int(min_level)
            )

    return {
        "nav_is_manager": nav_is_manager,
        "nav_is_admin": nav_is_admin,
        "nav_active_branch": nav_active_branch,
        "nav_branch_options": nav_branch_options,
        "nav_low_stock_count": nav_low_stock_count,
    }

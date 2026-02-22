import csv
from datetime import timedelta
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

from django.contrib import messages
from django.contrib.auth.decorators import login_required, user_passes_test
from django.core.cache import cache
from django.core.paginator import Paginator
from django.db import transaction
from django.db.models import Case, Count, DecimalField, ExpressionWrapper, F, Prefetch, Q, Sum, Value, When
from django.db.models.functions import Coalesce, TruncDate
from django.http import HttpResponse, HttpResponseForbidden, JsonResponse, StreamingHttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.http import url_has_allowed_host_and_scheme
from django.views.decorators.http import require_GET, require_POST

from .models import Branch, Customer, Order, Part, Sale, Stock, UserProfile, Vehicle

VAT_RATE = Decimal("0.15")
CSV_FORMULA_PREFIXES = ("=", "+", "-", "@", "\t", "\r")


def _quantize_money(value: Decimal) -> Decimal:
    return value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def _parse_decimal(value: str | None, default: Decimal = Decimal("0.00")) -> Decimal:
    try:
        return Decimal(value or default)
    except (TypeError, InvalidOperation):
        return default


def _is_ajax_request(request) -> bool:
    return request.headers.get("x-requested-with") == "XMLHttpRequest"


def _safe_next_url(request, fallback_name: str) -> str:
    next_url = request.POST.get("next") or request.GET.get("next") or request.META.get("HTTP_REFERER")
    if next_url and url_has_allowed_host_and_scheme(next_url, {request.get_host()}, require_https=request.is_secure()):
        return next_url
    return reverse(fallback_name)


def _get_or_create_profile(user) -> UserProfile:
    default_role = UserProfile.Roles.ADMIN if user.is_superuser else UserProfile.Roles.CASHIER
    profile, _ = UserProfile.objects.select_related("branch").get_or_create(
        user=user,
        defaults={"role": default_role},
    )
    return profile


def is_manager(user) -> bool:
    if not user.is_authenticated:
        return False
    if user.is_superuser:
        return True
    if user.groups.filter(name__in=["manager", "admin"]).exists():
        return True
    profile = _get_or_create_profile(user)
    return profile.role in {UserProfile.Roles.MANAGER, UserProfile.Roles.ADMIN}


manager_required = user_passes_test(is_manager)


def _user_branch(user):
    profile = _get_or_create_profile(user)
    return profile.branch


def _scope_branch(queryset, user, field_name: str = "branch"):
    if is_manager(user):
        return queryset
    branch = _user_branch(user)
    if not branch:
        return queryset.none()
    return queryset.filter(**{field_name: branch})


def _user_has_branch_access(user, branch: Branch | None) -> bool:
    if branch is None:
        return is_manager(user)
    if is_manager(user):
        return True
    user_branch = _user_branch(user)
    return bool(user_branch and user_branch.id == branch.id)


def _accessible_branches(user):
    if is_manager(user):
        cache_key = "inventory:branches:all"
        cached = cache.get(cache_key)
        if cached is None:
            cached = list(Branch.objects.all().order_by("name"))
            cache.set(cache_key, cached, 300)
        return cached

    branch = _user_branch(user)
    if not branch:
        return []
    return [branch]


def _get_cart(request) -> dict[str, int]:
    raw_cart = request.session.get("cart", {})
    normalized: dict[str, int] = {}
    for stock_id, quantity in raw_cart.items():
        try:
            normalized_id = str(int(stock_id))
            normalized_qty = int(quantity)
        except (TypeError, ValueError):
            continue
        if normalized_qty > 0:
            normalized[normalized_id] = normalized_qty

    if normalized != raw_cart:
        request.session["cart"] = normalized
        request.session.modified = True
    return normalized


def _stock_scope_for_user(user):
    return _scope_branch(
        Stock.objects.select_related("part", "branch"),
        user,
        field_name="branch",
    )


def _build_cart_items(request):
    cart = _get_cart(request)
    if not cart:
        return [], Decimal("0.00"), Decimal("0.00"), 0

    stocks = _stock_scope_for_user(request.user).filter(id__in=[int(stock_id) for stock_id in cart.keys()])
    stock_map = {str(stock.id): stock for stock in stocks}

    cart_items = []
    subtotal = Decimal("0.00")
    estimated_profit = Decimal("0.00")
    total_qty = 0
    valid_cart = {}

    for stock_id, quantity in cart.items():
        stock = stock_map.get(stock_id)
        if not stock:
            continue

        line_total = stock.part.selling_price * quantity
        line_cost = stock.part.cost_price * quantity
        subtotal += line_total
        estimated_profit += (line_total - line_cost)
        total_qty += quantity

        cart_items.append(
            {
                "stock": stock,
                "quantity": quantity,
                "subtotal": line_total,
                "total_price": line_total,
                "unit_price": stock.part.selling_price,
                "unit_cost": stock.part.cost_price,
            }
        )
        valid_cart[stock_id] = quantity

    if valid_cart != cart:
        request.session["cart"] = valid_cart
        request.session.modified = True

    return cart_items, subtotal, estimated_profit, total_qty


def _warn_if_branch_unassigned(request):
    if is_manager(request.user):
        return
    if _user_branch(request.user):
        return
    if request.session.get("branch_warning_shown"):
        return

    messages.warning(
        request,
        "Your account has no branch assigned. Ask an admin to assign one for full POS access.",
    )
    request.session["branch_warning_shown"] = True


@login_required
def part_search(request):
    _warn_if_branch_unassigned(request)

    vehicles = Vehicle.objects.all().order_by("make", "model", "year")
    query_text = (request.GET.get("q") or "").strip()
    query_vehicle_id = request.GET.get("vehicle")
    stock_scope = _stock_scope_for_user(request.user).filter(quantity__gt=0)

    part_page = None
    if query_text or query_vehicle_id:
        stock_filter = Q()
        if query_vehicle_id:
            stock_filter &= Q(part__compatible_vehicles__id=query_vehicle_id)
        if query_text:
            stock_filter &= (
                Q(part__part_number__icontains=query_text)
                | Q(part__name__icontains=query_text)
                | Q(part__barcode=query_text)
            )

        filtered_stock = stock_scope.filter(stock_filter).order_by("part__name", "branch__name")
        part_queryset = (
            Part.objects.filter(stock__in=filtered_stock)
            .distinct()
            .order_by("name")
            .prefetch_related(
                Prefetch(
                    "stock_set",
                    queryset=filtered_stock,
                    to_attr="available_stock",
                )
            )
        )
        part_page = Paginator(part_queryset, 20).get_page(request.GET.get("page"))

    cart_total_count = sum(_get_cart(request).values())

    return render(
        request,
        "inventory/search.html",
        {
            "vehicles": vehicles,
            "part_page": part_page,
            "results": part_page.object_list if part_page else None,
            "query": query_text,
            "query_vehicle_id": query_vehicle_id,
            "cart_total_count": cart_total_count,
        },
    )


@login_required
def pos_console(request):
    _warn_if_branch_unassigned(request)

    query = (request.GET.get("q") or "").strip()
    results = []
    if query:
        results = (
            _stock_scope_for_user(request.user)
            .filter(
                Q(part__part_number__icontains=query)
                | Q(part__name__icontains=query)
                | Q(part__barcode=query)
            )
            .order_by("part__name", "branch__name")[:30]
        )

    cart_items, subtotal, _estimated_profit, cart_total_count = _build_cart_items(request)

    return render(
        request,
        "inventory/pos_console.html",
        {
            "results": results,
            "query": query,
            "cart_items": cart_items,
            "subtotal": subtotal,
            "cart_total_count": cart_total_count,
            "active_branch": _user_branch(request.user),
            "is_manager": is_manager(request.user),
        },
    )


@login_required
@require_POST
def add_to_cart(request, stock_id):
    _warn_if_branch_unassigned(request)

    stock = get_object_or_404(_stock_scope_for_user(request.user), id=stock_id)

    try:
        quantity = int(request.POST.get("quantity", 1))
    except (TypeError, ValueError):
        quantity = 1

    if quantity <= 0:
        message = "Quantity must be at least 1."
        if _is_ajax_request(request):
            return JsonResponse({"success": False, "message": message}, status=400)
        messages.error(request, message)
        return redirect(_safe_next_url(request, "part_search"))

    cart = _get_cart(request)
    stock_id_str = str(stock_id)
    current_qty = cart.get(stock_id_str, 0)

    if current_qty + quantity > stock.quantity:
        message = f"Only {stock.quantity} units available in stock."
        if _is_ajax_request(request):
            return JsonResponse({"success": False, "message": message}, status=400)
        messages.error(request, message)
        return redirect(_safe_next_url(request, "part_search"))

    cart[stock_id_str] = current_qty + quantity
    request.session["cart"] = cart
    request.session.modified = True

    if _is_ajax_request(request):
        return JsonResponse(
            {
                "success": True,
                "message": f"Added {stock.part.name}.",
                "total_items": sum(cart.values()),
            }
        )

    messages.success(request, f"Added {stock.part.name}.")
    return redirect(_safe_next_url(request, "part_search"))


@login_required
@require_POST
def update_cart_item(request, stock_id):
    cart = _get_cart(request)
    stock_id_str = str(stock_id)

    try:
        quantity = int(request.POST.get("quantity", 0))
    except (TypeError, ValueError):
        quantity = 0

    stock = Stock.objects.select_related("part", "branch").filter(id=stock_id).first()
    if not stock or not _user_has_branch_access(request.user, stock.branch):
        cart.pop(stock_id_str, None)
        request.session["cart"] = cart
        request.session.modified = True
        messages.error(request, "Item not found or not accessible.")
        return redirect(_safe_next_url(request, "cart_view"))

    if quantity <= 0:
        cart.pop(stock_id_str, None)
    elif quantity > stock.quantity:
        cart[stock_id_str] = stock.quantity
        messages.warning(request, f"Adjusted to available quantity ({stock.quantity}).")
    else:
        cart[stock_id_str] = quantity

    request.session["cart"] = cart
    request.session.modified = True
    return redirect(_safe_next_url(request, "cart_view"))


@login_required
@require_POST
def clear_cart(request):
    request.session["cart"] = {}
    request.session.modified = True
    messages.info(request, "Cart cleared.")
    return redirect("pos_console")


@login_required
def cart_view(request):
    _warn_if_branch_unassigned(request)

    cart_items, subtotal, estimated_profit, _total_qty = _build_cart_items(request)

    return render(
        request,
        "inventory/pos_checkout.html",
        {
            "cart_items": cart_items,
            "subtotal": subtotal,
            "estimated_profit": estimated_profit,
            "vat_rate": VAT_RATE,
            "is_manager": is_manager(request.user),
        },
    )


@login_required
@require_POST
def finalize_order(request):
    cart = _get_cart(request)
    if not cart:
        messages.error(request, "Cart is empty.")
        return redirect("pos_console")

    stock_ids = [int(stock_id) for stock_id in cart.keys()]

    with transaction.atomic():
        stocks = list(
            _stock_scope_for_user(request.user)
            .select_for_update()
            .filter(id__in=stock_ids)
            .order_by("id")
        )
        stock_map = {str(stock.id): stock for stock in stocks}

        missing_or_forbidden = [stock_id for stock_id in cart.keys() if stock_id not in stock_map]
        if missing_or_forbidden:
            messages.error(request, "Some cart items are no longer available for your account.")
            request.session["cart"] = {k: v for k, v in cart.items() if k in stock_map}
            request.session.modified = True
            return redirect("cart_view")

        branch_ids = {stock.branch_id for stock in stocks}
        if len(branch_ids) > 1:
            messages.error(request, "Checkout must contain items from one branch only.")
            return redirect("cart_view")

        insufficient_items = []
        subtotal = Decimal("0.00")
        for stock_id, qty in cart.items():
            stock = stock_map[stock_id]
            if stock.quantity < qty:
                insufficient_items.append(f"{stock.part.name} ({stock.quantity} left)")
            subtotal += stock.part.selling_price * qty

        if insufficient_items:
            messages.error(
                request,
                "Not enough stock for: " + ", ".join(insufficient_items),
            )
            return redirect("cart_view")

        discount = _parse_decimal(request.POST.get("discount"), Decimal("0.00"))
        if discount < 0:
            discount = Decimal("0.00")
        if discount > subtotal:
            discount = subtotal

        taxable = subtotal - discount
        vat_amount = _quantize_money(taxable * VAT_RATE)
        grand_total = _quantize_money(taxable + vat_amount)

        customer_phone = (request.POST.get("phone_number") or "").strip()
        customer_name = (request.POST.get("customer_name") or "").strip()
        customer_car = (request.POST.get("customer_car") or "").strip()
        customer_obj = None

        if customer_phone:
            customer_obj, _ = Customer.objects.get_or_create(
                phone_number=customer_phone,
                defaults={
                    "name": customer_name or "Walk-in Customer",
                    "car_model": customer_car,
                },
            )
            customer_updates = []
            if customer_name and customer_obj.name != customer_name:
                customer_obj.name = customer_name
                customer_updates.append("name")
            if customer_car and customer_obj.car_model != customer_car:
                customer_obj.car_model = customer_car
                customer_updates.append("car_model")
            if customer_updates:
                customer_obj.save(update_fields=customer_updates)

        order = Order.objects.create(
            seller=request.user,
            branch=stocks[0].branch,
            customer=customer_obj,
            subtotal=_quantize_money(subtotal),
            vat_amount=vat_amount,
            discount_amount=_quantize_money(discount),
            grand_total=grand_total,
        )

        sale_rows = []
        for stock_id, qty in cart.items():
            stock = stock_map[stock_id]
            stock.quantity -= qty
            stock.save(update_fields=["quantity"])

            sale_rows.append(
                Sale(
                    order=order,
                    part=stock.part,
                    branch=stock.branch,
                    seller=request.user,
                    quantity=qty,
                    price_at_sale=stock.part.selling_price,
                    cost_at_sale=stock.part.cost_price,
                )
            )

        Sale.objects.bulk_create(sale_rows)

    request.session["cart"] = {}
    request.session.modified = True
    messages.success(request, f"Sale completed. Order {order.order_id} created.")
    return redirect("receipt_view", order_id=order.order_id)


@login_required
def receipt_view(request, order_id):
    order = get_object_or_404(
        Order.objects.select_related("seller", "branch", "customer").prefetch_related("items__part"),
        order_id=order_id,
    )
    if not _user_has_branch_access(request.user, order.branch):
        return HttpResponseForbidden("You do not have access to this receipt.")

    return render(request, "inventory/receipt.html", {"order": order})


@login_required
def order_list(request):
    orders = Order.objects.select_related("seller", "branch", "customer").prefetch_related("items").order_by("-created_at")
    orders = _scope_branch(orders, request.user, field_name="branch")

    page_obj = Paginator(orders, 25).get_page(request.GET.get("page"))
    return render(request, "inventory/order_list.html", {"orders": page_obj, "page_obj": page_obj})


@login_required
def scanner_view(request):
    return render(request, "inventory/scanner.html")


@login_required
def sell_part(request, stock_id):
    if request.method == "POST":
        return add_to_cart(request, stock_id=stock_id)
    return redirect("pos_console")


def _sales_queryset_with_filters(request):
    sales = Sale.objects.select_related("order", "part", "branch", "seller").order_by("-date_sold")
    sales = _scope_branch(sales, request.user, field_name="branch")

    start_date = request.GET.get("start_date")
    end_date = request.GET.get("end_date")
    branch_id = request.GET.get("branch")

    if start_date:
        sales = sales.filter(date_sold__date__gte=start_date)
    if end_date:
        sales = sales.filter(date_sold__date__lte=end_date)
    if branch_id:
        sales = sales.filter(branch_id=branch_id)

    return sales, start_date, end_date, branch_id


def _sales_aggregates(sales_queryset):
    revenue_expr = Case(
        When(
            is_refunded=True,
            then=Value(Decimal("0.00")),
        ),
        default=ExpressionWrapper(
            F("price_at_sale") * F("quantity"),
            output_field=DecimalField(max_digits=16, decimal_places=2),
        ),
        output_field=DecimalField(max_digits=16, decimal_places=2),
    )
    profit_expr = Case(
        When(
            is_refunded=True,
            then=Value(Decimal("0.00")),
        ),
        default=ExpressionWrapper(
            (F("price_at_sale") - F("cost_at_sale")) * F("quantity"),
            output_field=DecimalField(max_digits=16, decimal_places=2),
        ),
        output_field=DecimalField(max_digits=16, decimal_places=2),
    )
    return sales_queryset.aggregate(
        total_revenue=Coalesce(Sum(revenue_expr), Value(Decimal("0.00"))),
        total_profit=Coalesce(Sum(profit_expr), Value(Decimal("0.00"))),
    )


@login_required
@manager_required
@require_POST
def refund_sale(request, sale_id: int):
    scoped_sales = _scope_branch(
        Sale.objects.select_related("part", "branch", "order"),
        request.user,
        field_name="branch",
    )
    sale = get_object_or_404(scoped_sales, id=sale_id)

    with transaction.atomic():
        locked_sale = (
            _scope_branch(
                Sale.objects.select_for_update().select_related("part", "branch", "order"),
                request.user,
                field_name="branch",
            )
            .filter(id=sale_id)
            .first()
        )
        if not locked_sale:
            return HttpResponseForbidden("You do not have access to this sale.")

        if locked_sale.is_refunded:
            messages.info(request, "Sale is already refunded.")
            return redirect(_safe_next_url(request, "sales_history"))

        locked_sale.is_refunded = True
        locked_sale.save(update_fields=["is_refunded"])

        if locked_sale.part_id and locked_sale.branch_id:
            stock, _ = Stock.objects.select_for_update().get_or_create(
                part_id=locked_sale.part_id,
                branch_id=locked_sale.branch_id,
                defaults={"quantity": 0},
            )
            stock.quantity += locked_sale.quantity
            stock.save(update_fields=["quantity"])

    messages.success(request, f"Sale #{sale_id} refunded successfully.")
    return redirect(_safe_next_url(request, "sales_history"))


@login_required
@manager_required
def sales_history(request):
    sales_query, start_date, end_date, current_branch = _sales_queryset_with_filters(request)
    totals = _sales_aggregates(sales_query)

    page_obj = Paginator(sales_query, 50).get_page(request.GET.get("page"))
    branches = _accessible_branches(request.user)

    context = {
        "sales": page_obj.object_list,
        "page_obj": page_obj,
        "total_revenue": totals["total_revenue"],
        "total_profit": totals["total_profit"],
        "branches": branches,
        "start_date": start_date,
        "end_date": end_date,
        "current_branch": int(current_branch) if current_branch and current_branch.isdigit() else None,
    }
    return render(request, "inventory/sales_history.html", context)


def _sanitize_csv_value(value):
    text = "" if value is None else str(value)
    if text and text[0] in CSV_FORMULA_PREFIXES:
        return "'" + text
    return text


class _Echo:
    def write(self, value):
        return value


def _sales_csv_rows(sales_queryset):
    writer = csv.writer(_Echo())
    yield "\ufeff"
    yield writer.writerow(
        [
            "Order ID",
            "Part Name",
            "Part Number",
            "Branch",
            "Seller",
            "Quantity",
            "Sale Price",
            "Cost",
            "Profit",
            "Date Sold",
        ]
    )

    for sale in sales_queryset.iterator(chunk_size=500):
        row = [
            sale.order.order_id if sale.order else "-",
            sale.part.name if sale.part else "-",
            sale.part.part_number if sale.part else "-",
            sale.branch.name if sale.branch else "-",
            sale.seller.username if sale.seller else "-",
            sale.quantity,
            sale.price_at_sale,
            sale.cost_at_sale,
            sale.total_profit,
            timezone.localtime(sale.date_sold).strftime("%Y-%m-%d %H:%M"),
        ]
        yield writer.writerow([_sanitize_csv_value(col) for col in row])


def _export_sales_xlsx_response(sales_queryset, filename_stem: str):
    try:
        from openpyxl import Workbook
    except ImportError:
        return None

    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "Sales"
    headers = [
        "Order ID",
        "Part Name",
        "Part Number",
        "Branch",
        "Seller",
        "Quantity",
        "Sale Price",
        "Cost",
        "Profit",
        "Date Sold",
    ]
    sheet.append(headers)

    for sale in sales_queryset.iterator(chunk_size=500):
        sheet.append(
            [
                sale.order.order_id if sale.order else "-",
                sale.part.name if sale.part else "-",
                sale.part.part_number if sale.part else "-",
                sale.branch.name if sale.branch else "-",
                sale.seller.username if sale.seller else "-",
                sale.quantity,
                float(sale.price_at_sale),
                float(sale.cost_at_sale),
                float(sale.total_profit),
                timezone.localtime(sale.date_sold).strftime("%Y-%m-%d %H:%M"),
            ]
        )

    response = HttpResponse(
        content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    )
    response["Content-Disposition"] = f'attachment; filename="{filename_stem}.xlsx"'
    response["X-Content-Type-Options"] = "nosniff"
    workbook.save(response)
    return response


@login_required
@manager_required
def export_sales_csv(request):
    sales_queryset, _, _, _ = _sales_queryset_with_filters(request)
    filename_stem = f"sales_{timezone.localdate()}"

    if request.GET.get("format", "").lower() == "xlsx":
        xlsx_response = _export_sales_xlsx_response(sales_queryset, filename_stem)
        if xlsx_response is not None:
            return xlsx_response
        messages.warning(request, "XLSX export requires openpyxl. Falling back to CSV.")

    response = StreamingHttpResponse(
        _sales_csv_rows(sales_queryset),
        content_type="text/csv; charset=utf-8",
    )
    response["Content-Disposition"] = f'attachment; filename="{filename_stem}.csv"'
    response["X-Content-Type-Options"] = "nosniff"
    response["Cache-Control"] = "no-store"
    return response


@login_required
@manager_required
def reports_dashboard(request):
    sales_queryset, start_date, end_date, current_branch = _sales_queryset_with_filters(request)

    if not start_date and not end_date:
        seven_days_ago = timezone.localdate() - timedelta(days=6)
        sales_queryset = sales_queryset.filter(date_sold__date__gte=seven_days_ago)

    totals = _sales_aggregates(sales_queryset)

    revenue_expr = Case(
        When(
            is_refunded=True,
            then=Value(Decimal("0.00")),
        ),
        default=ExpressionWrapper(
            F("price_at_sale") * F("quantity"),
            output_field=DecimalField(max_digits=16, decimal_places=2),
        ),
        output_field=DecimalField(max_digits=16, decimal_places=2),
    )

    daily_stats = (
        sales_queryset.annotate(day=TruncDate("date_sold"))
        .values("day")
        .annotate(total=Coalesce(Sum(revenue_expr), Value(Decimal("0.00"))))
        .order_by("day")
    )

    top_sellers = (
        sales_queryset.values("seller__username")
        .annotate(
            total_sales=Count("id"),
            total_qty=Coalesce(Sum("quantity"), Value(0)),
            revenue=Coalesce(Sum(revenue_expr), Value(Decimal("0.00"))),
        )
        .order_by("-revenue")[:5]
    )

    return render(
        request,
        "inventory/reports_dashboard.html",
        {
            "total_revenue": totals["total_revenue"],
            "total_profit": totals["total_profit"],
            "daily_stats": daily_stats,
            "top_sellers": top_sellers,
            "branches": _accessible_branches(request.user),
            "start_date": start_date,
            "end_date": end_date,
            "current_branch": int(current_branch) if current_branch and current_branch.isdigit() else None,
        },
    )


@login_required
@manager_required
def low_stock_list(request):
    low_stock_items = _scope_branch(
        Stock.objects.select_related("part", "branch").filter(quantity__lte=5).order_by("quantity", "part__name"),
        request.user,
        field_name="branch",
    )
    page_obj = Paginator(low_stock_items, 30).get_page(request.GET.get("page"))
    return render(request, "inventory/low_stock.html", {"low_stock_items": page_obj, "page_obj": page_obj})


@login_required
@require_GET
def vehicle_catalog(request):
    vehicles = Vehicle.objects.all().order_by("make", "model", "year")
    grouped_vehicles = {}
    for vehicle in vehicles:
        grouped_vehicles.setdefault(vehicle.model, []).append(vehicle)

    return render(request, "inventory/vehicle_catalog.html", {"grouped_vehicles": grouped_vehicles})

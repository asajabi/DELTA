import csv
import json
import logging
import re
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from functools import wraps
from urllib.parse import urlencode

from django.conf import settings
from django.contrib import messages
from django.contrib.auth import password_validation
from django.contrib.auth.decorators import login_required, user_passes_test
from django.contrib.auth.models import User
from django.core.cache import cache
from django.core.exceptions import ValidationError
from django.core.mail import send_mail
from django.core.paginator import Paginator
from django.db import IntegrityError, transaction
from django.db.models import Case, Count, DecimalField, ExpressionWrapper, F, IntegerField, Prefetch, Q, Sum, Value, When
from django.db.models.functions import Coalesce, TruncDate
from django.http import HttpResponse, HttpResponseBadRequest, HttpResponseForbidden, JsonResponse, StreamingHttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.http import url_has_allowed_host_and_scheme
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_GET, require_POST

from .assistant_llm import (
    AssistantLLMError,
    assistant_llm_enabled,
    assistant_llm_engine_label,
    generate_assistant_plan,
)
from .audit import log_audit_event
from .chat_assistant import (
    WRITE_ACTIONS,
    add_stock,
    create_transfer_request,
    find_part_candidates,
    is_cancel_message,
    is_confirm_message,
    lookup_stock,
    move_stock,
    parse_chat_message,
    remove_stock,
    resolve_branch_context,
    resolve_location,
    user_role,
    validate_tool_permission,
)
from .invoicing import (
    amount_to_words_ar,
    build_invoice_template_context,
    create_posted_invoice_from_order,
    render_invoice_pdf_bytes,
)
from .models import (
    AuditLog,
    Branch,
    CreditNote,
    CreditNoteLine,
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
    PasswordResetOtp,
    PurchaseOrder,
    PurchaseOrderLine,
    PurchaseReceipt,
    PurchaseReceiptLine,
    Sale,
    SmaccSyncLog,
    SmaccSyncQueue,
    Stock,
    StockLocation,
    StockMovement,
    TaxInvoice,
    Ticket,
    TransferRequest,
    UserProfile,
    Vendor,
    Vehicle,
    add_stock_to_location,
    ensure_stock_locations_seeded_from_branch_stock,
    move_stock_between_locations,
    remove_stock_from_locations,
    sync_stock_total_from_locations,
    update_branch_average_cost,
)
from .smacc_client import SmaccClient

VAT_RATE = Decimal("0.15")
CSV_FORMULA_PREFIXES = ("=", "+", "-", "@", "\t", "\r")
ACTIVE_BRANCH_SESSION_KEY = "active_branch_id"
_ACTIVE_BRANCH_CACHE_ATTR = "_inventory_active_branch_cache"
_ACTIVE_BRANCH_UNSET = object()
SCAN_BATCH_SESSION_KEY = "scan_batch_lines"
SCAN_BATCH_UNDO_SESSION_KEY = "scan_batch_undo"
SCAN_REPEAT_GUARD_SESSION_KEY = "scan_repeat_guard"
POS_SCAN_UNDO_SESSION_KEY = "pos_scan_undo"
POS_SCAN_REPEAT_GUARD_SESSION_KEY = "pos_scan_repeat_guard"
SCAN_REPEAT_WINDOW_SECONDS = 3
SMACC_SYNC_EVENT_TYPES = {"document.created", "document.updated", "accountingRecord.updated"}
PASSWORD_RESET_OTP_SESSION_KEY = "password_reset_otp_id"
PASSWORD_RESET_VERIFIED_SESSION_KEY = "password_reset_verified_otp_id"
PASSWORD_RESET_REQUEST_LIMIT_PER_HOUR = 5
PASSWORD_RESET_MAX_VERIFY_ATTEMPTS = 5

logger = logging.getLogger("inventory")


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


def _request_ip(request) -> str | None:
    forwarded_for = (request.META.get("HTTP_X_FORWARDED_FOR") or "").strip()
    if forwarded_for:
        return forwarded_for.split(",")[0].strip()
    remote_addr = (request.META.get("REMOTE_ADDR") or "").strip()
    return remote_addr or None


def _normalize_phone(value: str) -> str:
    digits = re.sub(r"\D", "", value or "")
    if digits.startswith("966") and len(digits) >= 12:
        digits = "0" + digits[3:]
    return digits


def _phone_variants(value: str) -> set[str]:
    normalized = _normalize_phone(value)
    if not normalized:
        return set()
    variants = {normalized}
    variants.add(normalized.lstrip("0"))
    if normalized.startswith("0"):
        variants.add(f"966{normalized[1:]}")
    return {v for v in variants if v}


def _mask_contact(channel: str, contact: str) -> str:
    if channel == PasswordResetOtp.Channels.EMAIL:
        local, _, domain = (contact or "").partition("@")
        if not domain:
            return "***"
        masked_local = f"{local[:2]}***" if local else "***"
        return f"{masked_local}@{domain}"
    phone = _normalize_phone(contact)
    if len(phone) <= 4:
        return "***"
    return f"{phone[:3]}***{phone[-2:]}"


def _matches_recovery_contact(user: User, channel: str, contact: str) -> bool:
    if channel == PasswordResetOtp.Channels.EMAIL:
        user_email = (user.email or "").strip().casefold()
        contact_email = (contact or "").strip().casefold()
        return bool(user_email and contact_email and user_email == contact_email)

    if channel == PasswordResetOtp.Channels.PHONE:
        profile = getattr(user, "profile", None)
        profile_phone = (getattr(profile, "phone_number", "") or "").strip()
        if not profile_phone:
            return False
        return bool(_phone_variants(profile_phone).intersection(_phone_variants(contact)))

    return False


def _dispatch_password_reset_otp(otp: PasswordResetOtp, raw_code: str) -> tuple[bool, str | None]:
    if otp.channel == PasswordResetOtp.Channels.EMAIL:
        subject = "DELTA POS - Password Reset Code"
        body = (
            f"Your DELTA POS password reset code is: {raw_code}\n\n"
            "This code expires in 15 minutes.\n"
            "If you did not request this, contact your administrator."
        )
        sender = getattr(settings, "DEFAULT_FROM_EMAIL", "no-reply@delta.local")
        try:
            send_mail(subject, body, sender, [otp.destination], fail_silently=False)
            return True, None
        except Exception:
            logger.exception("Password reset email send failed for user=%s", otp.user_id)
            if settings.DEBUG:
                logger.warning("Password reset email fallback used (DEBUG). user=%s code=%s", otp.user_id, raw_code)
                return True, raw_code
            return False, "تعذر إرسال الرمز إلى البريد الإلكتروني. تحقق من إعدادات البريد ثم أعد المحاولة."

    if otp.channel == PasswordResetOtp.Channels.PHONE:
        logger.info("Password reset OTP (SMS fallback) user=%s phone=%s code=%s", otp.user_id, otp.destination, raw_code)
        if settings.DEBUG:
            return True, raw_code
        return True, None

    return False, "قناة الإرسال غير مدعومة."


def password_reset_request(request):
    context = {
        "username": "",
        "contact": "",
        "channel": PasswordResetOtp.Channels.EMAIL,
    }
    if request.method == "POST":
        username = (request.POST.get("username") or "").strip()
        contact = (request.POST.get("contact") or "").strip()
        channel = (request.POST.get("channel") or PasswordResetOtp.Channels.EMAIL).strip().lower()
        context.update({"username": username, "contact": contact, "channel": channel})

        if channel not in {PasswordResetOtp.Channels.EMAIL, PasswordResetOtp.Channels.PHONE}:
            messages.error(request, "اختر وسيلة صحيحة لاستلام الرمز.")
            return render(request, "registration/password_reset_request.html", context)

        if not username or not contact:
            messages.error(request, "أدخل اسم المستخدم ووسيلة التواصل.")
            return render(request, "registration/password_reset_request.html", context)

        user = User.objects.filter(username__iexact=username).first()
        if not user or not _matches_recovery_contact(user, channel, contact):
            messages.error(request, "بيانات الاسترجاع غير صحيحة.")
            return render(request, "registration/password_reset_request.html", context)

        rate_key = f"pwd_reset_req:{user.id}:{_request_ip(request) or 'unknown'}"
        request_count = int(cache.get(rate_key, 0))
        if request_count >= PASSWORD_RESET_REQUEST_LIMIT_PER_HOUR:
            messages.error(request, "تم تجاوز عدد المحاولات. أعد المحاولة بعد ساعة.")
            return render(request, "registration/password_reset_request.html", context)

        with transaction.atomic():
            otp, raw_code = PasswordResetOtp.issue_code(
                user=user,
                channel=channel,
                destination=contact,
                request_ip=_request_ip(request),
            )
        sent, dispatch_payload = _dispatch_password_reset_otp(otp, raw_code)
        if not sent:
            otp.used_at = timezone.now()
            otp.save(update_fields=["used_at"])
            messages.error(request, dispatch_payload or "تعذر إرسال رمز التحقق. أعد المحاولة.")
            return render(request, "registration/password_reset_request.html", context)

        cache.set(rate_key, request_count + 1, timeout=3600)
        request.session[PASSWORD_RESET_OTP_SESSION_KEY] = otp.id
        request.session.pop(PASSWORD_RESET_VERIFIED_SESSION_KEY, None)
        request.session.modified = True
        if settings.DEBUG and dispatch_payload and channel == PasswordResetOtp.Channels.EMAIL:
            messages.warning(request, "وضع التطوير: لم يتم إرسال بريد فعلي لأن SMTP غير مُعد. استخدم الرمز الظاهر أدناه.")
        else:
            messages.success(request, f"تم إرسال رمز تحقق إلى {_mask_contact(channel, contact)}.")
        if settings.DEBUG and dispatch_payload:
            messages.info(request, f"رمز التحقق (وضع التطوير): {dispatch_payload}")
        return redirect("password_reset_verify")

    return render(request, "registration/password_reset_request.html", context)


def password_reset_verify(request):
    otp_id = request.session.get(PASSWORD_RESET_OTP_SESSION_KEY)
    otp = PasswordResetOtp.objects.select_related("user").filter(id=otp_id).first()
    if not otp:
        messages.error(request, "انتهت جلسة الاسترجاع. أعد طلب رمز جديد.")
        return redirect("password_reset_request")

    if otp.used_at is not None:
        messages.error(request, "رمز التحقق لم يعد صالحًا. اطلب رمزًا جديدًا.")
        return redirect("password_reset_request")

    if otp.verified_at is not None:
        request.session[PASSWORD_RESET_VERIFIED_SESSION_KEY] = otp.id
        request.session.modified = True
        return redirect("password_reset_set_new")

    if otp.is_expired():
        otp.used_at = timezone.now()
        otp.save(update_fields=["used_at"])
        messages.error(request, "انتهت صلاحية الرمز (15 دقيقة). اطلب رمزًا جديدًا.")
        return redirect("password_reset_request")

    if request.method == "POST":
        code = (request.POST.get("code") or "").strip()
        if not re.fullmatch(r"\d{6}", code):
            messages.error(request, "أدخل رمزًا صحيحًا مكوّنًا من 6 أرقام.")
        else:
            if otp.verify_code(code):
                otp.verified_at = timezone.now()
                otp.save(update_fields=["verified_at"])
                request.session[PASSWORD_RESET_VERIFIED_SESSION_KEY] = otp.id
                request.session.modified = True
                messages.success(request, "تم التحقق من الرمز. أدخل كلمة المرور الجديدة.")
                return redirect("password_reset_set_new")

            otp.attempts = otp.attempts + 1
            update_fields = ["attempts"]
            if otp.attempts >= PASSWORD_RESET_MAX_VERIFY_ATTEMPTS:
                otp.used_at = timezone.now()
                update_fields.append("used_at")
            otp.save(update_fields=update_fields)
            if otp.used_at:
                messages.error(request, "تم تجاوز عدد محاولات التحقق. اطلب رمزًا جديدًا.")
                return redirect("password_reset_request")
            messages.error(request, "رمز التحقق غير صحيح.")

    remaining_seconds = max(0, int((otp.expires_at - timezone.now()).total_seconds()))
    context = {
        "masked_contact": _mask_contact(otp.channel, otp.destination),
        "channel": otp.channel,
        "minutes_left": max(1, remaining_seconds // 60),
    }
    return render(request, "registration/password_reset_verify.html", context)


def password_reset_set_new(request):
    verified_otp_id = request.session.get(PASSWORD_RESET_VERIFIED_SESSION_KEY)
    otp = PasswordResetOtp.objects.select_related("user").filter(id=verified_otp_id).first()
    if not otp or otp.used_at is not None or otp.verified_at is None or otp.is_expired():
        messages.error(request, "جلسة تعيين كلمة المرور انتهت. ابدأ من جديد.")
        return redirect("password_reset_request")

    if request.method == "POST":
        password1 = (request.POST.get("new_password1") or "").strip()
        password2 = (request.POST.get("new_password2") or "").strip()

        if not password1 or not password2:
            messages.error(request, "أدخل كلمة المرور الجديدة وتأكيدها.")
        elif password1 != password2:
            messages.error(request, "كلمتا المرور غير متطابقتين.")
        else:
            try:
                password_validation.validate_password(password1, user=otp.user)
            except ValidationError as exc:
                for err in exc.messages:
                    messages.error(request, err)
            else:
                with transaction.atomic():
                    otp.user.set_password(password1)
                    otp.user.save(update_fields=["password"])
                    otp.used_at = timezone.now()
                    otp.save(update_fields=["used_at"])
                request.session.pop(PASSWORD_RESET_OTP_SESSION_KEY, None)
                request.session.pop(PASSWORD_RESET_VERIFIED_SESSION_KEY, None)
                request.session.modified = True
                messages.success(request, "تم تغيير كلمة المرور بنجاح. يمكنك تسجيل الدخول الآن.")
                return redirect("login")

    return render(
        request,
        "registration/password_reset_new.html",
        {"username": otp.user.username},
    )


def _active_branch_for_request(request) -> Branch | None:
    cached = getattr(request, _ACTIVE_BRANCH_CACHE_ATTR, _ACTIVE_BRANCH_UNSET)
    if cached is not _ACTIVE_BRANCH_UNSET:
        return cached

    profile = _get_or_create_profile(request.user)
    if is_admin_user(request.user):
        branch_raw = request.session.get(ACTIVE_BRANCH_SESSION_KEY)
        try:
            branch_id = int(branch_raw)
        except (TypeError, ValueError):
            branch_id = None
        branch = Branch.objects.filter(id=branch_id).first() if branch_id else None
    else:
        branch = profile.branch

    setattr(request, _ACTIVE_BRANCH_CACHE_ATTR, branch)
    return branch


def active_branch_required(view_func):
    @wraps(view_func)
    def _wrapped(request, *args, **kwargs):
        branch = _active_branch_for_request(request)
        if branch is None:
            messages.error(request, "يجب اختيار الفرع النشط أولاً قبل تنفيذ العملية.")
            return redirect(_safe_next_url(request, "part_search"))
        request.active_branch = branch
        return view_func(request, *args, **kwargs)

    return _wrapped


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


def is_admin_user(user) -> bool:
    if not user.is_authenticated:
        return False
    if user.is_superuser:
        return True
    profile = _get_or_create_profile(user)
    return profile.role == UserProfile.Roles.ADMIN or user.groups.filter(name="admin").exists()


admin_required = user_passes_test(is_admin_user)


def is_tech_user(user) -> bool:
    if not user.is_authenticated:
        return False
    if user.is_superuser or user.username.strip().casefold() == "abdullah":
        return True
    profile = _get_or_create_profile(user)
    return bool(
        profile.role == UserProfile.Roles.TECH
        or user.groups.filter(name__in=["tech", "support"]).exists()
    )


def _can_use_pos(user) -> bool:
    if not user.is_authenticated:
        return False
    if is_admin_user(user):
        return True
    profile = _get_or_create_profile(user)
    return profile.role in {UserProfile.Roles.CASHIER, UserProfile.Roles.MANAGER}


def _has_explicit_model_acl(user, model_cls) -> bool:
    perms = user.get_all_permissions()
    app_label = model_cls._meta.app_label
    model_name = model_cls._meta.model_name
    relevant = {
        f"{app_label}.view_{model_name}",
        f"{app_label}.add_{model_name}",
        f"{app_label}.change_{model_name}",
        f"{app_label}.delete_{model_name}",
    }
    return bool(perms.intersection(relevant))


def _has_model_write_acl(user, model_cls) -> bool:
    app_label = model_cls._meta.app_label
    model_name = model_cls._meta.model_name
    return any(
        user.has_perm(f"{app_label}.{verb}_{model_name}")
        for verb in ("add", "change", "delete")
    )


def _blocked_by_view_only_acl(user, model_cls) -> bool:
    if not user.is_authenticated or user.is_superuser:
        return False
    if not _has_explicit_model_acl(user, model_cls):
        return False
    return not _has_model_write_acl(user, model_cls)


def _default_ticket_assignee():
    return User.objects.filter(username__iexact="abdullah").first()


def _user_branch(user):
    profile = _get_or_create_profile(user)
    return profile.branch


def _audit_branch_for_user(user, fallback: Branch | None = None) -> Branch | None:
    branch = _user_branch(user)
    return branch or fallback


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


def _assistant_or_manager_branch_scope(user, requested_branch_id: str | None) -> Branch | None:
    if is_admin_user(user):
        if requested_branch_id and requested_branch_id.isdigit():
            return Branch.objects.filter(id=int(requested_branch_id)).first()
        return None
    return _user_branch(user)


def _redirect_with_branch(base_name: str, branch_id: int | None) -> str:
    url = reverse(base_name)
    if branch_id:
        return f"{url}?branch={branch_id}"
    return url


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


def _reserved_quantity_for_part_branch(part_id: int, branch_id: int, *, exclude_transfer_id: int | None = None) -> int:
    active_statuses = [
        TransferRequest.Status.APPROVED,
        TransferRequest.Status.PICKED_UP,
        TransferRequest.Status.DELIVERED,
    ]
    qs = TransferRequest.objects.filter(
        part_id=part_id,
        source_branch_id=branch_id,
        status__in=active_statuses,
    )
    if exclude_transfer_id is not None:
        qs = qs.exclude(id=exclude_transfer_id)

    total = qs.aggregate(total=Coalesce(Sum("reserved_quantity"), Value(0)))["total"]
    return int(total or 0)


def _available_stock_quantity(stock: Stock, *, exclude_transfer_id: int | None = None) -> int:
    location_agg = StockLocation.objects.filter(part_id=stock.part_id, branch_id=stock.branch_id).aggregate(
        rows=Count("id"),
        total=Coalesce(Sum("quantity"), Value(0)),
    )
    if int(location_agg.get("rows") or 0) > 0:
        on_hand = int(location_agg.get("total") or 0)
    else:
        on_hand = int(stock.quantity or 0)

    reserved = _reserved_quantity_for_part_branch(
        part_id=stock.part_id,
        branch_id=stock.branch_id,
        exclude_transfer_id=exclude_transfer_id,
    )
    return max(on_hand - reserved, 0)


def _reserved_quantity_map_for_stocks(stock_qs):
    pairs = list(stock_qs.values_list("part_id", "branch_id"))
    if not pairs:
        return {}

    part_ids = {part_id for part_id, _ in pairs}
    branch_ids = {branch_id for _, branch_id in pairs}
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
    return {
        (row["part_id"], row["source_branch_id"]): int(row["total_reserved"] or 0)
        for row in reserved_rows
    }


def _transfer_scope_for_user(queryset, user):
    if is_admin_user(user):
        return queryset

    branch = _user_branch(user)
    if not branch:
        return queryset.none()

    return queryset.filter(Q(source_branch=branch) | Q(destination_branch=branch))


def _can_approve_transfer(user, transfer: TransferRequest) -> bool:
    if is_admin_user(user):
        return True
    profile = _get_or_create_profile(user)
    if profile.role != UserProfile.Roles.MANAGER:
        return False
    return bool(profile.branch and profile.branch_id == transfer.source_branch_id)


def _can_request_transfer(user) -> bool:
    if not user.is_authenticated:
        return False
    if is_admin_user(user):
        return True
    profile = _get_or_create_profile(user)
    return profile.role in {UserProfile.Roles.CASHIER, UserProfile.Roles.MANAGER}


def _build_cart_items(request, *, active_branch: Branch | None = None):
    cart = _get_cart(request)
    if not cart:
        return [], Decimal("0.00"), Decimal("0.00"), 0

    stocks = _stock_scope_for_user(request.user)
    if active_branch is not None:
        stocks = stocks.filter(branch=active_branch)
    elif is_admin_user(request.user):
        stocks = stocks.none()
    stocks = stocks.filter(id__in=[int(stock_id) for stock_id in cart.keys()])
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

    active_branch = _active_branch_for_request(request)
    vehicles = Vehicle.objects.all().order_by("make", "model", "year")
    query_text = (request.GET.get("q") or "").strip()
    global_query_text = (request.GET.get("global_q") or "").strip()
    active_query_text = global_query_text or query_text
    search_mode = "global" if global_query_text else "branch"
    query_vehicle_id = (request.GET.get("vehicle") or "").strip()
    vehicle_filter = None
    if query_vehicle_id:
        if query_vehicle_id.isdigit():
            vehicle_filter = Q(part__compatible_vehicles__id=int(query_vehicle_id))
        else:
            # Accept text input safely (e.g. "patrol") instead of crashing on numeric-id coercion.
            vehicle_filter = (
                Q(part__compatible_vehicles__make__icontains=query_vehicle_id)
                | Q(part__compatible_vehicles__model__icontains=query_vehicle_id)
            )
    stock_scope = _stock_scope_for_user(request.user).filter(quantity__gt=0)
    if search_mode == "branch":
        if active_branch is not None:
            stock_scope = stock_scope.filter(branch=active_branch)
        elif is_admin_user(request.user):
            stock_scope = stock_scope.none()

    ajax_request = _is_ajax_request(request)
    part_page = None
    if active_query_text or query_vehicle_id:
        stock_filter = Q()
        if vehicle_filter is not None:
            stock_filter &= vehicle_filter
        if active_query_text:
            # Suggestions: keep short-query strict, allow contains for 3+ chars for practical UX.
            if ajax_request and len(active_query_text) < 3:
                stock_filter &= (
                    Q(part__part_number__istartswith=active_query_text)
                    | Q(part__name__istartswith=active_query_text)
                    | Q(part__barcode__istartswith=active_query_text)
                    | Q(part__sku__istartswith=active_query_text)
                    | Q(part__manufacturer_part_number__istartswith=active_query_text)
                    | Q(part__barcodes__barcode__istartswith=active_query_text)
                )
            # For 1-2 chars, allow only exact/prefix identifier matching to avoid noisy false positives.
            elif len(active_query_text) < 3:
                stock_filter &= (
                    Q(part__part_number__istartswith=active_query_text)
                    | Q(part__barcode__istartswith=active_query_text)
                    | Q(part__sku__istartswith=active_query_text)
                    | Q(part__manufacturer_part_number__istartswith=active_query_text)
                    | Q(part__barcodes__barcode__istartswith=active_query_text)
                )
            else:
                stock_filter &= (
                    Q(part__part_number__icontains=active_query_text)
                    | Q(part__name__icontains=active_query_text)
                    | Q(part__barcode__icontains=active_query_text)
                    | Q(part__sku__icontains=active_query_text)
                    | Q(part__manufacturer_part_number__icontains=active_query_text)
                    | Q(part__barcodes__barcode__icontains=active_query_text)
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
        if part_page:
            page_parts = list(part_page.object_list)
            part_ids = [part.id for part in page_parts]
            branch_ids = {
                stock.branch_id
                for part in page_parts
                for stock in (getattr(part, "available_stock", []) or [])
                if getattr(stock, "branch_id", None)
            }
            location_map: dict[tuple[int, int], list[str]] = {}
            location_total_map: dict[tuple[int, int], int] = {}
            if part_ids and branch_ids:
                location_rows = (
                    StockLocation.objects.select_related("location")
                    .filter(part_id__in=part_ids, branch_id__in=branch_ids)
                    .order_by("location__code")
                )
                for row in location_rows:
                    key = (row.part_id, row.branch_id)
                    label = row.location.code
                    qty = int(row.quantity or 0)
                    location_total_map[key] = location_total_map.get(key, 0) + qty
                    if qty > 0:
                        label = f"{label} ({qty})"
                        location_map.setdefault(key, []).append(label)

            for part in page_parts:
                for stock in (getattr(part, "available_stock", []) or []):
                    key = (part.id, stock.branch_id)
                    labels = location_map.get(key, [])
                    if key in location_total_map:
                        stock.display_quantity = int(location_total_map[key])
                    else:
                        stock.display_quantity = int(stock.quantity or 0)
                    if labels:
                        stock.location_summary = " | ".join(labels)
                    else:
                        stock.location_summary = (stock.location_in_warehouse or "").strip() or "-"

    cart_total_count = sum(_get_cart(request).values())

    if ajax_request:
        ajax_rows = []
        if part_page:
            for part in part_page.object_list:
                stock_rows = list(getattr(part, "available_stock", []) or [])
                if not stock_rows:
                    continue
                primary = stock_rows[0]
                ajax_rows.append(
                    {
                        "part_id": part.id,
                        "name": part.name,
                        "part_number": part.part_number,
                        "barcode": part.barcode or "",
                        "price": str(part.selling_price),
                        "branch": primary.branch.name if primary.branch else "",
                        "stock_qty": int(getattr(primary, "display_quantity", primary.quantity) or 0),
                    }
                )
                if len(ajax_rows) >= 15:
                    break
        return JsonResponse({"results": ajax_rows, "query": active_query_text, "mode": search_mode})

    return render(
        request,
        "inventory/search.html",
        {
            "vehicles": vehicles,
            "part_page": part_page,
            "results": part_page.object_list if part_page else None,
            "query": query_text,
            "global_query": global_query_text,
            "query_vehicle_id": query_vehicle_id,
            "cart_total_count": cart_total_count,
            "active_branch": active_branch,
            "search_mode": search_mode,
        },
    )


@login_required
def pos_console(request):
    _warn_if_branch_unassigned(request)
    if not _can_use_pos(request.user):
        return HttpResponseForbidden("Your role is view-only for POS operations.")

    active_branch = _active_branch_for_request(request)
    query = (request.GET.get("q") or "").strip()
    results = []
    if query:
        stock_qs = _stock_scope_for_user(request.user)
        if active_branch is not None:
            stock_qs = stock_qs.filter(branch=active_branch)
        elif is_admin_user(request.user):
            stock_qs = stock_qs.none()
        results = (
            stock_qs
            .filter(
                Q(part__part_number__icontains=query)
                | Q(part__name__icontains=query)
                | Q(part__barcode=query)
            )
            .order_by("part__name", "branch__name")[:30]
        )

    cart_items, subtotal, _estimated_profit, cart_total_count = _build_cart_items(request, active_branch=active_branch)

    return render(
        request,
        "inventory/pos_console.html",
        {
            "results": results,
            "query": query,
            "cart_items": cart_items,
            "subtotal": subtotal,
            "cart_total_count": cart_total_count,
            "active_branch": active_branch,
            "is_manager": is_manager(request.user),
        },
    )


@login_required
@require_POST
@active_branch_required
def add_to_cart(request, stock_id):
    _warn_if_branch_unassigned(request)
    if not _can_use_pos(request.user):
        return HttpResponseForbidden("Your role is not allowed to modify POS cart.")
    if _blocked_by_view_only_acl(request.user, Order):
        return HttpResponseForbidden("Your account is view-only for sales actions.")

    stock = get_object_or_404(
        _stock_scope_for_user(request.user).filter(branch=request.active_branch),
        id=stock_id,
    )

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
    available_qty = _available_stock_quantity(stock)

    if current_qty + quantity > available_qty:
        message = f"Only {available_qty} units available in stock."
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
@active_branch_required
def update_cart_item(request, stock_id):
    if not _can_use_pos(request.user):
        return HttpResponseForbidden("Your role is not allowed to modify POS cart.")
    if _blocked_by_view_only_acl(request.user, Order):
        return HttpResponseForbidden("Your account is view-only for sales actions.")
    cart = _get_cart(request)
    stock_id_str = str(stock_id)

    try:
        quantity = int(request.POST.get("quantity", 0))
    except (TypeError, ValueError):
        quantity = 0

    stock = (
        Stock.objects.select_related("part", "branch")
        .filter(id=stock_id, branch=request.active_branch)
        .first()
    )
    if not stock or not _user_has_branch_access(request.user, stock.branch):
        cart.pop(stock_id_str, None)
        request.session["cart"] = cart
        request.session.modified = True
        messages.error(request, "Item not found or not accessible.")
        return redirect(_safe_next_url(request, "cart_view"))

    available_qty = _available_stock_quantity(stock)
    if quantity <= 0:
        cart.pop(stock_id_str, None)
    elif quantity > available_qty:
        cart[stock_id_str] = available_qty
        messages.warning(request, f"Adjusted to available quantity ({available_qty}).")
    else:
        cart[stock_id_str] = quantity

    request.session["cart"] = cart
    request.session.modified = True
    return redirect(_safe_next_url(request, "cart_view"))


@login_required
@require_POST
def clear_cart(request):
    if not _can_use_pos(request.user):
        return HttpResponseForbidden("Your role is not allowed to modify POS cart.")
    if _blocked_by_view_only_acl(request.user, Order):
        return HttpResponseForbidden("Your account is view-only for sales actions.")
    request.session["cart"] = {}
    request.session.pop(POS_SCAN_UNDO_SESSION_KEY, None)
    request.session.pop(POS_SCAN_REPEAT_GUARD_SESSION_KEY, None)
    request.session.modified = True
    messages.info(request, "Cart cleared.")
    return redirect("pos_console")


@login_required
@require_POST
def set_active_branch(request):
    if not is_admin_user(request.user):
        return HttpResponseForbidden("Only admin can switch the active branch.")

    branch_raw = (request.POST.get("active_branch") or "").strip()
    if not branch_raw:
        request.session.pop(ACTIVE_BRANCH_SESSION_KEY, None)
        setattr(request, _ACTIVE_BRANCH_CACHE_ATTR, _ACTIVE_BRANCH_UNSET)
        request.session.modified = True
        messages.info(request, "تم مسح الفرع النشط. اختر فرعاً قبل تنفيذ أي عملية كتابة.")
        return redirect(_safe_next_url(request, "part_search"))

    branch = Branch.objects.filter(id=branch_raw).first()
    if not branch:
        messages.error(request, "الفرع المحدد غير موجود.")
        return redirect(_safe_next_url(request, "part_search"))

    request.session[ACTIVE_BRANCH_SESSION_KEY] = branch.id
    setattr(request, _ACTIVE_BRANCH_CACHE_ATTR, branch)
    request.session.modified = True
    messages.success(request, f"تم تعيين الفرع النشط إلى: {branch.name}")
    return redirect(_safe_next_url(request, "part_search"))


@login_required
def cart_view(request):
    _warn_if_branch_unassigned(request)

    cart_items, subtotal, estimated_profit, _total_qty = _build_cart_items(
        request,
        active_branch=_active_branch_for_request(request),
    )

    return render(
        request,
        "inventory/pos_checkout.html",
        {
            "cart_items": cart_items,
            "subtotal": subtotal,
            "estimated_profit": estimated_profit,
            "vat_rate": VAT_RATE,
            "vat_rate_percent": int(VAT_RATE * 100),
            "is_manager": is_manager(request.user),
        },
    )


@login_required
@require_POST
@active_branch_required
def finalize_order(request):
    if not _can_use_pos(request.user):
        return HttpResponseForbidden("Your role is not allowed to checkout POS sales.")
    if _blocked_by_view_only_acl(request.user, Order):
        return HttpResponseForbidden("Your account is view-only for sales actions.")
    cart = _get_cart(request)
    if not cart:
        messages.error(request, "Cart is empty.")
        return redirect("pos_console")

    stock_ids = [int(stock_id) for stock_id in cart.keys()]

    with transaction.atomic():
        stocks = list(
            _stock_scope_for_user(request.user)
            .select_for_update()
            .filter(id__in=stock_ids, branch=request.active_branch)
            .order_by("id")
        )
        stock_map = {str(stock.id): stock for stock in stocks}

        missing_or_forbidden = [stock_id for stock_id in cart.keys() if stock_id not in stock_map]
        if missing_or_forbidden:
            messages.error(request, "Some cart items are no longer available for your account.")
            request.session["cart"] = {k: v for k, v in cart.items() if k in stock_map}
            request.session.modified = True
            return redirect("cart_view")

        insufficient_items = []
        subtotal = Decimal("0.00")
        for stock_id, qty in cart.items():
            stock = stock_map[stock_id]
            available_qty = _available_stock_quantity(stock)
            if available_qty < qty:
                insufficient_items.append(f"{stock.part.name} ({available_qty} left)")
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

        payment_method = (request.POST.get("payment_method") or TaxInvoice.PaymentMethod.CASH).strip().lower()
        if payment_method not in TaxInvoice.PaymentMethod.values:
            payment_method = TaxInvoice.PaymentMethod.CASH

        customer_vat_number = (request.POST.get("customer_vat_number") or "").strip()
        total_in_words_ar = (request.POST.get("total_in_words_ar") or "").strip()
        due_date_raw = (request.POST.get("due_date") or "").strip()
        due_date = None
        if due_date_raw:
            try:
                due_date = date.fromisoformat(due_date_raw)
            except ValueError:
                due_date = None

        advance_payment = _parse_decimal(request.POST.get("advance_payment"), Decimal("0.00"))
        if advance_payment < 0:
            advance_payment = Decimal("0.00")
        if advance_payment > grand_total:
            advance_payment = grand_total

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

        if customer_obj:
            CustomerLedgerEntry.objects.create(
                customer=customer_obj,
                order=order,
                entry_type=CustomerLedgerEntry.EntryType.INVOICE,
                amount=grand_total,
                created_by=request.user,
                notes=f"Order {order.order_id}",
            )
            if advance_payment > 0:
                mapped_method = {
                    TaxInvoice.PaymentMethod.CASH: Payment.Method.CASH,
                    TaxInvoice.PaymentMethod.CARD: Payment.Method.CARD,
                    TaxInvoice.PaymentMethod.TRANSFER: Payment.Method.BANK,
                }.get(payment_method, Payment.Method.CASH)
                payment = Payment.objects.create(
                    customer=customer_obj,
                    branch=order.branch,
                    method=mapped_method,
                    amount=advance_payment,
                    reference=f"POS-{order.order_id}",
                    order=order,
                    created_by=request.user,
                )
                CustomerLedgerEntry.objects.create(
                    customer=customer_obj,
                    order=order,
                    entry_type=CustomerLedgerEntry.EntryType.PAYMENT,
                    amount=payment.amount,
                    created_by=request.user,
                    notes=f"Advance payment ({payment.get_method_display()})",
                )

        for stock_id, qty in cart.items():
            stock = stock_map[stock_id]
            old_qty = stock.quantity
            ensure_stock_locations_seeded_from_branch_stock(stock)
            remove_stock_from_locations(
                part=stock.part,
                branch=stock.branch,
                quantity=qty,
                reason=f"sale_checkout_order_{order.order_id}",
                actor=request.user,
                action="sale_out",
            )
            stock.refresh_from_db(fields=["quantity"])

            sale = Sale.objects.create(
                order=order,
                part=stock.part,
                branch=stock.branch,
                seller=request.user,
                quantity=qty,
                price_at_sale=stock.part.selling_price,
                cost_at_sale=stock.part.cost_price,
            )

            log_audit_event(
                actor=request.user,
                action="stock.adjustment",
                reason="sale_create_stock_decrement",
                object_type="Stock",
                object_id=stock.id,
                branch=stock.branch,
                before={
                    "quantity": old_qty,
                    "part_number": stock.part.part_number,
                    "reason": "sale_create",
                },
                after={
                    "quantity": stock.quantity,
                    "part_number": stock.part.part_number,
                    "reason": "sale_create",
                },
            )
            log_audit_event(
                actor=request.user,
                action="sale.create",
                reason="sale_created_at_checkout",
                object_type="Sale",
                object_id=sale.id,
                branch=stock.branch,
                before={},
                after={
                    "order_id": order.order_id,
                    "part_id": stock.part_id,
                    "quantity": qty,
                    "price_at_sale": stock.part.selling_price,
                    "cost_at_sale": stock.part.cost_price,
                },
            )

        try:
            create_posted_invoice_from_order(
                order=order,
                actor=request.user,
                payment_method=payment_method,
                due_date=due_date,
                customer_vat_number=customer_vat_number,
                advance_payment=advance_payment,
                total_in_words_ar=total_in_words_ar,
            )
        except Exception as exc:  # noqa: BLE001
            messages.warning(
                request,
                f"Sale saved, but tax invoice posting is pending ({exc}).",
            )

    request.session["cart"] = {}
    request.session.modified = True
    messages.success(request, f"Sale completed. Order {order.order_id} created.")
    return redirect("receipt_view", order_id=order.order_id)


@login_required
def receipt_view(request, order_id):
    order = get_object_or_404(
        Order.objects.select_related("seller", "seller__profile", "branch", "customer").prefetch_related("items__part"),
        order_id=order_id,
    )
    if not _user_has_branch_access(request.user, order.branch):
        return HttpResponseForbidden("You do not have access to this receipt.")

    invoice = TaxInvoice.objects.select_related("branch", "order").prefetch_related("lines").filter(order=order).first()
    if invoice is None:
        try:
            invoice = create_posted_invoice_from_order(order=order, actor=request.user)
        except Exception:  # noqa: BLE001
            invoice = None

    if invoice is None:
        return render(request, "inventory/receipt.html", {"order": order, "invoice": None})

    context = build_invoice_template_context(invoice)
    context.update(
        {
            "order": order,
            "invoice": invoice,
            "layout_mode": (request.GET.get("layout") or "a4").strip().lower(),
        }
    )
    return render(request, "inventory/receipt.html", context)


@login_required
@require_GET
def receipt_pdf_view(request, order_id):
    order = get_object_or_404(
        Order.objects.select_related("branch").prefetch_related("items__part"),
        order_id=order_id,
    )
    if not _user_has_branch_access(request.user, order.branch):
        return HttpResponseForbidden("You do not have access to this invoice.")

    invoice = TaxInvoice.objects.select_related("branch", "order").prefetch_related("lines").filter(order=order).first()
    if invoice is None:
        try:
            invoice = create_posted_invoice_from_order(order=order, actor=request.user)
        except Exception as exc:  # noqa: BLE001
            return HttpResponseBadRequest(f"Invoice is not available yet: {exc}")

    layout_mode = (request.GET.get("layout") or "a4").strip().lower()
    if layout_mode not in {"a4", "thermal"}:
        layout_mode = "a4"
    pdf_bytes = render_invoice_pdf_bytes(invoice, layout_mode=layout_mode)
    filename = f"INV-{invoice.branch.code}-{invoice.invoice_number:06d}.pdf"
    response = HttpResponse(pdf_bytes, content_type="application/pdf")
    response["Content-Disposition"] = f'inline; filename="{filename}"'
    return response


@login_required
@require_GET
def invoice_amount_words(request):
    if not _can_use_pos(request.user):
        return HttpResponseForbidden("Your role is not allowed to access invoice utilities.")

    amount_raw = (request.GET.get("amount") or "0").strip()
    amount = _parse_decimal(amount_raw, Decimal("0.00"))
    if amount < 0:
        amount = Decimal("0.00")
    words = amount_to_words_ar(amount)
    return JsonResponse({"amount": f"{_quantize_money(amount):.2f}", "words_ar": words})


def _client_ip(request) -> str:
    return (
        request.META.get("HTTP_X_FORWARDED_FOR", "").split(",")[0].strip()
        or request.META.get("REMOTE_ADDR", "").strip()
    )


def _smacc_ip_allowed(ip_address: str) -> bool:
    allowlist = {str(value).strip() for value in getattr(settings, "SMACC_WEBHOOK_IP_ALLOWLIST", []) if str(value).strip()}
    if not allowlist:
        return True
    return ip_address in allowlist


def _smacc_rate_limited(ip_address: str) -> bool:
    limit = int(getattr(settings, "SMACC_WEBHOOK_RATE_LIMIT_PER_MINUTE", 120) or 120)
    if limit <= 0:
        return False
    bucket = timezone.now().strftime("%Y%m%d%H%M")
    key = f"inventory:smacc:webhook:{ip_address}:{bucket}"
    try:
        current = cache.incr(key)
    except ValueError:
        cache.set(key, 1, timeout=65)
        current = 1
    return current > limit


@csrf_exempt
@require_POST
def smacc_webhook(request):
    ip_address = _client_ip(request)
    if not _smacc_ip_allowed(ip_address):
        return HttpResponseForbidden("IP is not allowed.")
    if _smacc_rate_limited(ip_address):
        return JsonResponse({"ok": False, "error": "Rate limit exceeded."}, status=429)

    signature_header = str(getattr(settings, "SMACC_WEBHOOK_SIGNATURE_HEADER", "X-Smacc-Signature") or "X-Smacc-Signature")
    signature = request.headers.get(signature_header, "")
    client = SmaccClient()
    if not client.verify_webhook_signature(request.body, signature):
        return HttpResponseForbidden("Invalid webhook signature.")

    try:
        payload = json.loads(request.body.decode("utf-8") or "{}")
    except json.JSONDecodeError:
        return HttpResponseBadRequest("Invalid JSON body.")

    event_type = str(payload.get("event") or payload.get("type") or "").strip()
    if event_type and event_type not in SMACC_SYNC_EVENT_TYPES:
        return JsonResponse({"ok": True, "ignored": event_type})

    data = payload.get("data")
    if not isinstance(data, dict):
        data = {}
    origin_id = str(data.get("originId") or data.get("origin_id") or payload.get("originId") or "").strip()
    job_id = str(data.get("jobId") or data.get("job_id") or payload.get("jobId") or "").strip()
    document_id = str(data.get("documentId") or data.get("document_id") or payload.get("documentId") or "").strip()
    status_text = str(data.get("status") or payload.get("status") or "").strip().upper()
    error_text = str(data.get("error") or payload.get("error") or "").strip()

    queue_item = None
    if origin_id:
        queue_item = SmaccSyncQueue.objects.filter(idempotency_key=f"invoice:{origin_id}").first()
    if queue_item is None and job_id:
        queue_item = SmaccSyncQueue.objects.filter(smacc_job_id=job_id).first()
    if queue_item is None and document_id:
        queue_item = SmaccSyncQueue.objects.filter(smacc_document_id=document_id).first()

    if queue_item is None:
        return JsonResponse({"ok": True, "matched_queue": None})

    previous_status = queue_item.status
    if job_id:
        queue_item.smacc_job_id = job_id
    if document_id:
        queue_item.smacc_document_id = document_id

    if status_text in {"FAILED", "ERROR", "REJECTED"}:
        queue_item.status = SmaccSyncQueue.Status.FAILED
        queue_item.last_error = error_text or status_text or "SMACC webhook failure."
    else:
        queue_item.status = SmaccSyncQueue.Status.SYNCED
        queue_item.last_error = ""

    queue_item.save(update_fields=["status", "smacc_job_id", "smacc_document_id", "last_error", "updated_at"])
    SmaccSyncLog.objects.create(
        queue_item=queue_item,
        request_payload={"event": event_type, "ip": ip_address, "payload": payload},
        response_payload={"previous_status": previous_status, "new_status": queue_item.status},
        http_status=200,
    )
    log_audit_event(
        action="smacc.webhook",
        reason=event_type or "smacc_webhook",
        object_type="SmaccSyncQueue",
        object_id=queue_item.id,
        branch=None,
        before={"status": previous_status},
        after={
            "status": queue_item.status,
            "smacc_job_id": queue_item.smacc_job_id,
            "smacc_document_id": queue_item.smacc_document_id,
        },
        ip_address=ip_address,
    )
    return JsonResponse({"ok": True, "matched_queue": queue_item.id, "status": queue_item.status})


@login_required
def order_list(request):
    orders = (
        Order.objects.select_related("seller", "seller__profile", "branch", "customer")
        .prefetch_related("items")
        .order_by("-created_at")
    )
    orders = _scope_branch(orders, request.user, field_name="branch")

    page_obj = Paginator(orders, 25).get_page(request.GET.get("page"))
    return render(request, "inventory/order_list.html", {"orders": page_obj, "page_obj": page_obj})


def _generate_po_number() -> str:
    base = timezone.localtime().strftime("PO-%Y%m%d-%H%M%S")
    candidate = base
    suffix = 1
    while PurchaseOrder.objects.filter(po_number=candidate).exists():
        candidate = f"{base}-{suffix:02d}"
        suffix += 1
    return candidate


def _branch_in_scope_or_none(user, branch_id: str | None) -> Branch | None:
    if not branch_id or not str(branch_id).isdigit():
        return None
    branch = Branch.objects.filter(id=int(branch_id)).first()
    if not branch:
        return None
    if _user_has_branch_access(user, branch):
        return branch
    return None


@login_required
@manager_required
def vendor_list(request):
    query = (request.GET.get("q") or "").strip()
    vendors = Vendor.objects.all().order_by("name_ar", "name_en", "vendor_code")
    if query:
        vendors = vendors.filter(
            Q(vendor_code__icontains=query)
            | Q(name_ar__icontains=query)
            | Q(name_en__icontains=query)
            | Q(phone__icontains=query)
            | Q(email__icontains=query)
        )
    page_obj = Paginator(vendors, 40).get_page(request.GET.get("page"))
    return render(
        request,
        "inventory/vendor_list.html",
        {
            "vendors": page_obj.object_list,
            "page_obj": page_obj,
            "query": query,
        },
    )


@login_required
@manager_required
def vendor_create(request):
    if _blocked_by_view_only_acl(request.user, Vendor):
        return HttpResponseForbidden("حسابك يملك صلاحية عرض فقط على إجراءات الموردين.")
    if request.method == "POST":
        name_ar = (request.POST.get("name_ar") or "").strip()
        name_en = (request.POST.get("name_en") or "").strip()
        if not name_ar and not name_en:
            messages.error(request, "يجب إدخال اسم المورد بالعربية أو الإنجليزية.")
            return redirect("vendor_create")
        vendor = Vendor.objects.create(
            vendor_code=(request.POST.get("vendor_code") or "").strip().upper(),
            name_ar=name_ar,
            name_en=name_en or None,
            phone=(request.POST.get("phone") or "").strip(),
            email=(request.POST.get("email") or "").strip(),
            vat_number=(request.POST.get("vat_number") or "").strip(),
            cr_number=(request.POST.get("cr_number") or "").strip(),
            address=(request.POST.get("address") or "").strip(),
            notes=(request.POST.get("notes") or "").strip(),
            is_active=(request.POST.get("is_active") or "1") in {"1", "true", "on"},
        )
        log_audit_event(
            actor=request.user,
            action="vendor.create",
            reason="vendor_created",
            object_type="Vendor",
            object_id=vendor.id,
            branch=_audit_branch_for_user(request.user),
            before={},
            after={"vendor_code": vendor.vendor_code, "name_ar": vendor.name_ar, "name_en": vendor.name_en},
        )
        messages.success(request, "تم إنشاء المورد بنجاح.")
        return redirect("vendor_detail", vendor_id=vendor.id)
    return render(request, "inventory/vendor_form.html", {"vendor": None})


@login_required
@manager_required
def vendor_detail(request, vendor_id: int):
    vendor = get_object_or_404(Vendor, id=vendor_id)
    po_count = vendor.purchase_orders.count()
    return render(
        request,
        "inventory/vendor_detail.html",
        {"vendor": vendor, "po_count": po_count},
    )


@login_required
@manager_required
def vendor_edit(request, vendor_id: int):
    vendor = get_object_or_404(Vendor, id=vendor_id)
    if _blocked_by_view_only_acl(request.user, Vendor):
        return HttpResponseForbidden("حسابك يملك صلاحية عرض فقط على إجراءات الموردين.")
    if request.method == "POST":
        before = {
            "name_ar": vendor.name_ar,
            "name_en": vendor.name_en,
            "phone": vendor.phone,
            "email": vendor.email,
            "is_active": vendor.is_active,
        }
        vendor.vendor_code = (request.POST.get("vendor_code") or vendor.vendor_code).strip().upper()
        vendor.name_ar = (request.POST.get("name_ar") or "").strip()
        vendor.name_en = (request.POST.get("name_en") or "").strip() or None
        vendor.phone = (request.POST.get("phone") or "").strip()
        vendor.email = (request.POST.get("email") or "").strip()
        vendor.vat_number = (request.POST.get("vat_number") or "").strip()
        vendor.cr_number = (request.POST.get("cr_number") or "").strip()
        vendor.address = (request.POST.get("address") or "").strip()
        vendor.notes = (request.POST.get("notes") or "").strip()
        vendor.is_active = (request.POST.get("is_active") or "1") in {"1", "true", "on"}
        vendor.save()
        log_audit_event(
            actor=request.user,
            action="vendor.update",
            reason="vendor_updated",
            object_type="Vendor",
            object_id=vendor.id,
            branch=_audit_branch_for_user(request.user),
            before=before,
            after={
                "name_ar": vendor.name_ar,
                "name_en": vendor.name_en,
                "phone": vendor.phone,
                "email": vendor.email,
                "is_active": vendor.is_active,
            },
        )
        messages.success(request, "تم تحديث بيانات المورد.")
        return redirect("vendor_detail", vendor_id=vendor.id)
    return render(request, "inventory/vendor_form.html", {"vendor": vendor})


@login_required
@manager_required
def purchase_order_list(request):
    status_filter = (request.GET.get("status") or "").strip().lower()
    branch_filter = (request.GET.get("branch") or "").strip()
    orders = PurchaseOrder.objects.select_related("vendor", "branch", "created_by").prefetch_related("lines").order_by("-created_at")
    if not is_admin_user(request.user):
        branch = _user_branch(request.user)
        if not branch:
            orders = orders.none()
        else:
            orders = orders.filter(branch=branch)
    elif branch_filter.isdigit():
        orders = orders.filter(branch_id=int(branch_filter))
    if status_filter in PurchaseOrder.Status.values:
        orders = orders.filter(status=status_filter)
    page_obj = Paginator(orders, 30).get_page(request.GET.get("page"))
    return render(
        request,
        "inventory/purchase_order_list.html",
        {
            "orders": page_obj.object_list,
            "page_obj": page_obj,
            "status_filter": status_filter,
            "status_choices": PurchaseOrder.Status.choices,
            "branches": _accessible_branches(request.user),
            "selected_branch": int(branch_filter) if branch_filter.isdigit() else None,
        },
    )


@login_required
@manager_required
@active_branch_required
def purchase_order_create(request):
    if _blocked_by_view_only_acl(request.user, PurchaseOrder):
        return HttpResponseForbidden("حسابك يملك صلاحية عرض فقط على إجراءات أوامر الشراء.")
    branches = _accessible_branches(request.user)
    vendors = Vendor.objects.filter(is_active=True).order_by("name_ar", "name_en")
    parts = Part.objects.order_by("part_number")
    if request.method == "POST":
        vendor = Vendor.objects.filter(id=request.POST.get("vendor_id"), is_active=True).first()
        branch = request.active_branch
        if is_admin_user(request.user):
            selected_branch = _branch_in_scope_or_none(request.user, request.POST.get("branch_id"))
            if selected_branch:
                branch = selected_branch

        if not vendor:
            messages.error(request, "يرجى اختيار المورد.")
            return redirect("purchase_order_create")
        if not branch:
            messages.error(request, "يرجى اختيار الفرع.")
            return redirect("purchase_order_create")

        part_ids = request.POST.getlist("part_id")
        qty_values = request.POST.getlist("qty_ordered")
        cost_values = request.POST.getlist("unit_cost")
        tax_values = request.POST.getlist("tax_rate")
        discount_values = request.POST.getlist("discount")
        line_payloads = []
        for idx, raw_part_id in enumerate(part_ids):
            if not str(raw_part_id).isdigit():
                continue
            try:
                qty_ordered = int((qty_values[idx] if idx < len(qty_values) else "0") or 0)
            except (TypeError, ValueError):
                qty_ordered = 0
            unit_cost = _parse_decimal(cost_values[idx] if idx < len(cost_values) else "0", Decimal("0.00"))
            tax_rate = _parse_decimal(tax_values[idx] if idx < len(tax_values) else "15", Decimal("15.00"))
            discount = _parse_decimal(discount_values[idx] if idx < len(discount_values) else "0", Decimal("0.00"))
            if qty_ordered <= 0:
                continue
            part = Part.objects.filter(id=int(raw_part_id)).first()
            if not part:
                continue
            line_payloads.append(
                {
                    "part": part,
                    "qty_ordered": qty_ordered,
                    "unit_cost": unit_cost,
                    "tax_rate": tax_rate,
                    "discount": discount,
                }
            )
        if not line_payloads:
            messages.error(request, "يجب إضافة بند صحيح واحد على الأقل في أمر الشراء.")
            return redirect("purchase_order_create")

        expected_date = None
        expected_date_raw = (request.POST.get("expected_date") or "").strip()
        if expected_date_raw:
            try:
                expected_date = date.fromisoformat(expected_date_raw)
            except ValueError:
                expected_date = None
        notes = (request.POST.get("notes") or "").strip()
        initial_status = PurchaseOrder.Status.SENT if (request.POST.get("mark_sent") or "") in {"1", "true", "on"} else PurchaseOrder.Status.DRAFT

        with transaction.atomic():
            po = PurchaseOrder.objects.create(
                po_number=_generate_po_number(),
                vendor=vendor,
                branch=branch,
                created_by=request.user,
                status=initial_status,
                expected_date=expected_date,
                notes=notes,
            )
            for payload in line_payloads:
                PurchaseOrderLine.objects.create(po=po, **payload)
            log_audit_event(
                actor=request.user,
                action="purchase_order.create",
                reason=notes or "purchase_order_created",
                object_type="PurchaseOrder",
                object_id=po.id,
                branch=po.branch,
                before={},
                after={
                    "po_number": po.po_number,
                    "vendor_id": po.vendor_id,
                    "status": po.status,
                    "line_count": len(line_payloads),
                },
            )
        messages.success(request, f"تم إنشاء أمر الشراء {po.po_number}.")
        return redirect("purchase_order_detail", po_id=po.id)

    return render(
        request,
        "inventory/purchase_order_form.html",
        {
            "vendors": vendors,
            "parts": parts,
            "branches": branches,
            "active_branch": request.active_branch,
        },
    )


@login_required
@manager_required
def purchase_order_detail(request, po_id: int):
    po = get_object_or_404(
        PurchaseOrder.objects.select_related("vendor", "branch", "created_by").prefetch_related("lines__part", "receipts"),
        id=po_id,
    )
    if not _user_has_branch_access(request.user, po.branch):
        return HttpResponseForbidden("ليس لديك صلاحية الوصول إلى أمر الشراء هذا.")
    if request.method == "POST":
        if _blocked_by_view_only_acl(request.user, PurchaseOrderLine):
            return HttpResponseForbidden("حسابك يملك صلاحية عرض فقط على إجراءات أوامر الشراء.")
        if po.is_closed:
            messages.error(request, "لا يمكن تعديل أوامر الشراء المغلقة.")
            return redirect("purchase_order_detail", po_id=po.id)
        part = Part.objects.filter(id=request.POST.get("part_id")).first()
        try:
            qty_ordered = int(request.POST.get("qty_ordered") or 0)
        except (TypeError, ValueError):
            qty_ordered = 0
        unit_cost = _parse_decimal(request.POST.get("unit_cost"), Decimal("0.00"))
        tax_rate = _parse_decimal(request.POST.get("tax_rate"), Decimal("15.00"))
        discount = _parse_decimal(request.POST.get("discount"), Decimal("0.00"))
        if not part or qty_ordered <= 0:
            messages.error(request, "يرجى اختيار صنف صحيح وإدخال كمية أكبر من صفر.")
            return redirect("purchase_order_detail", po_id=po.id)
        try:
            PurchaseOrderLine.objects.create(
                po=po,
                part=part,
                qty_ordered=qty_ordered,
                unit_cost=unit_cost,
                tax_rate=tax_rate,
                discount=discount,
            )
        except IntegrityError:
            messages.error(request, "هذا الصنف موجود مسبقًا في بنود أمر الشراء.")
            return redirect("purchase_order_detail", po_id=po.id)
        messages.success(request, "تمت إضافة بند أمر الشراء.")
        return redirect("purchase_order_detail", po_id=po.id)
    return render(
        request,
        "inventory/purchase_order_detail.html",
        {
            "po": po,
            "lines": po.lines.select_related("part").order_by("id"),
            "parts": Part.objects.order_by("part_number"),
        },
    )


@login_required
@manager_required
def purchase_receipt_list(request):
    status_filter = (request.GET.get("status") or "").strip().lower()
    receipts = PurchaseReceipt.objects.select_related("po", "branch", "received_by").order_by("-received_at")
    if not is_admin_user(request.user):
        branch = _user_branch(request.user)
        if not branch:
            receipts = receipts.none()
        else:
            receipts = receipts.filter(branch=branch)
    if status_filter in PurchaseReceipt.Status.values:
        receipts = receipts.filter(status=status_filter)
    page_obj = Paginator(receipts, 30).get_page(request.GET.get("page"))
    return render(
        request,
        "inventory/purchase_receipt_list.html",
        {
            "receipts": page_obj.object_list,
            "page_obj": page_obj,
            "status_filter": status_filter,
            "status_choices": PurchaseReceipt.Status.choices,
        },
    )


@login_required
@manager_required
@require_POST
def purchase_receipt_create(request, po_id: int):
    po = get_object_or_404(PurchaseOrder.objects.select_related("branch"), id=po_id)
    if not _user_has_branch_access(request.user, po.branch):
        return HttpResponseForbidden("ليس لديك صلاحية الوصول إلى أمر الشراء هذا.")
    if _blocked_by_view_only_acl(request.user, PurchaseReceipt):
        return HttpResponseForbidden("حسابك يملك صلاحية عرض فقط على إجراءات الاستلام.")
    if po.status == PurchaseOrder.Status.CANCELLED:
        messages.error(request, "لا يمكن إنشاء استلام لأمر شراء ملغي.")
        return redirect("purchase_order_detail", po_id=po.id)
    receipt = PurchaseReceipt.objects.create(
        po=po,
        branch=po.branch,
        received_by=request.user,
        invoice_ref=(request.POST.get("invoice_ref") or "").strip(),
        notes=(request.POST.get("notes") or "").strip(),
        status=PurchaseReceipt.Status.DRAFT,
    )
    messages.success(request, f"تم إنشاء مسودة سند الاستلام GRN-{receipt.id}.")
    return redirect("purchase_receipt_detail", receipt_id=receipt.id)


def _resolve_po_line_from_scan(*, po: PurchaseOrder, scan_code: str) -> PurchaseOrderLine | None:
    token = (scan_code or "").strip()
    if not token:
        return None
    matched_part = (
        Part.objects.filter(
            Q(part_number__iexact=token)
            | Q(barcode__iexact=token)
            | Q(sku__iexact=token)
            | Q(manufacturer_part_number__iexact=token)
            | Q(barcodes__barcode__iexact=token)
        )
        .distinct()
        .first()
    )
    if not matched_part:
        return None
    return (
        po.lines.select_related("part")
        .filter(part=matched_part)
        .order_by("id")
        .first()
    )


@login_required
@manager_required
def purchase_receipt_detail(request, receipt_id: int):
    receipt = get_object_or_404(
        PurchaseReceipt.objects.select_related("po", "po__vendor", "branch", "received_by")
        .prefetch_related("lines__po_line__part"),
        id=receipt_id,
    )
    if not _user_has_branch_access(request.user, receipt.branch):
        return HttpResponseForbidden("ليس لديك صلاحية الوصول إلى سجل الاستلام هذا.")
    if request.method == "POST":
        if _blocked_by_view_only_acl(request.user, PurchaseReceiptLine):
            return HttpResponseForbidden("حسابك يملك صلاحية عرض فقط على إجراءات الاستلام.")
        if receipt.status == PurchaseReceipt.Status.POSTED:
            messages.error(request, "سند الاستلام المرحّل غير قابل للتعديل.")
            return redirect("purchase_receipt_detail", receipt_id=receipt.id)
        po_line = receipt.po.lines.select_related("part").filter(id=request.POST.get("po_line_id")).first()
        if not po_line:
            po_line = _resolve_po_line_from_scan(po=receipt.po, scan_code=request.POST.get("scan_code"))
        if not po_line:
            messages.error(request, "يرجى اختيار سطر أمر الشراء أو إدخال كود مسح صحيح.")
            return redirect("purchase_receipt_detail", receipt_id=receipt.id)
        try:
            qty_received = int(request.POST.get("qty_received") or 0)
        except (TypeError, ValueError):
            qty_received = 0
        if qty_received <= 0:
            messages.error(request, "الكمية يجب أن تكون أكبر من صفر.")
            return redirect("purchase_receipt_detail", receipt_id=receipt.id)
        if qty_received > po_line.remaining_qty:
            messages.error(request, f"الكمية تتجاوز المتبقي في أمر الشراء ({po_line.remaining_qty}).")
            return redirect("purchase_receipt_detail", receipt_id=receipt.id)
        unit_cost = _parse_decimal(request.POST.get("unit_cost"), po_line.unit_cost)
        location = Location.objects.filter(id=request.POST.get("location_id"), branch=receipt.branch).first()
        PurchaseReceiptLine.objects.create(
            receipt=receipt,
            po_line=po_line,
            qty_received=qty_received,
            unit_cost=unit_cost,
            location=location,
            batch_lot=(request.POST.get("batch_lot") or "").strip(),
            expiry=(request.POST.get("expiry") or None) or None,
        )
        messages.success(request, "تمت إضافة بند الاستلام.")
        return redirect("purchase_receipt_detail", receipt_id=receipt.id)

    return render(
        request,
        "inventory/purchase_receipt_detail.html",
        {
            "receipt": receipt,
            "lines": receipt.lines.select_related("po_line", "po_line__part", "location").order_by("id"),
            "po_lines_open": receipt.po.lines.select_related("part").order_by("id"),
            "locations": Location.objects.filter(branch=receipt.branch).order_by("code"),
        },
    )


@login_required
@manager_required
@require_POST
def purchase_receipt_post(request, receipt_id: int):
    receipt = get_object_or_404(
        PurchaseReceipt.objects.select_related("po", "po__vendor", "branch", "received_by"),
        id=receipt_id,
    )
    if not _user_has_branch_access(request.user, receipt.branch):
        return HttpResponseForbidden("ليس لديك صلاحية الوصول إلى سجل الاستلام هذا.")
    if _blocked_by_view_only_acl(request.user, PurchaseReceipt) or _blocked_by_view_only_acl(request.user, Stock):
        return HttpResponseForbidden("حسابك يملك صلاحية عرض فقط على إجراءات الاستلام.")
    reason = (request.POST.get("reason") or "").strip() or "purchase_receipt_post"

    try:
        with transaction.atomic():
            locked_receipt = (
                PurchaseReceipt.objects.select_for_update()
                .select_related("po", "branch")
                .filter(id=receipt.id)
                .first()
            )
            if not locked_receipt:
                raise ValidationError("سجل الاستلام غير موجود.")
            if locked_receipt.status == PurchaseReceipt.Status.POSTED:
                raise ValidationError("تم ترحيل سند الاستلام مسبقًا.")
            receipt_lines = list(
                locked_receipt.lines.select_for_update().select_related("po_line", "po_line__part", "location").order_by("id")
            )
            if not receipt_lines:
                raise ValidationError("لا يمكن ترحيل سند استلام بدون بنود.")

            for line in receipt_lines:
                po_line = PurchaseOrderLine.objects.select_for_update().select_related("part", "po").get(id=line.po_line_id)
                remaining = max(int(po_line.qty_ordered or 0) - int(po_line.qty_received or 0), 0)
                if remaining <= 0:
                    continue
                if int(line.qty_received or 0) > remaining:
                    raise ValidationError(
                        f"بند الاستلام للصنف {po_line.part.part_number} يتجاوز الكمية المتبقية ({remaining})."
                    )

                stock_before = int(
                    Stock.objects.filter(part=po_line.part, branch=locked_receipt.branch).values_list("quantity", flat=True).first()
                    or 0
                )
                add_stock_to_location(
                    part=po_line.part,
                    branch=locked_receipt.branch,
                    quantity=line.qty_received,
                    reason=reason,
                    actor=request.user,
                    location=line.location,
                    action="purchase_in",
                )
                update_branch_average_cost(
                    part=po_line.part,
                    branch=locked_receipt.branch,
                    received_qty=line.qty_received,
                    received_unit_cost=line.unit_cost,
                )
                po_line.qty_received = int(po_line.qty_received or 0) + int(line.qty_received or 0)
                po_line.save(update_fields=["qty_received"])

                stock_after = int(
                    Stock.objects.filter(part=po_line.part, branch=locked_receipt.branch).values_list("quantity", flat=True).first()
                    or 0
                )
                log_audit_event(
                    actor=request.user,
                    action="stock.adjustment",
                    reason=reason,
                    object_type="Stock",
                    object_id=f"{po_line.part_id}:{locked_receipt.branch_id}",
                    branch=locked_receipt.branch,
                    before={
                        "quantity": stock_before,
                        "part_number": po_line.part.part_number,
                        "reason": "purchase_receipt",
                        "receipt_id": locked_receipt.id,
                    },
                    after={
                        "quantity": stock_after,
                        "part_number": po_line.part.part_number,
                        "reason": "purchase_receipt",
                        "receipt_id": locked_receipt.id,
                    },
                )

            locked_receipt.status = PurchaseReceipt.Status.POSTED
            locked_receipt.posted_at = timezone.now()
            locked_receipt.save(update_fields=["status", "posted_at"])
            locked_receipt.po.refresh_status_from_lines()
            log_audit_event(
                actor=request.user,
                action="purchase_receipt.post",
                reason=reason,
                object_type="PurchaseReceipt",
                object_id=locked_receipt.id,
                branch=locked_receipt.branch,
                before={"status": PurchaseReceipt.Status.DRAFT},
                after={"status": locked_receipt.status, "po_status": locked_receipt.po.status},
            )
    except (ValidationError, ValueError) as exc:
        messages.error(request, str(exc))
        return redirect("purchase_receipt_detail", receipt_id=receipt.id)

    messages.success(request, f"تم ترحيل سند الاستلام GRN-{receipt.id} بنجاح.")
    return redirect("purchase_order_detail", po_id=receipt.po_id)


@login_required
def customer_profile(request, customer_id: int):
    customer = get_object_or_404(Customer, id=customer_id)
    ledger = customer.ledger_entries.select_related("order", "created_by").order_by("-created_at", "-id")
    payments = customer.payments.select_related("branch", "created_by", "order").order_by("-created_at")
    credits = customer.credit_notes.select_related("branch", "created_by", "order").order_by("-created_at")
    customer_orders = Order.objects.select_related("branch").filter(customer=customer).order_by("-created_at")
    if not is_manager(request.user):
        user_branch = _user_branch(request.user)
        if not user_branch:
            ledger = ledger.none()
            payments = payments.none()
            credits = credits.none()
            customer_orders = customer_orders.none()
        else:
            ledger = ledger.filter(Q(order__branch=user_branch) | Q(order__isnull=True))
            payments = payments.filter(branch=user_branch)
            credits = credits.filter(branch=user_branch)
            customer_orders = customer_orders.filter(branch=user_branch)
    return render(
        request,
        "inventory/customer_profile.html",
        {
            "customer": customer,
            "ledger_entries": ledger[:200],
            "payments": payments[:200],
            "credit_notes": credits[:100],
            "balance": customer.ledger_balance,
            "payment_methods": Payment.Method.choices,
            "is_manager": is_manager(request.user),
            "customer_orders": customer_orders[:100],
        },
    )


@login_required
@active_branch_required
@require_POST
def customer_add_payment(request, customer_id: int):
    customer = get_object_or_404(Customer, id=customer_id)
    if _blocked_by_view_only_acl(request.user, Payment):
        return HttpResponseForbidden("Your account is view-only for payment actions.")
    amount = _parse_decimal(request.POST.get("amount"), Decimal("0.00"))
    if amount <= 0:
        messages.error(request, "Payment amount must be greater than zero.")
        return redirect("customer_profile", customer_id=customer.id)
    method = (request.POST.get("method") or Payment.Method.CASH).strip().lower()
    if method not in Payment.Method.values:
        method = Payment.Method.CASH
    order = Order.objects.filter(id=request.POST.get("order_id"), customer=customer).first()
    if order and not _user_has_branch_access(request.user, order.branch):
        return HttpResponseForbidden("You cannot register payment for this order.")
    payment = Payment.objects.create(
        customer=customer,
        branch=request.active_branch,
        method=method,
        amount=amount,
        reference=(request.POST.get("reference") or "").strip(),
        order=order,
        created_by=request.user,
    )
    CustomerLedgerEntry.objects.create(
        customer=customer,
        order=order,
        entry_type=CustomerLedgerEntry.EntryType.PAYMENT,
        amount=payment.amount,
        created_by=request.user,
        notes=f"Manual payment ({payment.get_method_display()})",
    )
    log_audit_event(
        actor=request.user,
        action="payment.create",
        reason="customer_payment",
        object_type="Payment",
        object_id=payment.id,
        branch=payment.branch,
        before={},
        after={"customer_id": customer.id, "amount": str(payment.amount), "method": payment.method, "order_id": payment.order_id},
    )
    messages.success(request, "Payment recorded.")
    return redirect("customer_profile", customer_id=customer.id)


@login_required
@manager_required
@active_branch_required
@require_POST
def credit_note_create(request, order_id: int):
    if _blocked_by_view_only_acl(request.user, CreditNote):
        return HttpResponseForbidden("Your account is view-only for credit note actions.")
    order = get_object_or_404(Order.objects.select_related("customer", "branch"), id=order_id)
    if not _user_has_branch_access(request.user, order.branch):
        return HttpResponseForbidden("You do not have access to this order.")
    if order.branch_id != request.active_branch.id:
        messages.error(request, "Active branch must match order branch.")
        return redirect("order_list")
    invoice = TaxInvoice.objects.filter(order=order).first()
    if not invoice:
        messages.error(request, "Order has no tax invoice yet.")
        return redirect("order_list")

    reason = (request.POST.get("reason") or "").strip()
    if not reason:
        messages.error(request, "Credit note reason is required.")
        return redirect("order_list")
    return_to_stock_default = (request.POST.get("return_to_stock") or "") in {"1", "true", "on"}

    sale_rows = list(order.items.select_related("part", "branch"))
    selected_sale_ids = request.POST.getlist("sale_id")
    if not selected_sale_ids:
        selected_sale_ids = [str(sale.id) for sale in sale_rows]
    selected_set = {int(value) for value in selected_sale_ids if str(value).isdigit()}
    line_inputs = []
    for sale in sale_rows:
        if sale.id not in selected_set:
            continue
        qty_key = f"qty_returned_{sale.id}"
        try:
            qty_returned = int(request.POST.get(qty_key) or sale.quantity)
        except (TypeError, ValueError):
            qty_returned = sale.quantity
        qty_returned = max(min(qty_returned, int(sale.quantity or 0)), 0)
        if qty_returned <= 0 or not sale.part_id:
            continue
        line_return_flag = (request.POST.get(f"return_to_stock_{sale.id}") or "").strip()
        line_return_to_stock = return_to_stock_default if not line_return_flag else line_return_flag in {"1", "true", "on"}
        line_inputs.append(
            {
                "sale": sale,
                "qty": qty_returned,
                "return_to_stock": line_return_to_stock,
                "line_reason": (request.POST.get(f"line_reason_{sale.id}") or reason).strip(),
            }
        )
    if not line_inputs:
        messages.error(request, "At least one return line is required.")
        return redirect("order_list")

    with transaction.atomic():
        credit_note = CreditNote.objects.create(
            invoice=invoice,
            branch=order.branch,
            order=order,
            customer=order.customer,
            reason=reason,
            return_to_stock=return_to_stock_default,
            created_by=request.user,
            state="posted",
        )
        total_before = Decimal("0.00")
        total_vat = Decimal("0.00")
        for payload in line_inputs:
            sale = payload["sale"]
            subtotal = _quantize_money(Decimal(payload["qty"]) * Decimal(sale.price_at_sale))
            vat_value = _quantize_money(subtotal * VAT_RATE)
            CreditNoteLine.objects.create(
                credit_note=credit_note,
                sale=sale,
                part=sale.part,
                quantity=payload["qty"],
                unit_price=sale.price_at_sale,
                vat_rate=Decimal("15.00"),
                reason=payload["line_reason"],
                return_to_stock=payload["return_to_stock"],
            )
            total_before += subtotal
            total_vat += vat_value
            if payload["return_to_stock"]:
                stock_before = int(
                    Stock.objects.filter(part=sale.part, branch=order.branch).values_list("quantity", flat=True).first()
                    or 0
                )
                add_stock_to_location(
                    part=sale.part,
                    branch=order.branch,
                    quantity=payload["qty"],
                    reason=reason,
                    actor=request.user,
                    action="credit_note_return_in",
                )
                stock_after = int(
                    Stock.objects.filter(part=sale.part, branch=order.branch).values_list("quantity", flat=True).first()
                    or 0
                )
                log_audit_event(
                    actor=request.user,
                    action="stock.adjustment",
                    reason=reason,
                    object_type="Stock",
                    object_id=f"{sale.part_id}:{order.branch_id}",
                    branch=order.branch,
                    before={"quantity": stock_before, "sale_id": sale.id, "reason": "credit_note_return"},
                    after={"quantity": stock_after, "sale_id": sale.id, "reason": "credit_note_return"},
                )
            if int(payload["qty"]) >= int(sale.quantity or 0):
                sale.is_refunded = True
                sale.save(update_fields=["is_refunded"])
        credit_note.amount_before_vat = _quantize_money(total_before)
        credit_note.vat_amount = _quantize_money(total_vat)
        credit_note.amount_after_vat = _quantize_money(total_before + total_vat)
        credit_note.save(update_fields=["amount_before_vat", "vat_amount", "amount_after_vat"])

        if order.customer_id:
            CustomerLedgerEntry.objects.create(
                customer=order.customer,
                order=order,
                entry_type=CustomerLedgerEntry.EntryType.CREDIT_NOTE,
                amount=credit_note.amount_after_vat,
                created_by=request.user,
                notes=f"Credit note #{credit_note.id}",
            )
        log_audit_event(
            actor=request.user,
            action="credit_note.create",
            reason=reason,
            object_type="CreditNote",
            object_id=credit_note.id,
            branch=credit_note.branch,
            before={},
            after={
                "order_id": order.id,
                "customer_id": order.customer_id,
                "amount_after_vat": str(credit_note.amount_after_vat),
                "line_count": len(line_inputs),
            },
        )
    messages.success(request, f"Credit note #{credit_note.id} created.")
    if order.customer_id:
        return redirect("customer_profile", customer_id=order.customer_id)
    return redirect("order_list")


@login_required
def part_insight(request, part_id: int):
    part = get_object_or_404(Part, id=part_id)
    branch_scope = _accessible_branches(request.user)
    branch_ids = [branch.id for branch in branch_scope]
    stock_rows = (
        Stock.objects.select_related("branch")
        .filter(part=part, branch_id__in=branch_ids)
        .order_by("branch__name")
    )
    sales_rows = (
        Sale.objects.select_related("order", "branch", "seller")
        .filter(part=part, branch_id__in=branch_ids)
        .order_by("-date_sold")[:10]
    )
    transfer_rows = (
        TransferRequest.objects.select_related("source_branch", "destination_branch", "requested_by")
        .filter(part=part)
        .filter(Q(source_branch_id__in=branch_ids) | Q(destination_branch_id__in=branch_ids))
        .order_by("-created_at")[:10]
    )
    receipt_rows = (
        PurchaseReceiptLine.objects.select_related("receipt", "receipt__po", "receipt__branch")
        .filter(po_line__part=part, receipt__branch_id__in=branch_ids)
        .order_by("-receipt__received_at")[:10]
    )
    recent_barcodes = part.barcodes.order_by("-created_at")[:15]
    return render(
        request,
        "inventory/part_insight.html",
        {
            "part": part,
            "stock_rows": stock_rows,
            "sales_rows": sales_rows,
            "transfer_rows": transfer_rows,
            "receipt_rows": receipt_rows,
            "recent_barcodes": recent_barcodes,
        },
    )


@login_required
@manager_required
def barcode_unmatched(request):
    scan_code = (request.GET.get("code") or request.POST.get("scan_code") or "").strip()
    if request.method == "POST":
        if _blocked_by_view_only_acl(request.user, PartBarcode):
            return HttpResponseForbidden("Your account is view-only for barcode updates.")
        part = Part.objects.filter(id=request.POST.get("part_id")).first()
        if not scan_code:
            messages.error(request, "Barcode is required.")
            return redirect("barcode_unmatched")
        if not part:
            messages.error(request, "Select a part.")
            return redirect(f"{reverse('barcode_unmatched')}?code={scan_code}")
        existing = PartBarcode.objects.filter(barcode__iexact=scan_code).first()
        if existing and existing.part_id != part.id:
            messages.error(request, f"Barcode is already linked to {existing.part.part_number}.")
            return redirect(f"{reverse('barcode_unmatched')}?code={scan_code}")
        obj, created = PartBarcode.objects.get_or_create(
            barcode=scan_code,
            defaults={
                "part": part,
                "note": (request.POST.get("note") or "").strip(),
            },
        )
        if not created and obj.part_id != part.id:
            obj.part = part
            obj.note = (request.POST.get("note") or "").strip()
            obj.save(update_fields=["part", "note"])
        log_audit_event(
            actor=request.user,
            action="part_barcode.link",
            reason="barcode_linked",
            object_type="PartBarcode",
            object_id=obj.id,
            branch=_audit_branch_for_user(request.user),
            before={},
            after={"part_id": obj.part_id, "barcode": obj.barcode},
        )
        messages.success(request, f"Barcode linked to {part.part_number}.")
        return redirect("part_insight", part_id=part.id)

    return render(
        request,
        "inventory/barcode_unmatched.html",
        {
            "scan_code": scan_code,
            "parts": Part.objects.order_by("part_number")[:500],
        },
    )


def _can_approve_cycle_count(user) -> bool:
    if is_admin_user(user) or is_manager(user):
        return True
    return user.username.strip().casefold() == "dbp01"


@login_required
@manager_required
@active_branch_required
def cycle_count_list(request):
    sessions = CycleCountSession.objects.select_related("branch", "location", "created_by", "approved_by").order_by("-started_at")
    if not is_admin_user(request.user):
        sessions = sessions.filter(branch=request.active_branch)

    if request.method == "POST":
        if _blocked_by_view_only_acl(request.user, Stock):
            return HttpResponseForbidden("Your account is view-only for cycle count actions.")
        branch = request.active_branch
        if is_admin_user(request.user):
            selected = _branch_in_scope_or_none(request.user, request.POST.get("branch_id"))
            if selected:
                branch = selected
        location = Location.objects.filter(id=request.POST.get("location_id"), branch=branch).first()
        session = CycleCountSession.objects.create(
            branch=branch,
            location=location,
            created_by=request.user,
            notes=(request.POST.get("notes") or "").strip(),
            status=CycleCountSession.Status.DRAFT,
        )
        log_audit_event(
            actor=request.user,
            action="cycle_count.create",
            reason="cycle_count_started",
            object_type="CycleCountSession",
            object_id=session.id,
            branch=session.branch,
            before={},
            after={"status": session.status, "location_id": session.location_id},
        )
        messages.success(request, f"Cycle count session #{session.id} created.")
        return redirect("cycle_count_detail", session_id=session.id)

    page_obj = Paginator(sessions, 30).get_page(request.GET.get("page"))
    return render(
        request,
        "inventory/cycle_count_list.html",
        {
            "sessions": page_obj.object_list,
            "page_obj": page_obj,
            "branches": _accessible_branches(request.user),
            "locations": Location.objects.filter(branch=request.active_branch).order_by("code"),
            "active_branch": request.active_branch,
        },
    )


@login_required
@manager_required
@active_branch_required
def cycle_count_detail(request, session_id: int):
    session = get_object_or_404(
        CycleCountSession.objects.select_related("branch", "location", "created_by", "approved_by").prefetch_related("lines__part"),
        id=session_id,
    )
    if not _user_has_branch_access(request.user, session.branch):
        return HttpResponseForbidden("You do not have access to this cycle count session.")
    if request.method == "POST":
        action = (request.POST.get("action") or "").strip().lower()

        if action == "add_line":
            if _blocked_by_view_only_acl(request.user, Stock):
                return HttpResponseForbidden("Your account is view-only for cycle count actions.")
            if session.status in {CycleCountSession.Status.SUBMITTED, CycleCountSession.Status.APPROVED, CycleCountSession.Status.REJECTED}:
                messages.error(request, "This session is locked.")
                return redirect("cycle_count_detail", session_id=session.id)
            part = Part.objects.filter(id=request.POST.get("part_id")).first()
            if not part:
                scan_code = (request.POST.get("scan_code") or "").strip()
                if scan_code:
                    part = (
                        Part.objects.filter(
                            Q(part_number__iexact=scan_code)
                            | Q(barcode__iexact=scan_code)
                            | Q(sku__iexact=scan_code)
                            | Q(manufacturer_part_number__iexact=scan_code)
                            | Q(barcodes__barcode__iexact=scan_code)
                        )
                        .distinct()
                        .first()
                    )
            if not part:
                messages.error(request, "Part or valid scan code is required.")
                return redirect("cycle_count_detail", session_id=session.id)
            try:
                counted_qty = int(request.POST.get("counted_qty") or 0)
            except (TypeError, ValueError):
                counted_qty = 0
            line, _ = CycleCountLine.objects.get_or_create(session=session, part=part, defaults={"counted_qty": counted_qty})
            if line.counted_qty != counted_qty:
                line.counted_qty = counted_qty
                line.save(update_fields=["counted_qty"])
            if session.status == CycleCountSession.Status.DRAFT:
                session.status = CycleCountSession.Status.IN_PROGRESS
                session.save(update_fields=["status"])
            messages.success(request, "Count line saved.")
            return redirect("cycle_count_detail", session_id=session.id)

        if action == "submit":
            if _blocked_by_view_only_acl(request.user, Stock):
                return HttpResponseForbidden("Your account is view-only for cycle count actions.")
            if session.status not in {CycleCountSession.Status.DRAFT, CycleCountSession.Status.IN_PROGRESS}:
                messages.error(request, "Only draft/in-progress sessions can be submitted.")
                return redirect("cycle_count_detail", session_id=session.id)
            with transaction.atomic():
                locked_session = CycleCountSession.objects.select_for_update().get(id=session.id)
                lines = list(CycleCountLine.objects.select_for_update().filter(session=locked_session).select_related("part"))
                if not lines:
                    messages.error(request, "Add at least one line before submitting.")
                    return redirect("cycle_count_detail", session_id=session.id)
                for line in lines:
                    if locked_session.location_id:
                        snapshot = int(
                            StockLocation.objects.filter(
                                part=line.part,
                                branch=locked_session.branch,
                                location=locked_session.location,
                            ).values_list("quantity", flat=True).first()
                            or 0
                        )
                    else:
                        snapshot = int(
                            Stock.objects.filter(part=line.part, branch=locked_session.branch).values_list("quantity", flat=True).first()
                            or 0
                        )
                    line.system_qty_snapshot = snapshot
                    line.save(update_fields=["system_qty_snapshot"])
                locked_session.status = CycleCountSession.Status.SUBMITTED
                locked_session.submitted_at = timezone.now()
                locked_session.save(update_fields=["status", "submitted_at"])
                log_audit_event(
                    actor=request.user,
                    action="cycle_count.submit",
                    reason="cycle_count_submitted",
                    object_type="CycleCountSession",
                    object_id=locked_session.id,
                    branch=locked_session.branch,
                    before={"status": session.status},
                    after={"status": locked_session.status},
                )
            messages.success(request, "Cycle count submitted.")
            return redirect("cycle_count_detail", session_id=session.id)

        if action in {"approve", "reject"}:
            if not _can_approve_cycle_count(request.user):
                return HttpResponseForbidden("Only Manager/Admin/DBP01 can approve cycle counts.")
            if _blocked_by_view_only_acl(request.user, Stock):
                return HttpResponseForbidden("Your account is view-only for stock actions.")
            if session.status != CycleCountSession.Status.SUBMITTED:
                messages.error(request, "Only submitted sessions can be approved/rejected.")
                return redirect("cycle_count_detail", session_id=session.id)

            with transaction.atomic():
                locked_session = CycleCountSession.objects.select_for_update().get(id=session.id)
                if action == "reject":
                    locked_session.status = CycleCountSession.Status.REJECTED
                    locked_session.save(update_fields=["status"])
                    log_audit_event(
                        actor=request.user,
                        action="cycle_count.reject",
                        reason="cycle_count_rejected",
                        object_type="CycleCountSession",
                        object_id=locked_session.id,
                        branch=locked_session.branch,
                        before={"status": CycleCountSession.Status.SUBMITTED},
                        after={"status": CycleCountSession.Status.REJECTED},
                    )
                    messages.info(request, "Cycle count rejected.")
                    return redirect("cycle_count_detail", session_id=session.id)

                lines = list(CycleCountLine.objects.select_for_update().filter(session=locked_session).select_related("part"))
                for line in lines:
                    variance = line.variance
                    if variance == 0:
                        continue
                    if variance > 0:
                        add_stock_to_location(
                            part=line.part,
                            branch=locked_session.branch,
                            quantity=variance,
                            reason=f"cycle_count_session_{locked_session.id}",
                            actor=request.user,
                            location=locked_session.location,
                            action="cycle_count_adjust_in",
                        )
                    else:
                        remove_stock_from_locations(
                            part=line.part,
                            branch=locked_session.branch,
                            quantity=abs(variance),
                            reason=f"cycle_count_session_{locked_session.id}",
                            actor=request.user,
                            from_location=locked_session.location,
                            action="cycle_count_adjust_out",
                        )
                    log_audit_event(
                        actor=request.user,
                        action="cycle_count.adjust",
                        reason=f"cycle_count_session_{locked_session.id}",
                        object_type="CycleCountLine",
                        object_id=line.id,
                        branch=locked_session.branch,
                        before={
                            "system_qty_snapshot": line.system_qty_snapshot,
                            "counted_qty": line.counted_qty,
                        },
                        after={"variance": variance},
                    )
                locked_session.status = CycleCountSession.Status.APPROVED
                locked_session.approved_by = request.user
                locked_session.approved_at = timezone.now()
                locked_session.save(update_fields=["status", "approved_by", "approved_at"])
                log_audit_event(
                    actor=request.user,
                    action="cycle_count.approve",
                    reason="cycle_count_approved",
                    object_type="CycleCountSession",
                    object_id=locked_session.id,
                    branch=locked_session.branch,
                    before={"status": CycleCountSession.Status.SUBMITTED},
                    after={"status": CycleCountSession.Status.APPROVED, "approved_by": request.user.username},
                )
            messages.success(request, "Cycle count approved and adjustments applied.")
            return redirect("cycle_count_detail", session_id=session.id)

    lines = session.lines.select_related("part").order_by("part__part_number")
    return render(
        request,
        "inventory/cycle_count_detail.html",
        {
            "session": session,
            "lines": lines,
            "parts": Part.objects.order_by("part_number")[:500],
            "can_approve": _can_approve_cycle_count(request.user),
        },
    )


@login_required
@admin_required
def audit_log_list(request):
    logs = AuditLog.objects.select_related("actor", "branch").order_by("-timestamp")

    employee = (request.GET.get("employee") or "").strip()
    action = (request.GET.get("action") or "").strip()
    reason = (request.GET.get("reason") or "").strip()
    branch_id = request.GET.get("branch")
    start_date = request.GET.get("start_date")
    end_date = request.GET.get("end_date")

    if employee:
        logs = logs.filter(
            Q(actor_employee_id__icontains=employee)
            | Q(actor_username__icontains=employee)
            | Q(actor__username__icontains=employee)
        )
    if action:
        logs = logs.filter(action=action)
    if reason:
        logs = logs.filter(reason__icontains=reason)
    if branch_id:
        logs = logs.filter(branch_id=branch_id)
    if start_date:
        logs = logs.filter(timestamp__date__gte=start_date)
    if end_date:
        logs = logs.filter(timestamp__date__lte=end_date)

    page_obj = Paginator(logs, 50).get_page(request.GET.get("page"))
    actions = AuditLog.objects.order_by("action").values_list("action", flat=True).distinct()
    branches = Branch.objects.all().order_by("name")

    return render(
        request,
        "inventory/audit_log.html",
        {
            "logs": page_obj.object_list,
            "page_obj": page_obj,
            "actions": actions,
            "branches": branches,
            "employee": employee,
            "selected_action": action,
            "selected_reason": reason,
            "selected_branch": int(branch_id) if branch_id and branch_id.isdigit() else None,
            "start_date": start_date,
            "end_date": end_date,
        },
    )


@login_required
def ticket_list(request):
    tech = is_tech_user(request.user)
    status_filter = (request.GET.get("status") or "").strip()
    branch_filter_raw = (request.GET.get("branch") or "").strip()

    tickets = Ticket.objects.select_related("branch", "reporter", "assignee").order_by("-created_at")
    if not tech:
        tickets = tickets.filter(reporter=request.user)
    if status_filter and status_filter in Ticket.Status.values:
        tickets = tickets.filter(status=status_filter)
    if branch_filter_raw.isdigit():
        tickets = tickets.filter(branch_id=int(branch_filter_raw))

    page_obj = Paginator(tickets, 25).get_page(request.GET.get("page"))
    return render(
        request,
        "inventory/ticket_list.html",
        {
            "tickets": page_obj.object_list,
            "page_obj": page_obj,
            "status_choices": Ticket.Status.choices,
            "selected_status": status_filter,
            "selected_branch": int(branch_filter_raw) if branch_filter_raw.isdigit() else None,
            "branch_choices": Branch.objects.all().order_by("name"),
            "is_tech_user": tech,
        },
    )


@login_required
def ticket_create(request):
    if not request.user.is_staff and not is_tech_user(request.user):
        return HttpResponseForbidden("Only staff users can create tickets.")
    profile_branch = _user_branch(request.user)
    selectable_branches = Branch.objects.all().order_by("name") if is_tech_user(request.user) else [profile_branch] if profile_branch else []
    if request.method == "POST":
        title = (request.POST.get("title") or "").strip()
        description = (request.POST.get("description") or "").strip()
        priority = (request.POST.get("priority") or Ticket.Priority.MEDIUM).strip()
        screenshot = request.FILES.get("screenshot")
        branch_raw = (request.POST.get("branch") or "").strip()
        branch = Branch.objects.filter(id=int(branch_raw)).first() if branch_raw.isdigit() else None
        if not branch and profile_branch:
            branch = profile_branch

        if not title:
            messages.error(request, "Ticket title is required.")
            return redirect("ticket_create")
        if not description:
            messages.error(request, "Ticket description is required.")
            return redirect("ticket_create")
        if priority not in Ticket.Priority.values:
            priority = Ticket.Priority.MEDIUM

        ticket = Ticket.objects.create(
            title=title,
            description=description,
            branch=branch,
            priority=priority,
            screenshot=screenshot,
            reporter=request.user,
            assignee=_default_ticket_assignee(),
            status=Ticket.Status.NEW,
        )
        log_audit_event(
            actor=request.user,
            action="ticket.create",
            reason="ticket_created",
            object_type="Ticket",
            object_id=ticket.id,
            branch=branch or _audit_branch_for_user(request.user),
            before={},
            after={
                "status": ticket.status,
                "title": ticket.title,
                "priority": ticket.priority,
                "branch": ticket.branch.name if ticket.branch else "",
                "assignee": ticket.assignee.username if ticket.assignee else "",
            },
        )
        messages.success(request, f"Ticket #{ticket.id} created successfully.")
        return redirect("ticket_detail", ticket_id=ticket.id)

    return render(
        request,
        "inventory/ticket_create.html",
        {
            "priority_choices": Ticket.Priority.choices,
            "branch_choices": selectable_branches,
            "default_branch_id": profile_branch.id if profile_branch else "",
        },
    )


@login_required
def ticket_detail(request, ticket_id: int):
    tech = is_tech_user(request.user)
    queryset = Ticket.objects.select_related("branch", "reporter", "assignee")
    if not tech:
        queryset = queryset.filter(reporter=request.user)
    ticket = get_object_or_404(queryset, id=ticket_id)

    if request.method == "POST":
        if not tech:
            return HttpResponseForbidden("Only technical support can update ticket status.")

        new_status = (request.POST.get("status") or "").strip()
        new_notes = (request.POST.get("internal_notes") or "").strip()
        updates = []
        before_status = ticket.status
        before_notes = ticket.internal_notes

        if new_status and new_status in Ticket.Status.values and new_status != ticket.status:
            if not ticket.can_transition_to(new_status):
                messages.error(
                    request,
                    f"Invalid transition from {ticket.get_status_display()} to "
                    f"{Ticket.Status(new_status).label}.",
                )
                return redirect("ticket_detail", ticket_id=ticket.id)
            ticket.status = new_status
            updates.append("status")

        if new_notes != ticket.internal_notes:
            ticket.internal_notes = new_notes
            updates.append("internal_notes")

        if updates:
            ticket.save(update_fields=updates + ["updated_at"])
            if "status" in updates:
                log_audit_event(
                    actor=request.user,
                    action="ticket.status_change",
                    reason="ticket_status_updated",
                    object_type="Ticket",
                    object_id=ticket.id,
                    branch=_audit_branch_for_user(request.user),
                    before={"status": before_status, "internal_notes": before_notes},
                    after={"status": ticket.status, "internal_notes": ticket.internal_notes},
                )
            messages.success(request, "Ticket updated.")
        else:
            messages.info(request, "No changes detected.")
        return redirect("ticket_detail", ticket_id=ticket.id)

    return render(
        request,
        "inventory/ticket_detail.html",
        {
            "ticket": ticket,
            "status_choices": Ticket.Status.choices,
            "priority_choices": Ticket.Priority.choices,
            "is_tech_user": tech,
        },
    )


@login_required
def transfer_list(request):
    transfers = (
        TransferRequest.objects.select_related(
            "part",
            "source_branch",
            "destination_branch",
            "requested_by",
            "approved_by",
            "driver",
            "received_by",
        )
        .order_by("-created_at")
    )
    transfers = _transfer_scope_for_user(transfers, request.user)
    if is_admin_user(request.user):
        active_branch = _active_branch_for_request(request)
        if active_branch is not None:
            transfers = transfers.filter(
                Q(source_branch=active_branch) | Q(destination_branch=active_branch)
            )

    status_filter = (request.GET.get("status") or "").strip().lower()
    if status_filter in TransferRequest.Status.values:
        transfers = transfers.filter(status=status_filter)

    page_obj = Paginator(transfers, 30).get_page(request.GET.get("page"))
    return render(
        request,
        "inventory/transfers_list.html",
        {
            "page_obj": page_obj,
            "transfers": page_obj.object_list,
            "active_branch": _user_branch(request.user),
            "is_admin": is_admin_user(request.user),
            "is_manager": is_manager(request.user),
            "status_filter": status_filter,
            "status_choices": TransferRequest.Status.choices,
        },
    )


@login_required
def transfer_pick_list(request, transfer_id: int):
    transfer = get_object_or_404(
        TransferRequest.objects.select_related(
            "part",
            "source_branch",
            "destination_branch",
            "requested_by",
        ),
        id=transfer_id,
    )
    if not is_admin_user(request.user):
        transfer_scope = _transfer_scope_for_user(TransferRequest.objects.filter(id=transfer_id), request.user)
        if not transfer_scope.exists():
            return HttpResponseForbidden("You do not have access to this transfer.")
    location_hints = list(
        StockLocation.objects.select_related("location")
        .filter(part=transfer.part, branch=transfer.source_branch, quantity__gt=0)
        .order_by("-quantity", "location__code")
    )
    return render(
        request,
        "inventory/transfer_pick_list.html",
        {
            "transfer": transfer,
            "location_hints": location_hints,
            "print_mode": request.GET.get("print") == "1",
        },
    )


@login_required
@active_branch_required
def transfer_create(request):
    if not _can_request_transfer(request.user):
        return HttpResponseForbidden("You do not have permission to create transfer requests.")
    if _blocked_by_view_only_acl(request.user, TransferRequest):
        return HttpResponseForbidden("Your account is view-only for transfer actions.")

    is_admin = is_admin_user(request.user)
    allowed_branches = Branch.objects.all().order_by("name")
    profile_branch = _user_branch(request.user)

    if request.method == "POST":
        part = Part.objects.filter(id=request.POST.get("part_id")).first()
        try:
            quantity = int(request.POST.get("quantity") or 0)
        except (TypeError, ValueError):
            quantity = 0

        notes = (request.POST.get("notes") or "").strip()
        driver_id = (request.POST.get("driver_id") or "").strip()
        driver = User.objects.filter(id=int(driver_id)).first() if driver_id.isdigit() else None

        if is_admin:
            source_branch_id = (request.POST.get("from_branch") or "").strip()
            source_branch = (
                allowed_branches.filter(id=int(source_branch_id)).first()
                if source_branch_id.isdigit()
                else None
            )
        else:
            source_branch = request.active_branch

        destination_branch_id = (request.POST.get("to_branch") or "").strip()
        destination_branch = (
            allowed_branches.filter(id=int(destination_branch_id)).first()
            if destination_branch_id.isdigit()
            else None
        )

        if not part:
            messages.error(request, "Part is required.")
        elif quantity <= 0:
            messages.error(request, "Quantity must be greater than 0.")
        elif not source_branch or not destination_branch:
            messages.error(request, "From and to branches are required.")
        elif source_branch.id == destination_branch.id:
            messages.error(request, "Source and destination branches must be different.")
        elif not is_admin and profile_branch and source_branch.id != profile_branch.id:
            messages.error(request, "You can only request transfers from your own branch.")
        else:
            transfer = TransferRequest.objects.create(
                part=part,
                quantity=quantity,
                source_branch=source_branch,
                destination_branch=destination_branch,
                requested_by=request.user,
                driver=driver,
                notes=notes,
            )
            log_audit_event(
                actor=request.user,
                action="transfer.request",
                reason=notes or "manual_transfer_request",
                object_type="TransferRequest",
                object_id=transfer.id,
                branch=source_branch,
                before={},
                after={
                    "part_id": part.id,
                    "quantity": quantity,
                    "source_branch_id": source_branch.id,
                    "destination_branch_id": destination_branch.id,
                    "driver_id": driver.id if driver else None,
                    "status": transfer.status,
                },
            )
            messages.success(request, "Transfer request created.")
            return redirect("transfer_list")

    return render(
        request,
        "inventory/transfer_create_general.html",
        {
            "is_admin": is_admin,
            "parts": Part.objects.order_by("part_number"),
            "branches": allowed_branches,
            "default_source_branch": request.active_branch if not is_admin else None,
            "drivers": User.objects.filter(is_active=True).order_by("username"),
        },
    )


@login_required
@active_branch_required
def transfer_create_from_stock(request, stock_id: int):
    stock = get_object_or_404(Stock.objects.select_related("part", "branch"), id=stock_id)

    profile = _get_or_create_profile(request.user)
    is_admin = is_admin_user(request.user)
    if not _can_request_transfer(request.user):
        return HttpResponseForbidden("You do not have permission to create transfer requests.")
    if _blocked_by_view_only_acl(request.user, TransferRequest):
        return HttpResponseForbidden("Your account is view-only for transfer actions.")

    if not is_admin and not profile.branch:
        messages.error(request, "Your account has no branch assigned.")
        return redirect("part_search")

    destination_branches = Branch.objects.all().order_by("name") if is_admin else [profile.branch] if profile.branch else []

    if request.method == "POST":
        try:
            quantity = int(request.POST.get("quantity", "0"))
        except (TypeError, ValueError):
            quantity = 0

        notes = (request.POST.get("notes") or "").strip()
        destination_branch = profile.branch

        if is_admin:
            destination_branch_id = request.POST.get("destination_branch")
            destination_branch = Branch.objects.filter(id=destination_branch_id).first()

        if quantity <= 0:
            messages.error(request, "Quantity must be greater than 0.")
            return redirect("transfer_create_from_stock", stock_id=stock.id)
        if not notes:
            messages.error(request, "Reason is required to create a transfer request.")
            return redirect("transfer_create_from_stock", stock_id=stock.id)

        if destination_branch is None:
            messages.error(request, "Destination branch is required.")
            return redirect("transfer_create_from_stock", stock_id=stock.id)

        if destination_branch.id == stock.branch_id:
            messages.error(request, "Source and destination branches must be different.")
            return redirect("transfer_create_from_stock", stock_id=stock.id)

        transfer = TransferRequest.objects.create(
            part=stock.part,
            quantity=quantity,
            source_branch=stock.branch,
            destination_branch=destination_branch,
            requested_by=request.user,
            notes=notes,
        )
        log_audit_event(
            actor=request.user,
            action="transfer.request",
            reason=notes,
            object_type="TransferRequest",
            object_id=transfer.id,
            branch=destination_branch,
            before={},
            after={
                "part_id": stock.part_id,
                "quantity": quantity,
                "source_branch_id": stock.branch_id,
                "destination_branch_id": destination_branch.id,
                "status": transfer.status,
            },
        )
        messages.success(request, "Transfer request created.")
        return redirect("transfer_list")

    return render(
        request,
        "inventory/transfer_create.html",
        {
            "stock": stock,
            "destination_branches": destination_branches,
            "is_admin": is_admin,
            "active_branch": request.active_branch,
        },
    )


@login_required
@manager_required
def transfer_approvals(request):
    pending = (
        TransferRequest.objects.select_related("part", "source_branch", "destination_branch", "requested_by")
        .filter(status=TransferRequest.Status.REQUESTED)
        .order_by("created_at")
    )
    if not is_admin_user(request.user):
        branch = _user_branch(request.user)
        if not branch:
            pending = pending.none()
        else:
            pending = pending.filter(source_branch=branch)

    page_obj = Paginator(pending, 30).get_page(request.GET.get("page"))
    return render(
        request,
        "inventory/transfer_approvals.html",
        {
            "page_obj": page_obj,
            "transfers": page_obj.object_list,
        },
    )


@login_required
@manager_required
@require_POST
@active_branch_required
def transfer_approve(request, transfer_id: int):
    transfer = get_object_or_404(
        TransferRequest.objects.select_related("source_branch", "part"),
        id=transfer_id,
    )
    if not _can_approve_transfer(request.user, transfer):
        return HttpResponseForbidden("You cannot approve this transfer.")
    if _blocked_by_view_only_acl(request.user, TransferRequest):
        return HttpResponseForbidden("Your account is view-only for transfer actions.")
    if transfer.source_branch_id != request.active_branch.id:
        messages.error(request, "The transfer source branch must match the active branch.")
        return redirect(_safe_next_url(request, "transfer_approvals"))
    reason = (request.POST.get("reason") or "").strip()
    if not reason:
        messages.error(request, "Reason is required for transfer approval.")
        return redirect(_safe_next_url(request, "transfer_approvals"))

    with transaction.atomic():
        locked_transfer = (
            TransferRequest.objects.select_for_update().select_related("source_branch", "part").filter(id=transfer_id).first()
        )
        if not locked_transfer:
            messages.error(request, "Transfer not found.")
            return redirect("transfer_approvals")

        if locked_transfer.status != TransferRequest.Status.REQUESTED:
            messages.warning(request, "Transfer is no longer pending.")
            return redirect(_safe_next_url(request, "transfer_approvals"))

        source_stock = (
            Stock.objects.select_for_update()
            .select_related("part", "branch")
            .filter(part=locked_transfer.part, branch=locked_transfer.source_branch)
            .first()
        )
        if not source_stock:
            messages.error(request, "Source stock record is missing.")
            return redirect(_safe_next_url(request, "transfer_approvals"))

        available_qty = _available_stock_quantity(source_stock, exclude_transfer_id=locked_transfer.id)
        if available_qty < locked_transfer.quantity:
            messages.error(
                request,
                f"Insufficient available stock to reserve. Available: {available_qty}.",
            )
            return redirect(_safe_next_url(request, "transfer_approvals"))

        before_snapshot = {
            "status": locked_transfer.status,
            "reserved_quantity": locked_transfer.reserved_quantity,
        }
        locked_transfer.status = TransferRequest.Status.APPROVED
        locked_transfer.reserved_quantity = locked_transfer.quantity
        locked_transfer.approved_by = request.user
        locked_transfer.approved_at = timezone.now()
        locked_transfer.rejected_by = None
        locked_transfer.rejection_reason = ""
        locked_transfer.rejected_at = None
        locked_transfer.save(
            update_fields=[
                "status",
                "reserved_quantity",
                "approved_by",
                "approved_at",
                "rejected_by",
                "rejection_reason",
                "rejected_at",
            ]
        )
        log_audit_event(
            actor=request.user,
            action="transfer.approve",
            reason=reason,
            object_type="TransferRequest",
            object_id=locked_transfer.id,
            branch=locked_transfer.source_branch,
            before=before_snapshot,
            after={
                "status": locked_transfer.status,
                "reserved_quantity": locked_transfer.reserved_quantity,
                "approved_by": request.user.username,
            },
        )

    messages.success(request, "Transfer approved and stock reserved.")
    return redirect(_safe_next_url(request, "transfer_approvals"))


@login_required
@manager_required
@require_POST
@active_branch_required
def transfer_reject(request, transfer_id: int):
    transfer = get_object_or_404(TransferRequest, id=transfer_id)
    if not _can_approve_transfer(request.user, transfer):
        return HttpResponseForbidden("You cannot reject this transfer.")
    if _blocked_by_view_only_acl(request.user, TransferRequest):
        return HttpResponseForbidden("Your account is view-only for transfer actions.")
    if transfer.source_branch_id != request.active_branch.id:
        messages.error(request, "The transfer source branch must match the active branch.")
        return redirect(_safe_next_url(request, "transfer_approvals"))

    reason = (request.POST.get("reason") or "").strip()
    if not reason:
        messages.error(request, "Reason is required for transfer rejection.")
        return redirect(_safe_next_url(request, "transfer_approvals"))

    with transaction.atomic():
        locked_transfer = TransferRequest.objects.select_for_update().filter(id=transfer_id).first()
        if not locked_transfer:
            messages.error(request, "Transfer not found.")
            return redirect("transfer_approvals")

        if locked_transfer.status not in {TransferRequest.Status.REQUESTED, TransferRequest.Status.APPROVED}:
            messages.warning(request, "Transfer cannot be rejected in its current status.")
            return redirect(_safe_next_url(request, "transfer_approvals"))

        before_snapshot = {
            "status": locked_transfer.status,
            "reserved_quantity": locked_transfer.reserved_quantity,
            "rejection_reason": locked_transfer.rejection_reason,
        }
        locked_transfer.status = TransferRequest.Status.REJECTED
        locked_transfer.reserved_quantity = 0
        locked_transfer.rejected_by = request.user
        locked_transfer.rejected_at = timezone.now()
        locked_transfer.rejection_reason = reason
        locked_transfer.save(
            update_fields=[
                "status",
                "reserved_quantity",
                "rejected_by",
                "rejected_at",
                "rejection_reason",
            ]
        )
        log_audit_event(
            actor=request.user,
            action="transfer.reject",
            reason=reason,
            object_type="TransferRequest",
            object_id=locked_transfer.id,
            branch=locked_transfer.source_branch,
            before=before_snapshot,
            after={
                "status": locked_transfer.status,
                "reserved_quantity": locked_transfer.reserved_quantity,
                "rejected_by": request.user.username,
                "rejection_reason": locked_transfer.rejection_reason,
            },
        )

    messages.info(request, "Transfer rejected and reservation released.")
    return redirect(_safe_next_url(request, "transfer_approvals"))


@login_required
def transfer_driver_tasks(request):
    transfers = (
        TransferRequest.objects.select_related("part", "source_branch", "destination_branch", "driver")
        .filter(status__in=[TransferRequest.Status.APPROVED, TransferRequest.Status.PICKED_UP])
        .order_by("created_at")
    )

    if not is_admin_user(request.user):
        transfers = _transfer_scope_for_user(transfers, request.user)

    page_obj = Paginator(transfers, 30).get_page(request.GET.get("page"))
    return render(
        request,
        "inventory/transfer_driver_tasks.html",
        {
            "page_obj": page_obj,
            "transfers": page_obj.object_list,
            "is_admin": is_admin_user(request.user),
        },
    )


@login_required
@require_POST
def transfer_mark_picked_up(request, transfer_id: int):
    transfer = get_object_or_404(TransferRequest.objects.select_related("source_branch"), id=transfer_id)

    user_branch = _user_branch(request.user)
    if not is_admin_user(request.user) and (not user_branch or user_branch.id != transfer.source_branch_id):
        return HttpResponseForbidden("You cannot mark pickup for this transfer.")
    if _blocked_by_view_only_acl(request.user, TransferRequest):
        return HttpResponseForbidden("Your account is view-only for transfer actions.")
    reason = (request.POST.get("reason") or "").strip()
    if not reason:
        messages.error(request, "Reason is required for pickup confirmation.")
        return redirect(_safe_next_url(request, "transfer_driver_tasks"))

    with transaction.atomic():
        locked_transfer = TransferRequest.objects.select_for_update().filter(id=transfer_id).first()
        if not locked_transfer:
            messages.error(request, "Transfer not found.")
            return redirect("transfer_driver_tasks")

        if locked_transfer.status != TransferRequest.Status.APPROVED:
            messages.warning(request, "Transfer is not in approved status.")
            return redirect(_safe_next_url(request, "transfer_driver_tasks"))

        locked_transfer.status = TransferRequest.Status.PICKED_UP
        locked_transfer.driver = request.user
        locked_transfer.picked_up_at = timezone.now()
        locked_transfer.save(update_fields=["status", "driver", "picked_up_at"])
        log_audit_event(
            actor=request.user,
            action="transfer.pickup",
            reason=reason,
            object_type="TransferRequest",
            object_id=locked_transfer.id,
            branch=locked_transfer.source_branch,
            before={"status": TransferRequest.Status.APPROVED},
            after={
                "status": locked_transfer.status,
                "driver": request.user.username,
            },
        )

    messages.success(request, "Transfer marked as picked up.")
    return redirect(_safe_next_url(request, "transfer_driver_tasks"))


@login_required
@require_POST
def transfer_mark_delivered(request, transfer_id: int):
    transfer = get_object_or_404(TransferRequest.objects.select_related("driver"), id=transfer_id)

    if not is_admin_user(request.user) and transfer.driver_id != request.user.id:
        return HttpResponseForbidden("Only assigned driver can mark delivery.")
    if _blocked_by_view_only_acl(request.user, TransferRequest):
        return HttpResponseForbidden("Your account is view-only for transfer actions.")
    reason = (request.POST.get("reason") or "").strip()
    if not reason:
        messages.error(request, "Reason is required for delivery confirmation.")
        return redirect(_safe_next_url(request, "transfer_driver_tasks"))

    with transaction.atomic():
        locked_transfer = TransferRequest.objects.select_for_update().filter(id=transfer_id).first()
        if not locked_transfer:
            messages.error(request, "Transfer not found.")
            return redirect("transfer_driver_tasks")

        if locked_transfer.status != TransferRequest.Status.PICKED_UP:
            messages.warning(request, "Transfer is not in picked-up status.")
            return redirect(_safe_next_url(request, "transfer_driver_tasks"))

        locked_transfer.status = TransferRequest.Status.DELIVERED
        locked_transfer.delivered_at = timezone.now()
        locked_transfer.save(update_fields=["status", "delivered_at"])
        log_audit_event(
            actor=request.user,
            action="transfer.deliver",
            reason=reason,
            object_type="TransferRequest",
            object_id=locked_transfer.id,
            branch=locked_transfer.destination_branch,
            before={"status": TransferRequest.Status.PICKED_UP},
            after={
                "status": locked_transfer.status,
                "driver": request.user.username,
            },
        )

    messages.success(request, "Transfer marked as delivered.")
    return redirect(_safe_next_url(request, "transfer_driver_tasks"))


def _receive_transfer_quantity(
    *,
    request,
    transfer_id: int,
    quantity: int,
    reason: str,
    allow_over_receive: bool = False,
) -> tuple[TransferRequest, int]:
    qty = int(quantity or 0)
    if qty <= 0:
        raise ValidationError("Receive quantity must be greater than zero.")

    locked_transfer = (
        TransferRequest.objects.select_for_update()
        .select_related("source_branch", "destination_branch", "part")
        .filter(id=transfer_id)
        .first()
    )
    if not locked_transfer:
        raise ValidationError("Transfer not found.")
    if locked_transfer.status not in {TransferRequest.Status.DELIVERED, TransferRequest.Status.RECEIVED}:
        raise ValidationError("Transfer is not ready for receiving.")
    if locked_transfer.status == TransferRequest.Status.RECEIVED:
        raise ValidationError("Transfer is already fully received.")

    remaining_before = locked_transfer.remaining_quantity
    if remaining_before <= 0:
        raise ValidationError("No remaining quantity to receive.")

    if qty > remaining_before and not allow_over_receive:
        raise ValidationError(f"Cannot receive more than remaining quantity ({remaining_before}).")

    moved_qty = qty if allow_over_receive else min(qty, remaining_before)
    source_stock = (
        Stock.objects.select_for_update()
        .filter(part=locked_transfer.part, branch=locked_transfer.source_branch)
        .first()
    )
    if not source_stock:
        raise ValidationError("Source stock record is missing.")
    if source_stock.quantity < moved_qty:
        raise ValidationError(f"Source branch stock is below requested receive quantity ({source_stock.quantity} available).")

    destination_stock, _ = Stock.objects.select_for_update().get_or_create(
        part=locked_transfer.part,
        branch=locked_transfer.destination_branch,
        defaults={"quantity": 0},
    )

    source_before = source_stock.quantity
    destination_before = destination_stock.quantity
    transfer_before = {
        "status": locked_transfer.status,
        "reserved_quantity": locked_transfer.reserved_quantity,
        "received_quantity": locked_transfer.received_quantity,
    }

    ensure_stock_locations_seeded_from_branch_stock(source_stock)
    remove_stock_from_locations(
        part=locked_transfer.part,
        branch=locked_transfer.source_branch,
        quantity=moved_qty,
        reason=reason,
        actor=request.user,
        action="transfer_out",
    )
    add_stock_to_location(
        part=locked_transfer.part,
        branch=locked_transfer.destination_branch,
        quantity=moved_qty,
        reason=reason,
        actor=request.user,
        action="transfer_in",
    )
    source_stock.refresh_from_db(fields=["quantity"])
    destination_stock.refresh_from_db(fields=["quantity"])

    locked_transfer.received_quantity = int(locked_transfer.received_quantity or 0) + moved_qty
    if locked_transfer.received_quantity >= int(locked_transfer.quantity or 0):
        locked_transfer.status = TransferRequest.Status.RECEIVED
        locked_transfer.reserved_quantity = 0
        locked_transfer.received_by = request.user
        locked_transfer.received_at = timezone.now()
        update_fields = ["received_quantity", "status", "reserved_quantity", "received_by", "received_at"]
    else:
        locked_transfer.status = TransferRequest.Status.DELIVERED
        locked_transfer.reserved_quantity = max(int(locked_transfer.quantity or 0) - int(locked_transfer.received_quantity or 0), 0)
        update_fields = ["received_quantity", "status", "reserved_quantity"]
    locked_transfer.save(update_fields=update_fields)

    log_audit_event(
        actor=request.user,
        action="stock.adjustment",
        reason=reason,
        object_type="Stock",
        object_id=source_stock.id,
        branch=locked_transfer.source_branch,
        before={
            "quantity": source_before,
            "part_number": locked_transfer.part.part_number,
            "reason": "transfer_receive_out",
            "transfer_id": locked_transfer.id,
            "receive_qty": moved_qty,
        },
        after={
            "quantity": source_stock.quantity,
            "part_number": locked_transfer.part.part_number,
            "reason": "transfer_receive_out",
            "transfer_id": locked_transfer.id,
            "receive_qty": moved_qty,
        },
    )
    log_audit_event(
        actor=request.user,
        action="stock.adjustment",
        reason=reason,
        object_type="Stock",
        object_id=destination_stock.id,
        branch=locked_transfer.destination_branch,
        before={
            "quantity": destination_before,
            "part_number": locked_transfer.part.part_number,
            "reason": "transfer_receive_in",
            "transfer_id": locked_transfer.id,
            "receive_qty": moved_qty,
        },
        after={
            "quantity": destination_stock.quantity,
            "part_number": locked_transfer.part.part_number,
            "reason": "transfer_receive_in",
            "transfer_id": locked_transfer.id,
            "receive_qty": moved_qty,
        },
    )
    log_audit_event(
        actor=request.user,
        action="transfer.receive",
        reason=reason,
        object_type="TransferRequest",
        object_id=locked_transfer.id,
        branch=locked_transfer.destination_branch,
        before=transfer_before,
        after={
            "status": locked_transfer.status,
            "reserved_quantity": locked_transfer.reserved_quantity,
            "received_quantity": locked_transfer.received_quantity,
            "received_by": request.user.username,
            "receive_qty": moved_qty,
        },
    )
    return locked_transfer, moved_qty


@login_required
def transfer_receive_list(request):
    delivered_transfers = (
        TransferRequest.objects.select_related("part", "source_branch", "destination_branch", "driver")
        .filter(status__in=[TransferRequest.Status.DELIVERED, TransferRequest.Status.RECEIVED])
        .order_by("created_at")
    )

    if not is_admin_user(request.user):
        branch = _user_branch(request.user)
        if not branch:
            delivered_transfers = delivered_transfers.none()
        else:
            delivered_transfers = delivered_transfers.filter(destination_branch=branch)

    page_obj = Paginator(delivered_transfers, 30).get_page(request.GET.get("page"))
    return render(
        request,
        "inventory/transfer_receive.html",
        {
            "page_obj": page_obj,
            "transfers": page_obj.object_list,
        },
    )


@login_required
@require_POST
@active_branch_required
def transfer_confirm_receive(request, transfer_id: int):
    transfer = get_object_or_404(
        TransferRequest.objects.select_related("source_branch", "destination_branch", "part"),
        id=transfer_id,
    )
    user_branch = _user_branch(request.user)
    if not is_admin_user(request.user) and (not user_branch or user_branch.id != transfer.destination_branch_id):
        return HttpResponseForbidden("You cannot confirm receiving for this transfer.")
    if _blocked_by_view_only_acl(request.user, TransferRequest) or _blocked_by_view_only_acl(request.user, Stock):
        return HttpResponseForbidden("Your account is view-only for receiving actions.")
    if transfer.destination_branch_id != request.active_branch.id:
        messages.error(request, "The transfer destination branch must match the active branch.")
        return redirect(_safe_next_url(request, "transfer_receive_list"))
    reason = (request.POST.get("reason") or "").strip()
    if not reason:
        messages.error(request, "Reason is required to confirm receiving.")
        return redirect(_safe_next_url(request, "transfer_receive_list"))
    qty_raw = (request.POST.get("receive_qty") or "").strip()
    try:
        requested_qty = int(qty_raw) if qty_raw else transfer.remaining_quantity
    except (TypeError, ValueError):
        requested_qty = transfer.remaining_quantity
    manager_override = (request.POST.get("manager_override") or "").strip() in {"1", "true", "on"}
    allow_over_receive = bool(manager_override and is_manager(request.user))

    try:
        with transaction.atomic():
            locked_transfer, moved_qty = _receive_transfer_quantity(
                request=request,
                transfer_id=transfer_id,
                quantity=requested_qty,
                reason=reason,
                allow_over_receive=allow_over_receive,
            )
    except (ValidationError, ValueError) as exc:
        messages.error(request, str(exc))
        return redirect(_safe_next_url(request, "transfer_receive_list"))

    if locked_transfer.status == TransferRequest.Status.RECEIVED:
        messages.success(request, f"Transfer fully received ({moved_qty} item(s) moved).")
    else:
        messages.info(
            request,
            f"Partial receive saved ({moved_qty} moved). Remaining: {locked_transfer.remaining_quantity}.",
        )
    return redirect(_safe_next_url(request, "transfer_receive_list"))


def _transfer_scan_matches_part(transfer: TransferRequest, token: str) -> bool:
    query = (token or "").strip()
    if not query:
        return False
    part = transfer.part
    if not part:
        return False
    direct_codes = {
        (part.part_number or "").strip().casefold(),
        (part.barcode or "").strip().casefold(),
        (part.sku or "").strip().casefold(),
        (part.manufacturer_part_number or "").strip().casefold(),
    }
    normalized = query.casefold()
    if normalized in direct_codes:
        return True
    return PartBarcode.objects.filter(part_id=part.id, barcode__iexact=query).exists()


@login_required
@require_POST
@active_branch_required
def transfer_receive_scan(request, transfer_id: int):
    transfer = get_object_or_404(
        TransferRequest.objects.select_related("source_branch", "destination_branch", "part"),
        id=transfer_id,
    )
    user_branch = _user_branch(request.user)
    if not is_admin_user(request.user) and (not user_branch or user_branch.id != transfer.destination_branch_id):
        return HttpResponseForbidden("You cannot receive this transfer.")
    if _blocked_by_view_only_acl(request.user, TransferRequest) or _blocked_by_view_only_acl(request.user, Stock):
        return HttpResponseForbidden("Your account is view-only for receiving actions.")
    if transfer.destination_branch_id != request.active_branch.id:
        messages.error(request, "The transfer destination branch must match the active branch.")
        return redirect(_safe_next_url(request, "transfer_receive_list"))

    token = (request.POST.get("scan_code") or "").strip()
    if not token:
        messages.error(request, "Scan code is required.")
        return redirect(_safe_next_url(request, "transfer_receive_list"))
    if not _transfer_scan_matches_part(transfer, token):
        messages.error(request, f"Scanned code does not match transfer part {transfer.part.part_number}.")
        return redirect(_safe_next_url(request, "transfer_receive_list"))

    qty_raw = (request.POST.get("quantity") or "1").strip()
    try:
        qty = int(qty_raw)
    except (TypeError, ValueError):
        qty = 1
    qty = max(qty, 1)
    reason = (request.POST.get("reason") or "").strip() or "transfer_receive_scan"
    manager_override = (request.POST.get("manager_override") or "").strip() in {"1", "true", "on"}
    allow_over_receive = bool(manager_override and is_manager(request.user))

    try:
        with transaction.atomic():
            locked_transfer, moved_qty = _receive_transfer_quantity(
                request=request,
                transfer_id=transfer.id,
                quantity=qty,
                reason=reason,
                allow_over_receive=allow_over_receive,
            )
    except (ValidationError, ValueError) as exc:
        messages.error(request, str(exc))
        return redirect(_safe_next_url(request, "transfer_receive_list"))

    if locked_transfer.status == TransferRequest.Status.RECEIVED:
        messages.success(request, f"Transfer completed by scan ({moved_qty} moved).")
    else:
        messages.info(request, f"Scan received {moved_qty}. Remaining: {locked_transfer.remaining_quantity}.")
    return redirect(_safe_next_url(request, "transfer_receive_list"))


def _scan_part_candidates(token: str, branch: Branch) -> list[Part]:
    query = (token or "").strip()
    if not query:
        return []
    qs = (
        Part.objects.filter(
            Q(barcode__iexact=query)
            | Q(part_number__iexact=query)
            | Q(sku__iexact=query)
            | Q(manufacturer_part_number__iexact=query)
            | Q(barcodes__barcode__iexact=query)
            | Q(barcode__icontains=query)
            | Q(part_number__icontains=query)
            | Q(sku__icontains=query)
            | Q(manufacturer_part_number__icontains=query)
        )
        .filter(Q(stock__branch=branch) | Q(stock_locations__branch=branch))
        .distinct()
        .order_by("part_number")
    )
    return list(qs[:5])


def _scan_batch_get(request) -> dict[str, dict]:
    batch = request.session.get(SCAN_BATCH_SESSION_KEY)
    if isinstance(batch, dict):
        return batch
    return {}


def _scan_batch_set(request, batch: dict[str, dict]) -> None:
    request.session[SCAN_BATCH_SESSION_KEY] = batch
    request.session.modified = True


def _scan_batch_lines(request) -> list[dict]:
    batch = _scan_batch_get(request)
    lines = list(batch.values())
    lines.sort(key=lambda row: ((row.get("part_number") or ""), (row.get("part_name") or "")))
    return lines


def _scan_batch_push_undo(request, *, part_id: int, delta: int) -> None:
    stack = request.session.get(SCAN_BATCH_UNDO_SESSION_KEY, [])
    if not isinstance(stack, list):
        stack = []
    stack.append({"part_id": int(part_id), "delta": int(delta)})
    request.session[SCAN_BATCH_UNDO_SESSION_KEY] = stack[-100:]
    request.session.modified = True


def _scan_batch_pop_undo(request) -> dict | None:
    stack = request.session.get(SCAN_BATCH_UNDO_SESSION_KEY, [])
    if not isinstance(stack, list) or not stack:
        return None
    event = stack.pop()
    request.session[SCAN_BATCH_UNDO_SESSION_KEY] = stack
    request.session.modified = True
    return event


def _scan_batch_apply_delta(
    request,
    *,
    part: Part,
    delta: int,
    record_undo: bool = False,
) -> int:
    batch = _scan_batch_get(request)
    key = str(part.id)
    line = batch.get(
        key,
        {
            "part_id": part.id,
            "part_number": part.part_number,
            "part_name": part.name,
            "quantity": 0,
        },
    )
    current_qty = int(line.get("quantity") or 0)
    next_qty = max(current_qty + int(delta), 0)
    if next_qty <= 0:
        batch.pop(key, None)
    else:
        line["quantity"] = next_qty
        batch[key] = line
    _scan_batch_set(request, batch)
    if record_undo and delta != 0:
        _scan_batch_push_undo(request, part_id=part.id, delta=int(delta))
    return next_qty


def _scan_batch_clear(request) -> None:
    request.session.pop(SCAN_BATCH_SESSION_KEY, None)
    request.session.pop(SCAN_BATCH_UNDO_SESSION_KEY, None)
    request.session.pop(SCAN_REPEAT_GUARD_SESSION_KEY, None)
    request.session.modified = True


def _scan_repeat_needs_confirmation(request, *, session_key: str, token: str) -> bool:
    now_ts = timezone.now().timestamp()
    guard = request.session.get(session_key, {})
    if not isinstance(guard, dict):
        guard = {}

    last_token = guard.get("token")
    last_ts = float(guard.get("ts") or 0)
    awaiting_confirm = bool(guard.get("awaiting_confirm"))
    same_recent = last_token == token and (now_ts - last_ts) <= SCAN_REPEAT_WINDOW_SECONDS

    if same_recent and awaiting_confirm:
        request.session[session_key] = {"token": token, "ts": now_ts, "awaiting_confirm": False}
        request.session.modified = True
        return False

    if same_recent:
        request.session[session_key] = {"token": token, "ts": now_ts, "awaiting_confirm": True}
        request.session.modified = True
        return True

    request.session[session_key] = {"token": token, "ts": now_ts, "awaiting_confirm": False}
    request.session.modified = True
    return False


def _pos_scan_push_undo(request, *, stock_id: int, delta: int) -> None:
    stack = request.session.get(POS_SCAN_UNDO_SESSION_KEY, [])
    if not isinstance(stack, list):
        stack = []
    stack.append({"stock_id": int(stock_id), "delta": int(delta)})
    request.session[POS_SCAN_UNDO_SESSION_KEY] = stack[-100:]
    request.session.modified = True


def _pos_scan_pop_undo(request) -> dict | None:
    stack = request.session.get(POS_SCAN_UNDO_SESSION_KEY, [])
    if not isinstance(stack, list) or not stack:
        return None
    event = stack.pop()
    request.session[POS_SCAN_UNDO_SESSION_KEY] = stack
    request.session.modified = True
    return event


@login_required
@require_POST
@active_branch_required
def scan_resolve(request):
    token = (request.POST.get("scan_code") or "").strip()
    if not token:
        return JsonResponse({"ok": False, "error": "scan_code is required."}, status=400)

    candidates = _scan_part_candidates(token, request.active_branch)
    if not candidates:
        return JsonResponse({"ok": False, "error": f"No part found for scan '{token}'."}, status=404)

    if len(candidates) > 1:
        return JsonResponse(
            {
                "ok": False,
                "ambiguous": True,
                "error": "Multiple parts matched this scan.",
                "matches": [
                    {
                        "id": part.id,
                        "part_number": part.part_number,
                        "name": part.name,
                    }
                    for part in candidates
                ],
            },
            status=409,
        )

    part = candidates[0]
    return JsonResponse(
        {
            "ok": True,
            "part": {
                "id": part.id,
                "part_number": part.part_number,
                "name": part.name,
                "barcode": part.barcode,
            },
        }
    )


def _stock_locations_branch_url(branch_id: int) -> str:
    return f"{reverse('stock_locations_view')}?{urlencode({'branch': branch_id})}"


@login_required
@manager_required
@require_POST
@active_branch_required
def stock_scan_apply(request):
    branch = request.active_branch
    if _blocked_by_view_only_acl(request.user, Stock):
        return HttpResponseForbidden("Your account is view-only for stock actions.")
    action = (request.POST.get("action") or "scan").strip().lower()
    mode = (request.POST.get("mode") or "add").strip().lower()
    redirect_url = _stock_locations_branch_url(branch.id)

    if action == "scan":
        token = (request.POST.get("scan_code") or "").strip()
        try:
            qty = int(request.POST.get("quantity") or 1)
        except (TypeError, ValueError):
            qty = 1
        qty = max(qty, 1)

        if not token:
            messages.error(request, "Scan input is required.")
            return redirect(redirect_url)

        candidates = _scan_part_candidates(token, branch)
        if not candidates:
            messages.error(request, f"No part found for scan '{token}'.")
            return redirect(redirect_url)
        if len(candidates) > 1:
            messages.warning(request, f"Multiple parts matched '{token}'. Please scan exact barcode or part number.")
            return redirect(f"{reverse('part_search')}?q={token}")

        part = candidates[0]
        if mode == "info":
            return redirect(f"{redirect_url}&q={part.part_number}")

        token_key = f"{branch.id}:{mode}:{part.id}"
        if _scan_repeat_needs_confirmation(request, session_key=SCAN_REPEAT_GUARD_SESSION_KEY, token=token_key):
            messages.warning(
                request,
                "You just scanned this item. Scan again to confirm adding another unit.",
            )
            return redirect(redirect_url)

        next_qty = _scan_batch_apply_delta(request, part=part, delta=qty, record_undo=True)
        messages.success(request, f"Scanned {part.part_number}. Batch quantity is now {next_qty}.")
        return redirect(f"{redirect_url}&scan_mode={mode}")

    if action == "undo":
        event = _scan_batch_pop_undo(request)
        if not event:
            messages.info(request, "No scan action to undo.")
            return redirect(redirect_url)
        part = Part.objects.filter(id=event.get("part_id")).first()
        if not part:
            messages.warning(request, "Could not undo: part no longer exists.")
            return redirect(redirect_url)
        reversed_qty = _scan_batch_apply_delta(
            request,
            part=part,
            delta=-int(event.get("delta") or 0),
            record_undo=False,
        )
        messages.info(request, f"Undo applied for {part.part_number}. Quantity now {reversed_qty}.")
        return redirect(redirect_url)

    if action in {"line_plus", "line_minus", "line_remove"}:
        part = Part.objects.filter(id=request.POST.get("part_id")).first()
        if not part:
            messages.error(request, "Part not found.")
            return redirect(redirect_url)
        if action == "line_plus":
            _scan_batch_apply_delta(request, part=part, delta=1, record_undo=True)
        elif action == "line_minus":
            _scan_batch_apply_delta(request, part=part, delta=-1, record_undo=True)
        else:
            current = _scan_batch_get(request).get(str(part.id), {}).get("quantity", 0)
            if current:
                _scan_batch_apply_delta(request, part=part, delta=-int(current), record_undo=True)
        return redirect(redirect_url)

    if action == "clear":
        _scan_batch_clear(request)
        messages.info(request, "Scan batch cleared.")
        return redirect(redirect_url)

    if action != "apply":
        messages.error(request, "Invalid scan action.")
        return redirect(redirect_url)

    lines = _scan_batch_lines(request)
    if not lines:
        messages.error(request, "Scan batch is empty.")
        return redirect(redirect_url)

    reason = (request.POST.get("reason") or "").strip()
    if not reason:
        messages.error(request, "Reason is required.")
        return redirect(redirect_url)

    from_location = None
    to_location = None
    if mode == "add":
        to_location = Location.objects.filter(id=request.POST.get("to_location_id"), branch=branch).first()
        if not to_location:
            messages.error(request, "Destination location is required.")
            return redirect(redirect_url)
    elif mode == "remove":
        from_location = Location.objects.filter(id=request.POST.get("from_location_id"), branch=branch).first()
        if not from_location:
            messages.error(request, "Source location is required.")
            return redirect(redirect_url)
    elif mode == "move":
        from_location = Location.objects.filter(id=request.POST.get("from_location_id"), branch=branch).first()
        to_location = Location.objects.filter(id=request.POST.get("to_location_id"), branch=branch).first()
        if not from_location or not to_location:
            messages.error(request, "Source and destination locations are required.")
            return redirect(redirect_url)
    else:
        messages.error(request, "Invalid scan mode for stock apply.")
        return redirect(redirect_url)

    parts = Part.objects.in_bulk([line["part_id"] for line in lines])
    try:
        with transaction.atomic():
            for line in lines:
                part = parts.get(line["part_id"])
                if not part:
                    raise ValueError("One of the scanned parts no longer exists.")
                qty = int(line["quantity"] or 0)
                if qty <= 0:
                    continue

                Stock.objects.select_for_update().filter(part=part, branch=branch).first()
                if mode == "add":
                    add_stock_to_location(
                        part=part,
                        branch=branch,
                        quantity=qty,
                        reason=reason,
                        actor=request.user,
                        location=to_location,
                        action="scan_add",
                    )
                elif mode == "remove":
                    remove_stock_from_locations(
                        part=part,
                        branch=branch,
                        quantity=qty,
                        reason=reason,
                        actor=request.user,
                        from_location=from_location,
                        action="scan_remove",
                    )
                else:
                    move_stock_between_locations(
                        part=part,
                        branch=branch,
                        quantity=qty,
                        from_location=from_location,
                        to_location=to_location,
                        reason=reason,
                        actor=request.user,
                        action="scan_move",
                    )
    except (ValidationError, ValueError) as exc:
        messages.error(request, str(exc))
        return redirect(f"{redirect_url}&scan_mode={mode}")

    _scan_batch_clear(request)
    messages.success(request, f"Applied scan batch ({mode}) for {len(lines)} part(s).")
    return redirect(f"{redirect_url}&scan_mode={mode}")


@login_required
@require_POST
@active_branch_required
def scan_dispatch(request):
    action = (request.POST.get("action") or "scan").strip().lower()
    mode = (request.POST.get("mode") or "info").strip().lower()
    token = (request.POST.get("scan_code") or "").strip()
    branch = request.active_branch
    if mode == "pos" and not _can_use_pos(request.user):
        return HttpResponseForbidden("Your role is not allowed to use POS scan actions.")
    if mode in {"add", "remove", "move"} and not is_manager(request.user):
        return HttpResponseForbidden("Only manager/admin can run stock scan actions.")
    qty_raw = request.POST.get("quantity") or "1"
    try:
        quantity = int(qty_raw)
    except (TypeError, ValueError):
        quantity = 1
    if quantity <= 0:
        quantity = 1

    if mode == "pos" and action == "undo":
        event = _pos_scan_pop_undo(request)
        if not event:
            messages.info(request, "No scan action to undo.")
            return redirect("pos_console")
        stock_id = str(event.get("stock_id"))
        delta = int(event.get("delta") or 0)
        cart = _get_cart(request)
        current_qty = int(cart.get(stock_id, 0))
        next_qty = max(current_qty - delta, 0)
        if next_qty > 0:
            cart[stock_id] = next_qty
        else:
            cart.pop(stock_id, None)
        request.session["cart"] = cart
        request.session.modified = True
        messages.info(request, "Last POS scan undone.")
        return redirect("pos_console")

    if not token:
        messages.error(request, "Scan input is required.")
        return redirect(_safe_next_url(request, "pos_console"))

    part_candidates = _scan_part_candidates(token, branch)
    if not part_candidates:
        if mode in {"info", "lookup", "receiving", "transfer_receive"}:
            messages.warning(request, f"No part found for scan '{token}'. Link it to an existing part.")
            return redirect(f"{reverse('barcode_unmatched')}?code={token}")
        messages.error(request, f"No part found for scan '{token}'.")
        return redirect(_safe_next_url(request, "part_search"))
    if len(part_candidates) > 1:
        messages.warning(request, f"Multiple parts matched '{token}'. Please scan exact barcode or part number.")
        return redirect(f"{reverse('part_search')}?q={token}")

    part = part_candidates[0]

    if mode == "pos":
        stock = (
            Stock.objects.select_related("part", "branch")
            .filter(part=part, branch=branch)
            .first()
        )
        if not stock:
            messages.error(request, f"{part.part_number} is not stocked in {branch.name}.")
            return redirect("pos_console")

        cart = _get_cart(request)
        stock_id = str(stock.id)
        current_qty = int(cart.get(stock_id, 0))
        available_qty = _available_stock_quantity(stock)
        scan_token = f"{branch.id}:{part.id}"
        if _scan_repeat_needs_confirmation(request, session_key=POS_SCAN_REPEAT_GUARD_SESSION_KEY, token=scan_token):
            messages.warning(
                request,
                "You just scanned this item. Scan again to confirm adding another unit.",
            )
            return redirect("pos_console")
        if current_qty + quantity > available_qty:
            messages.error(request, f"Only {available_qty} units available for {part.part_number}.")
            return redirect("pos_console")

        cart[stock_id] = current_qty + quantity
        request.session["cart"] = cart
        request.session.modified = True
        _pos_scan_push_undo(request, stock_id=stock.id, delta=quantity)
        messages.success(request, f"Scanned {part.part_number} and added {quantity} to cart.")
        return redirect(f"{reverse('pos_console')}?q={part.part_number}")

    if mode in {"add", "remove"}:
        params = {
            "branch": branch.id,
            "scan_part": part.id,
            "scan_mode": mode,
            "scan_qty": quantity,
            "scan_reason": "scan_add" if mode == "add" else "scan_remove",
            "q": part.part_number,
        }
        return redirect(f"{reverse('stock_locations_view')}?{urlencode(params)}")

    return redirect("part_insight", part_id=part.id)


@login_required
def scanner_view(request):
    return render(request, "inventory/scanner.html")


@login_required
def sell_part(request, stock_id):
    if request.method == "POST":
        return add_to_cart(request, stock_id=stock_id)
    return redirect("pos_console")


def _sales_queryset_with_filters(request):
    sales = Sale.objects.select_related("order", "part", "branch", "seller", "seller__profile").order_by("-date_sold")
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


ASSISTANT_QUERY_TYPE_CHOICES = [
    ("auto", "Auto detect"),
    ("totals", "Totals overview"),
    ("part_stock", "Part stock lookup"),
    ("top_products", "Top products"),
    ("low_stock", "Low stock"),
    ("refunds_per_employee", "Refunds per employee"),
    ("transfer_delays", "Transfer delays"),
]
# Backward-compatible alias used in templates/tests.
ASSISTANT_INTENT_CHOICES = ASSISTANT_QUERY_TYPE_CHOICES

ASSISTANT_MUTATION_KEYWORDS = ["delete", "drop", "insert", "update", "create", "modify", "remove", "truncate"]
ASSISTANT_STOCK_STOPWORDS = {
    "how",
    "many",
    "left",
    "from",
    "of",
    "for",
    "the",
    "in",
    "stock",
    "is",
    "are",
    "remaining",
    "available",
    "qty",
    "quantity",
    "show",
    "me",
    "please",
    "part",
    "item",
    "parts",
}


def _parse_iso_date(raw_value: str | None) -> date | None:
    value = (raw_value or "").strip()
    if not value:
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        return None


def _assistant_branch_scope(user, requested_branch_id: str | None) -> Branch | None:
    if is_admin_user(user):
        if requested_branch_id and requested_branch_id.isdigit():
            return Branch.objects.filter(id=int(requested_branch_id)).first()
        return None
    return _user_branch(user)


def _assistant_sales_queryset(branch_scope: Branch | None, start_date: date | None, end_date: date | None):
    sales_qs = Sale.objects.select_related("part", "branch", "seller", "seller__profile", "order")
    if branch_scope is not None:
        sales_qs = sales_qs.filter(branch=branch_scope)
    if start_date:
        sales_qs = sales_qs.filter(date_sold__date__gte=start_date)
    if end_date:
        sales_qs = sales_qs.filter(date_sold__date__lte=end_date)
    return sales_qs


def _assistant_transfer_queryset(branch_scope: Branch | None, start_date: date | None, end_date: date | None):
    transfers_qs = TransferRequest.objects.select_related("part", "source_branch", "destination_branch")
    if branch_scope is not None:
        transfers_qs = transfers_qs.filter(Q(source_branch=branch_scope) | Q(destination_branch=branch_scope))
    if start_date:
        transfers_qs = transfers_qs.filter(created_at__date__gte=start_date)
    if end_date:
        transfers_qs = transfers_qs.filter(created_at__date__lte=end_date)
    return transfers_qs


def detect_query_type(question: str) -> str | None:
    text = (question or "").lower()
    if not text:
        return None
    if any(token in text for token in ASSISTANT_MUTATION_KEYWORDS):
        return "read_only_guard"

    word_set = set(re.findall(r"[a-z0-9_]+", text))
    if any(
        token in text
        for token in [
            "part stock lookup",
            "part stock",
            "how many left",
            "left from",
            "remaining",
            "available stock",
            "stock of",
            "كم",
            "متبقي",
            "باقي",
            "مخزون",
        ]
    ):
        return "part_stock"

    # Flexible ordering for natural phrases like "give me how many oil left".
    if (
        "left" in word_set and {"how", "many"}.issubset(word_set)
    ) or (
        "remaining" in word_set and ({"how", "many"}.issubset(word_set) or "stock" in word_set)
    ) or (
        "available" in word_set and ("stock" in word_set or {"how", "many"}.issubset(word_set))
    ):
        return "part_stock"

    if any(token in text for token in ["top products", "top product", "best selling", "most sold"]):
        return "top_products"
    if any(token in text for token in ["low stock", "critical stock", "reorder"]):
        return "low_stock"
    if any(token in text for token in ["refunds per employee", "refund per employee", "refunds by employee", "refund by employee"]):
        return "refunds_per_employee"
    if any(token in text for token in ["transfer delays", "transfer delay", "delayed transfer", "logistics"]):
        return "transfer_delays"
    if any(token in text for token in ["totals", "total", "overview", "summary"]):
        return "totals"
    return None


def _assistant_extract_stock_search(question: str) -> str:
    cleaned = []
    for token in (question or "").lower().replace("?", " ").replace(",", " ").split():
        token = token.strip()
        if not token:
            continue
        if token in ASSISTANT_STOCK_STOPWORDS:
            continue
        cleaned.append(token)
    return " ".join(cleaned)[:120]


def _assistant_query_part_stock(branch_scope: Branch | None, question: str):
    search_text = _assistant_extract_stock_search(question)
    stock_qs = Stock.objects.select_related("part", "branch")
    if branch_scope is not None:
        stock_qs = stock_qs.filter(branch=branch_scope)

    filtered_qs = stock_qs
    if search_text:
        filtered_qs = filtered_qs.filter(
            Q(part__name__icontains=search_text)
            | Q(part__part_number__icontains=search_text)
            | Q(part__barcode__icontains=search_text)
        )

    reserved_qs = TransferRequest.objects.filter(
        status__in=[
            TransferRequest.Status.APPROVED,
            TransferRequest.Status.PICKED_UP,
            TransferRequest.Status.DELIVERED,
        ]
    )
    if branch_scope is not None:
        reserved_qs = reserved_qs.filter(source_branch=branch_scope)

    reserved_map = {
        (row["part_id"], row["source_branch_id"]): int(row["total_reserved"] or 0)
        for row in reserved_qs.values("part_id", "source_branch_id").annotate(
            total_reserved=Coalesce(Sum("reserved_quantity"), Value(0))
        )
    }

    rows = []
    total_on_hand = 0
    total_reserved = 0
    total_available = 0
    for stock in filtered_qs.order_by("part__name", "branch__name")[:30]:
        reserved = reserved_map.get((stock.part_id, stock.branch_id), 0)
        available = max(stock.quantity - reserved, 0)
        total_on_hand += stock.quantity
        total_reserved += reserved
        total_available += available
        rows.append(
            [
                stock.branch.name,
                stock.part.part_number,
                stock.part.name,
                stock.quantity,
                reserved,
                available,
            ]
        )

    scope_label = branch_scope.name if branch_scope else "All branches"
    search_label = search_text if search_text else "(all parts)"
    answer = (
        f"Part stock lookup for '{search_label}' in {scope_label}. "
        f"Matched rows: {len(rows)}, on-hand: {total_on_hand}, reserved: {total_reserved}, available: {total_available}."
    )
    if not rows:
        answer = (
            f"No stock rows matched '{search_label}' in {scope_label}. "
            "Try a clearer part name or part number."
        )

    return {
        "intent": "part_stock",
        "title": "Part Stock Lookup",
        "answer": answer,
        "totals_used": [
            ("Search text", search_label),
            ("Stock rows scanned", stock_qs.count()),
            ("Matched stock rows", len(rows)),
            ("Total on-hand quantity", total_on_hand),
            ("Total reserved quantity", total_reserved),
            ("Total available quantity", total_available),
        ],
        "columns": ["Branch", "Part Number", "Part Name", "On Hand", "Reserved", "Available"],
        "rows": rows,
    }


def _assistant_query_totals(branch_scope: Branch | None, start_date: date | None, end_date: date | None):
    sales_qs = _assistant_sales_queryset(branch_scope, start_date, end_date)
    orders_qs = Order.objects.all()
    if branch_scope is not None:
        orders_qs = orders_qs.filter(branch=branch_scope)
    if start_date:
        orders_qs = orders_qs.filter(created_at__date__gte=start_date)
    if end_date:
        orders_qs = orders_qs.filter(created_at__date__lte=end_date)

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
    refunded_expr = Case(
        When(is_refunded=True, then=Value(1)),
        default=Value(0),
        output_field=IntegerField(),
    )

    totals = sales_qs.aggregate(
        sales_rows=Count("id"),
        total_qty=Coalesce(Sum("quantity"), Value(0)),
        refunded_sales=Coalesce(Sum(refunded_expr), Value(0)),
        total_revenue=Coalesce(Sum(revenue_expr), Value(Decimal("0.00"))),
        total_profit=Coalesce(Sum(profit_expr), Value(Decimal("0.00"))),
    )
    order_count = orders_qs.count()
    scope_label = branch_scope.name if branch_scope else "All branches"

    return {
        "intent": "totals",
        "title": "Totals Overview",
        "answer": (
            f"Scope: {scope_label}. Orders: {order_count}, sales rows: {totals['sales_rows']}, "
            f"revenue: {totals['total_revenue']:.2f} SAR, profit: {totals['total_profit']:.2f} SAR."
        ),
        "totals_used": [
            ("Orders counted", order_count),
            ("Sales rows considered", totals["sales_rows"]),
            ("Total sold quantity", totals["total_qty"]),
            ("Refunded sales rows", totals["refunded_sales"]),
            ("Total revenue (non-refunded)", f"{totals['total_revenue']:.2f} SAR"),
            ("Total profit (non-refunded)", f"{totals['total_profit']:.2f} SAR"),
        ],
        "columns": [],
        "rows": [],
    }


def _assistant_query_top_products(branch_scope: Branch | None, start_date: date | None, end_date: date | None):
    sales_qs = _assistant_sales_queryset(branch_scope, start_date, end_date).filter(is_refunded=False, part__isnull=False)
    line_revenue_expr = ExpressionWrapper(
        F("price_at_sale") * F("quantity"),
        output_field=DecimalField(max_digits=16, decimal_places=2),
    )
    grouped = list(
        sales_qs.values("part__part_number", "part__name")
        .annotate(
            qty=Coalesce(Sum("quantity"), Value(0)),
            revenue=Coalesce(Sum(line_revenue_expr), Value(Decimal("0.00"))),
        )
        .order_by("-qty", "-revenue")[:10]
    )

    rows = [
        [
            row["part__part_number"] or "-",
            row["part__name"] or "-",
            int(row["qty"] or 0),
            f"{Decimal(row['revenue'] or Decimal('0.00')):.2f}",
        ]
        for row in grouped
    ]
    scope_label = branch_scope.name if branch_scope else "All branches"
    return {
        "intent": "top_products",
        "title": "Top Products",
        "answer": f"Computed top-selling products for {scope_label} using non-refunded sales only.",
        "totals_used": [
            ("Sales rows considered", sales_qs.count()),
            ("Distinct products in result", len(rows)),
            ("Scope", scope_label),
        ],
        "columns": ["Part Number", "Part Name", "Qty Sold", "Revenue (SAR)"],
        "rows": rows,
    }


def _assistant_query_low_stock(branch_scope: Branch | None):
    threshold = 5
    stock_qs = Stock.objects.select_related("part", "branch")
    if branch_scope is not None:
        stock_qs = stock_qs.filter(branch=branch_scope)

    low_qs = list(stock_qs.filter(quantity__lte=threshold).order_by("quantity", "part__name", "branch__name")[:20])
    reserved_qs = TransferRequest.objects.filter(
        status__in=[
            TransferRequest.Status.APPROVED,
            TransferRequest.Status.PICKED_UP,
            TransferRequest.Status.DELIVERED,
        ]
    )
    if branch_scope is not None:
        reserved_qs = reserved_qs.filter(source_branch=branch_scope)

    reserved_map = {
        (row["part_id"], row["source_branch_id"]): int(row["total_reserved"] or 0)
        for row in reserved_qs.values("part_id", "source_branch_id").annotate(
            total_reserved=Coalesce(Sum("reserved_quantity"), Value(0))
        )
    }

    rows = []
    for stock in low_qs:
        reserved = reserved_map.get((stock.part_id, stock.branch_id), 0)
        available = max(stock.quantity - reserved, 0)
        rows.append(
            [
                stock.branch.name,
                stock.part.part_number,
                stock.part.name,
                stock.quantity,
                reserved,
                available,
            ]
        )

    scope_label = branch_scope.name if branch_scope else "All branches"
    return {
        "intent": "low_stock",
        "title": "Low Stock",
        "answer": f"Low-stock items (threshold <= {threshold}) for {scope_label}, with reserved transfer quantities included.",
        "totals_used": [
            ("Stock rows scanned", stock_qs.count()),
            ("Low-stock rows returned", len(rows)),
            ("Threshold", threshold),
        ],
        "columns": ["Branch", "Part Number", "Part Name", "On Hand", "Reserved", "Available"],
        "rows": rows,
    }


def _assistant_query_refunds_per_employee(branch_scope: Branch | None, start_date: date | None, end_date: date | None):
    sales_qs = _assistant_sales_queryset(branch_scope, start_date, end_date).filter(is_refunded=True)
    refund_value_expr = ExpressionWrapper(
        F("price_at_sale") * F("quantity"),
        output_field=DecimalField(max_digits=16, decimal_places=2),
    )
    grouped = list(
        sales_qs.values("seller__username", "seller__profile__employee_id")
        .annotate(
            refund_count=Count("id"),
            refunded_qty=Coalesce(Sum("quantity"), Value(0)),
            refunded_value=Coalesce(Sum(refund_value_expr), Value(Decimal("0.00"))),
        )
        .order_by("-refund_count", "-refunded_qty")[:20]
    )
    rows = [
        [
            row["seller__username"] or "-",
            row["seller__profile__employee_id"] or "-",
            int(row["refund_count"] or 0),
            int(row["refunded_qty"] or 0),
            f"{Decimal(row['refunded_value'] or Decimal('0.00')):.2f}",
        ]
        for row in grouped
    ]
    scope_label = branch_scope.name if branch_scope else "All branches"
    return {
        "intent": "refunds_per_employee",
        "title": "Refunds Per Employee",
        "answer": f"Refund breakdown by employee for {scope_label}.",
        "totals_used": [
            ("Refund rows considered", sales_qs.count()),
            ("Employees in result", len(rows)),
            ("Scope", scope_label),
        ],
        "columns": ["Seller", "Employee ID", "Refund Count", "Refund Qty", "Refund Value (SAR)"],
        "rows": rows,
    }


def _assistant_query_transfer_delays(branch_scope: Branch | None, start_date: date | None, end_date: date | None):
    transfers_qs = _assistant_transfer_queryset(branch_scope, start_date, end_date)
    completed = list(transfers_qs.filter(status=TransferRequest.Status.RECEIVED, received_at__isnull=False))
    durations = []
    for transfer in completed:
        if transfer.received_at and transfer.created_at:
            durations.append((transfer.received_at - transfer.created_at).total_seconds() / 3600)

    avg_delay = (sum(durations) / len(durations)) if durations else 0.0
    now = timezone.now()
    open_transfers = list(
        transfers_qs.filter(
            status__in=[
                TransferRequest.Status.APPROVED,
                TransferRequest.Status.PICKED_UP,
                TransferRequest.Status.DELIVERED,
            ]
        ).order_by("created_at")[:20]
    )
    open_rows = []
    for transfer in open_transfers:
        age_hours = (now - transfer.created_at).total_seconds() / 3600 if transfer.created_at else 0
        open_rows.append(
            [
                transfer.id,
                transfer.part.part_number if transfer.part else "-",
                transfer.status,
                transfer.source_branch.name if transfer.source_branch else "-",
                transfer.destination_branch.name if transfer.destination_branch else "-",
                f"{age_hours:.1f}",
            ]
        )

    scope_label = branch_scope.name if branch_scope else "All branches"
    return {
        "intent": "transfer_delays",
        "title": "Transfer Delays",
        "answer": (
            f"Transfer timing summary for {scope_label}. "
            f"Average received-delay is {avg_delay:.1f} hours based on completed transfers."
        ),
        "totals_used": [
            ("Transfers considered", transfers_qs.count()),
            ("Completed transfers used in average", len(durations)),
            ("Open in-progress transfers", len(open_rows)),
            ("Average created->received delay (hours)", f"{avg_delay:.1f}"),
        ],
        "columns": ["Transfer #", "Part Number", "Status", "Source", "Destination", "Age (hours)"],
        "rows": open_rows,
    }


def _run_assistant_query(
    intent: str,
    branch_scope: Branch | None,
    start_date: date | None,
    end_date: date | None,
    question: str,
):
    if intent == "read_only_guard":
        return {
            "intent": intent,
            "title": "Read-Only Assistant",
            "answer": "This assistant is read-only and cannot modify data. Ask for analytics instead.",
            "totals_used": [("Safety mode", "Read-only database access only")],
            "columns": [],
            "rows": [],
        }
    if intent == "top_products":
        return _assistant_query_top_products(branch_scope, start_date, end_date)
    if intent == "part_stock":
        return _assistant_query_part_stock(branch_scope, question)
    if intent == "low_stock":
        return _assistant_query_low_stock(branch_scope)
    if intent == "refunds_per_employee":
        return _assistant_query_refunds_per_employee(branch_scope, start_date, end_date)
    if intent == "transfer_delays":
        return _assistant_query_transfer_delays(branch_scope, start_date, end_date)
    return _assistant_query_totals(branch_scope, start_date, end_date)


ASSISTANT_CHAT_HISTORY_KEY = "assistant_chat_history"
ASSISTANT_CHAT_PENDING_KEY = "assistant_chat_pending_action"


def _assistant_append_chat(history: list[dict[str, str]], role: str, content: str) -> None:
    history.append({"role": role, "content": content})


def _assistant_suggestions_text(parts) -> str:
    return "\n".join([f"- {part.part_number}: {part.name}" for part in parts[:5]])


def _assistant_format_lookup_response(result: dict, *, include_locations: bool) -> str:
    rows = result.get("rows", [])
    if not rows:
        return result.get("summary", "No rows found.")

    preview = []
    for row in rows[:10]:
        if include_locations:
            preview.append(
                f"- {row['part_number']} | {row['branch']} | {row['location']} | qty={row['quantity']}"
            )
        else:
            preview.append(f"- {row['part_number']} | {row['branch']} | qty={row['quantity']}")
    suffix = "\n...and more rows." if len(rows) > 10 else ""
    return f"{result.get('summary', 'Lookup complete.')}\n" + "\n".join(preview) + suffix


def _assistant_execute_pending_action(request, pending: dict) -> str:
    action = pending.get("action")
    params = pending.get("params", {})

    if action == "add_stock":
        branch = Branch.objects.filter(id=params.get("branch_id")).first()
        location = Location.objects.filter(id=params.get("location_id")).first()
        allowed, reason_text = validate_tool_permission(
            user=request.user,
            action=action,
            branch=branch,
        )
        if not allowed:
            raise ValueError(reason_text)
        movement = add_stock(
            params.get("part_number"),
            branch,
            location,
            int(params.get("qty", 0)),
            params.get("reason", "assistant_chat_action"),
            actor=request.user,
        )
        return (
            f"Done: added {params.get('qty')} of {params.get('part_number')} "
            f"to {branch.name}/{location.code}. Movement #{movement.id}."
        )

    if action == "remove_stock":
        branch = Branch.objects.filter(id=params.get("branch_id")).first()
        location = Location.objects.filter(id=params.get("location_id")).first()
        allowed, reason_text = validate_tool_permission(
            user=request.user,
            action=action,
            branch=branch,
        )
        if not allowed:
            raise ValueError(reason_text)
        movements = remove_stock(
            params.get("part_number"),
            branch,
            location,
            int(params.get("qty", 0)),
            params.get("reason", "assistant_chat_action"),
            actor=request.user,
        )
        return (
            f"Done: removed {params.get('qty')} of {params.get('part_number')} "
            f"from {branch.name}/{location.code}. Movement rows: {len(movements)}."
        )

    if action == "move_stock":
        branch = Branch.objects.filter(id=params.get("branch_id")).first()
        from_location = Location.objects.filter(id=params.get("from_location_id")).first()
        to_location = Location.objects.filter(id=params.get("to_location_id")).first()
        allowed, reason_text = validate_tool_permission(
            user=request.user,
            action=action,
            branch=branch,
        )
        if not allowed:
            raise ValueError(reason_text)
        movement = move_stock(
            params.get("part_number"),
            branch,
            from_location,
            to_location,
            int(params.get("qty", 0)),
            params.get("reason", "assistant_chat_action"),
            actor=request.user,
        )
        return (
            f"Done: moved {params.get('qty')} of {params.get('part_number')} "
            f"from {from_location.code} to {to_location.code} in {branch.name}. Movement #{movement.id}."
        )

    if action == "create_transfer_request":
        from_branch = Branch.objects.filter(id=params.get("from_branch_id")).first()
        to_branch = Branch.objects.filter(id=params.get("to_branch_id")).first()
        allowed, reason_text = validate_tool_permission(
            user=request.user,
            action=action,
            from_branch=from_branch,
            to_branch=to_branch,
        )
        if not allowed:
            raise ValueError(reason_text)
        transfer = create_transfer_request(
            params.get("part_number"),
            from_branch,
            to_branch,
            int(params.get("qty", 0)),
            params.get("note", "assistant_transfer_request"),
            actor=request.user,
        )
        return f"Done: created transfer request #{transfer.id} for {transfer.part.part_number} x{transfer.quantity}."

    raise ValueError("Unknown pending action.")


@login_required
def analytics_assistant(request):
    profile = _get_or_create_profile(request.user)
    role = user_role(request.user)
    active_branch = _active_branch_for_request(request)
    history = request.session.get(ASSISTANT_CHAT_HISTORY_KEY, [])
    pending_action = request.session.get(ASSISTANT_CHAT_PENDING_KEY)
    ai_engine_live = assistant_llm_enabled()
    ai_engine_label = assistant_llm_engine_label()

    if not history:
        intro_lines = [
            "Chat Assistant ready. I understand English + Arabic.",
            "For writes, I always do: Draft -> Confirm -> Apply.",
        ]
        if ai_engine_live:
            intro_lines.append(f"AI engine online: {ai_engine_label}.")
        else:
            intro_lines.append("AI engine: fallback parser mode (set AI_ASSISTANT_API_KEY to enable live model).")
        history = [{"role": "assistant", "content": "\n".join(intro_lines)}]

    if request.method == "POST":
        if request.POST.get("clear_chat") == "1":
            history = []
            pending_action = None
            request.session[ASSISTANT_CHAT_HISTORY_KEY] = history
            request.session[ASSISTANT_CHAT_PENDING_KEY] = pending_action
            request.session.modified = True
            return redirect("analytics_assistant")

        message = (request.POST.get("message") or "").strip()
        if message:
            _assistant_append_chat(history, "user", message)

            llm_preface = ""
            message_for_actions = message
            llm_handled = False

            if not pending_action and ai_engine_live:
                try:
                    llm_plan = generate_assistant_plan(
                        message=message,
                        chat_history=history,
                        user_role=role,
                        active_branch_name=active_branch.name if active_branch else "",
                        branch_names=list(Branch.objects.order_by("name").values_list("name", flat=True)[:50]),
                        location_codes=(
                            list(
                                Location.objects.filter(branch=active_branch)
                                .order_by("code")
                                .values_list("code", flat=True)[:200]
                            )
                            if active_branch
                            else []
                        ),
                    )
                except AssistantLLMError:
                    llm_plan = None

                if llm_plan:
                    if llm_plan.get("mode") == "chat":
                        _assistant_append_chat(
                            history,
                            "assistant",
                            llm_plan.get("assistant_reply", "").strip() or "How can I help?",
                        )
                        llm_handled = True
                    elif llm_plan.get("mode") == "command":
                        command_text = (llm_plan.get("command_text") or "").strip()
                        if command_text:
                            message_for_actions = command_text
                            llm_preface = (llm_plan.get("assistant_reply") or "").strip()

            if llm_handled:
                pass
            elif pending_action:
                if is_confirm_message(message):
                    if pending_action.get("action") in WRITE_ACTIONS and active_branch is None:
                        response_text = "Select an active branch before applying write operations."
                    else:
                        try:
                            response_text = _assistant_execute_pending_action(request, pending_action)
                            pending_action = None
                        except Exception as exc:
                            response_text = f"Could not apply action: {exc}"
                elif is_cancel_message(message):
                    pending_action = None
                    response_text = "Pending action canceled."
                else:
                    response_text = (
                        "You have a pending write action. Reply with 'confirm' (or 'تأكيد') to apply, "
                        "or 'cancel' (or 'إلغاء') to discard."
                    )
                _assistant_append_chat(history, "assistant", response_text)
            else:
                parsed = parse_chat_message(message_for_actions)
                action = parsed["action"]
                branch_context = resolve_branch_context(user=request.user, action=action, message=message_for_actions)
                branch = branch_context.get("branch")
                from_branch = branch_context.get("from_branch")
                to_branch = branch_context.get("to_branch")

                precheck_error = ""
                if action in WRITE_ACTIONS and active_branch is None:
                    precheck_error = "Select an active branch before applying write operations."
                elif action in {"add_stock", "remove_stock", "move_stock"} and active_branch is not None:
                    if branch is None:
                        branch = active_branch
                    elif is_admin_user(request.user) and branch.id != active_branch.id:
                        precheck_error = "You can apply write actions only against the currently active branch."
                elif action == "create_transfer_request" and active_branch is not None:
                    if from_branch is None:
                        from_branch = active_branch
                    elif is_admin_user(request.user) and from_branch.id != active_branch.id:
                        precheck_error = "Transfer source branch must match the currently active branch."

                if precheck_error:
                    _assistant_append_chat(history, "assistant", precheck_error)
                else:
                    allowed, denial_reason = validate_tool_permission(
                        user=request.user,
                        action=action,
                        branch=branch,
                        from_branch=from_branch,
                        to_branch=to_branch,
                    )
                    if not allowed:
                        _assistant_append_chat(history, "assistant", denial_reason)
                    elif action == "lookup_stock":
                        part_query = (parsed.get("part_query") or message_for_actions).strip()
                        part_candidates = find_part_candidates(part_query, branch)
                        if not part_candidates:
                            _assistant_append_chat(
                                history,
                                "assistant",
                                f"No part matched '{part_query}'. Try part number or clearer name.",
                            )
                        elif len(part_candidates) > 1 and len(part_query.split()) <= 1:
                            _assistant_append_chat(
                                history,
                                "assistant",
                                (
                                    f"I found multiple parts for '{part_query}'. Which one do you mean?\n"
                                    f"{_assistant_suggestions_text(part_candidates)}"
                                ),
                            )
                        else:
                            result = lookup_stock(
                                part_query=part_query,
                                branch_scope=branch,
                                include_locations=bool(parsed.get("include_locations")),
                            )
                            _assistant_append_chat(
                                history,
                                "assistant",
                                _assistant_format_lookup_response(
                                    result,
                                    include_locations=bool(parsed.get("include_locations")),
                                ),
                            )
                    else:
                        qty = int(parsed.get("qty") or 0)
                        part_query = (parsed.get("part_query") or "").strip()
                        reason = parsed.get("reason") or "assistant_chat_action"
                        location_hints = parsed.get("location_hints") or []

                        if qty <= 0:
                            _assistant_append_chat(history, "assistant", "Please specify a valid quantity.")
                        elif not part_query:
                            _assistant_append_chat(history, "assistant", "Please specify the part name/number.")
                        else:
                            part_scope = branch or from_branch
                            part_candidates = find_part_candidates(part_query, part_scope)
                            if not part_candidates:
                                _assistant_append_chat(
                                    history,
                                    "assistant",
                                    f"No part matched '{part_query}'. Try part number or clearer name.",
                                )
                            elif len(part_candidates) > 1:
                                _assistant_append_chat(
                                    history,
                                    "assistant",
                                    (
                                        f"I found multiple parts for '{part_query}'. Please clarify with part number:\n"
                                        f"{_assistant_suggestions_text(part_candidates)}"
                                    ),
                                )
                            else:
                                part = part_candidates[0]
                                pending_action = None
                                if action in {"add_stock", "remove_stock"}:
                                    if not branch:
                                        _assistant_append_chat(history, "assistant", "Please mention the branch.")
                                    elif not location_hints:
                                        _assistant_append_chat(history, "assistant", "Please mention location code (A3/B1/shelf 3).")
                                    else:
                                        location = resolve_location(branch, location_hints[0])
                                        if not location:
                                            _assistant_append_chat(
                                                history,
                                                "assistant",
                                                f"Location '{location_hints[0]}' not found in {branch.name}.",
                                            )
                                        else:
                                            pending_action = {
                                                "action": action,
                                                "params": {
                                                    "part_number": part.part_number,
                                                    "branch_id": branch.id,
                                                    "location_id": location.id,
                                                    "qty": qty,
                                                    "reason": reason,
                                                },
                                            }
                                elif action == "move_stock":
                                    if not branch:
                                        _assistant_append_chat(history, "assistant", "Please mention the branch.")
                                    elif len(location_hints) < 2:
                                        _assistant_append_chat(
                                            history,
                                            "assistant",
                                            "For move, specify source and destination locations (example: from A3 to B1).",
                                        )
                                    else:
                                        from_location = resolve_location(branch, location_hints[0])
                                        to_location = resolve_location(branch, location_hints[1])
                                        if not from_location or not to_location:
                                            _assistant_append_chat(
                                                history,
                                                "assistant",
                                                "Could not resolve one of the locations in that branch.",
                                            )
                                        else:
                                            pending_action = {
                                                "action": action,
                                                "params": {
                                                    "part_number": part.part_number,
                                                    "branch_id": branch.id,
                                                    "from_location_id": from_location.id,
                                                    "to_location_id": to_location.id,
                                                    "qty": qty,
                                                    "reason": reason,
                                                },
                                            }
                                elif action == "create_transfer_request":
                                    note = reason
                                    if not from_branch or not to_branch:
                                        _assistant_append_chat(
                                            history,
                                            "assistant",
                                            "Please specify both source and destination branches. Example: الصناعية القديمة -> مخرج 18",
                                        )
                                    else:
                                        pending_action = {
                                            "action": action,
                                            "params": {
                                                "part_number": part.part_number,
                                                "from_branch_id": from_branch.id,
                                                "to_branch_id": to_branch.id,
                                                "qty": qty,
                                                "note": note,
                                            },
                                        }

                                if pending_action:
                                    preview = pending_action["params"]
                                    draft_text = (
                                        f"Draft action: `{action}` for part `{preview['part_number']}` qty `{preview['qty']}`.\n"
                                        "Reply `confirm` / `تأكيد` to apply, or `cancel` / `إلغاء` to discard."
                                    )
                                    if llm_preface:
                                        draft_text = f"{llm_preface}\n\n{draft_text}"
                                    _assistant_append_chat(history, "assistant", draft_text)

        request.session[ASSISTANT_CHAT_HISTORY_KEY] = history
        request.session[ASSISTANT_CHAT_PENDING_KEY] = pending_action
        request.session.modified = True

    context = {
        "chat_history": history,
        "pending_action": pending_action,
        "assistant_role": role,
        "assistant_branch": active_branch or profile.branch,
        "assistant_write_actions": sorted(WRITE_ACTIONS),
        "assistant_engine_live": ai_engine_live,
        "assistant_engine_label": ai_engine_label,
    }
    return render(request, "inventory/ai_assistant.html", context)

@login_required
@manager_required
@require_POST
@active_branch_required
def refund_sale(request, sale_id: int):
    if _blocked_by_view_only_acl(request.user, Sale):
        return HttpResponseForbidden("Your account is view-only for refund actions.")
    scoped_sales = _scope_branch(
        Sale.objects.select_related("part", "branch", "order"),
        request.user,
        field_name="branch",
    )
    sale = get_object_or_404(scoped_sales, id=sale_id)
    if sale.branch_id and sale.branch_id != request.active_branch.id:
        return HttpResponseForbidden("You can refund sales only for the active branch.")
    reason = (request.POST.get("reason") or "").strip()
    if not reason:
        messages.error(request, "Refund reason is required.")
        return redirect(_safe_next_url(request, "sales_history"))

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

        sale_before = {"is_refunded": locked_sale.is_refunded}
        locked_sale.is_refunded = True
        locked_sale.save(update_fields=["is_refunded"])

        if locked_sale.part_id and locked_sale.branch_id:
            stock, _ = Stock.objects.select_for_update().get_or_create(
                part_id=locked_sale.part_id,
                branch_id=locked_sale.branch_id,
                defaults={"quantity": 0},
            )
            stock_before = stock.quantity
            add_stock_to_location(
                part=locked_sale.part,
                branch=locked_sale.branch,
                quantity=locked_sale.quantity,
                reason=reason,
                actor=request.user,
                action="refund_in",
            )
            stock.refresh_from_db(fields=["quantity"])

            log_audit_event(
                actor=request.user,
                action="stock.adjustment",
                reason=reason,
                object_type="Stock",
                object_id=stock.id,
                branch=locked_sale.branch,
                before={
                    "quantity": stock_before,
                    "part_number": locked_sale.part.part_number if locked_sale.part else "",
                    "reason": "sale_refund",
                    "sale_id": locked_sale.id,
                },
                after={
                    "quantity": stock.quantity,
                    "part_number": locked_sale.part.part_number if locked_sale.part else "",
                    "reason": "sale_refund",
                    "sale_id": locked_sale.id,
                },
            )

        log_audit_event(
            actor=request.user,
            action="sale.refund",
            reason=reason,
            object_type="Sale",
            object_id=locked_sale.id,
            branch=locked_sale.branch,
            before=sale_before,
            after={"is_refunded": locked_sale.is_refunded},
        )

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
        messages.warning(request, "تصدير XLSX يتطلب تثبيت مكتبة openpyxl. تم التحويل تلقائيًا إلى CSV.")

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
        sales_queryset.values("seller__username", "seller__profile__employee_id")
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
    profile = _get_or_create_profile(request.user)
    is_admin = is_admin_user(request.user)
    selected_branch_raw = (request.GET.get("branch") or "").strip()
    selected_branch = _assistant_or_manager_branch_scope(request.user, selected_branch_raw)

    stock_qs = _scope_branch(
        Stock.objects.select_related("part", "branch").order_by("part__name"),
        request.user,
        field_name="branch",
    )
    if is_admin and selected_branch is not None:
        stock_qs = stock_qs.filter(branch=selected_branch)
    elif not is_admin and profile.branch:
        stock_qs = stock_qs.filter(branch=profile.branch)

    reserved_map = _reserved_quantity_map_for_stocks(stock_qs)
    low_stock_items = []
    for stock in stock_qs:
        reserved_qty = reserved_map.get((stock.part_id, stock.branch_id), 0)
        available_qty = max(stock.quantity - reserved_qty, 0)
        if stock.min_stock_level > 0 and available_qty <= stock.min_stock_level:
            stock.available_quantity = available_qty
            stock.reserved_quantity = reserved_qty
            low_stock_items.append(stock)

    low_stock_items.sort(key=lambda row: (row.available_quantity, row.part.name, row.part.part_number))
    page_obj = Paginator(low_stock_items, 30).get_page(request.GET.get("page"))
    return render(
        request,
        "inventory/low_stock.html",
        {
            "low_stock_items": page_obj,
            "page_obj": page_obj,
            "branches": Branch.objects.all().order_by("name") if is_admin else [profile.branch] if profile.branch else [],
            "selected_branch": selected_branch.id if selected_branch else (profile.branch_id if (not is_admin and profile.branch) else None),
            "is_admin": is_admin,
        },
    )


@login_required
@manager_required
def export_low_stock_csv(request):
    profile = _get_or_create_profile(request.user)
    is_admin = is_admin_user(request.user)
    selected_branch_raw = (request.GET.get("branch") or "").strip()
    selected_branch = _assistant_or_manager_branch_scope(request.user, selected_branch_raw)

    stock_qs = _scope_branch(
        Stock.objects.select_related("part", "branch").order_by("part__name"),
        request.user,
        field_name="branch",
    )
    if is_admin and selected_branch is not None:
        stock_qs = stock_qs.filter(branch=selected_branch)
    elif not is_admin and profile.branch:
        stock_qs = stock_qs.filter(branch=profile.branch)

    reserved_map = _reserved_quantity_map_for_stocks(stock_qs)
    low_stock_items = []
    for stock in stock_qs:
        reserved_qty = reserved_map.get((stock.part_id, stock.branch_id), 0)
        available_qty = max(stock.quantity - reserved_qty, 0)
        if stock.min_stock_level > 0 and available_qty <= stock.min_stock_level:
            low_stock_items.append((stock, available_qty, reserved_qty))

    low_stock_items.sort(
        key=lambda row: (
            row[1],
            row[0].part.name if row[0].part else "",
            row[0].part.part_number if row[0].part else "",
        )
    )

    filename = f"low_stock_{timezone.localdate()}.csv"
    response = HttpResponse(content_type="text/csv; charset=utf-8")
    response["Content-Disposition"] = f'attachment; filename="{filename}"'
    response["X-Content-Type-Options"] = "nosniff"
    response["Cache-Control"] = "no-store"
    response.write("\ufeff")

    writer = csv.writer(response)
    writer.writerow(["Available", "Reserved", "Min Level", "Part Name", "Part Number", "Branch"])
    for stock, available_qty, reserved_qty in low_stock_items:
        writer.writerow(
            [
                available_qty,
                reserved_qty,
                stock.min_stock_level,
                _sanitize_csv_value(stock.part.name if stock.part else "-"),
                _sanitize_csv_value(stock.part.part_number if stock.part else "-"),
                _sanitize_csv_value(stock.branch.name if stock.branch else "-"),
            ]
        )
    return response


@login_required
@manager_required
def location_list(request):
    profile = _get_or_create_profile(request.user)
    is_admin = is_admin_user(request.user)
    branches = Branch.objects.all().order_by("name") if is_admin else [profile.branch] if profile.branch else []
    selected_branch_raw = (request.POST.get("branch") or request.GET.get("branch") or "").strip()
    branch_scope = _assistant_or_manager_branch_scope(request.user, selected_branch_raw)

    if not is_admin and profile.branch is None:
        messages.error(request, "Your manager account has no branch assignment.")
    if is_admin and selected_branch_raw and branch_scope is None:
        messages.error(request, "Selected branch was not found.")

    if request.method == "POST":
        action = (request.POST.get("action") or "create").strip()
        target_branch = branch_scope
        if is_admin and target_branch is None:
            branch_id_raw = (request.POST.get("branch") or "").strip()
            if branch_id_raw.isdigit():
                target_branch = Branch.objects.filter(id=int(branch_id_raw)).first()

        if action == "create":
            code = (request.POST.get("code") or "").strip().upper()
            name_ar = (request.POST.get("name_ar") or "").strip()
            name_en = (request.POST.get("name_en") or "").strip()

            if not target_branch:
                messages.error(request, "Branch is required.")
            elif not code:
                messages.error(request, "Location code is required.")
            else:
                try:
                    Location.objects.create(
                        branch=target_branch,
                        code=code,
                        name_ar=name_ar,
                        name_en=name_en,
                    )
                    messages.success(request, f"Location {code} created for {target_branch.name}.")
                except (ValidationError, IntegrityError) as exc:
                    messages.error(request, f"Could not create location: {exc}")

        if action == "delete":
            location_id = request.POST.get("location_id")
            delete_qs = Location.objects.select_related("branch")
            if branch_scope is not None:
                delete_qs = delete_qs.filter(branch=branch_scope)
            elif not is_admin:
                delete_qs = delete_qs.none()
            location = delete_qs.filter(id=location_id).first()
            if not location:
                messages.error(request, "Location not found.")
            elif location.code == "UNASSIGNED":
                messages.error(request, "Default location cannot be deleted.")
            elif StockLocation.objects.filter(location=location, quantity__gt=0).exists():
                messages.error(request, "Cannot delete a location that still has stock.")
            else:
                label = f"{location.branch.code}-{location.code}"
                location.delete()
                messages.success(request, f"Location {label} deleted.")

        return redirect(_redirect_with_branch("location_list", target_branch.id if target_branch else None))

    locations = Location.objects.select_related("branch").annotate(
        total_qty=Coalesce(Sum("stock_locations__quantity"), Value(0)),
        sku_count=Count("stock_locations__part", distinct=True),
    )
    if branch_scope is not None:
        locations = locations.filter(branch=branch_scope)
    elif not is_admin:
        locations = locations.none()
    locations = locations.order_by("branch__name", "code")

    return render(
        request,
        "inventory/locations_list.html",
        {
            "locations": locations,
            "branches": branches,
            "selected_branch": branch_scope.id if branch_scope else None,
            "is_admin": is_admin,
        },
    )


@login_required
@manager_required
def stock_locations_view(request):
    profile = _get_or_create_profile(request.user)
    is_admin = is_admin_user(request.user)
    active_branch = _active_branch_for_request(request)
    branches = Branch.objects.all().order_by("name")
    selected_branch_raw = (request.POST.get("branch") or request.GET.get("branch") or "").strip()

    if is_admin:
        if selected_branch_raw and selected_branch_raw.isdigit():
            selected_branch = Branch.objects.filter(id=int(selected_branch_raw)).first()
        elif request.method == "GET":
            selected_branch = branches.first()
        else:
            selected_branch = None
        if selected_branch_raw and selected_branch is None:
            messages.error(request, "Selected branch was not found.")
    else:
        selected_branch = profile.branch
        if not selected_branch:
            messages.error(request, "Your manager account has no branch assignment.")

    selected_branch_id = selected_branch.id if selected_branch else None
    location_choices = (
        Location.objects.filter(branch=selected_branch).order_by("code")
        if selected_branch
        else Location.objects.none()
    )
    part_choices = (
        Part.objects.filter(Q(stock__branch=selected_branch) | Q(stock_locations__branch=selected_branch))
        .distinct()
        .order_by("name")
        if selected_branch
        else Part.objects.none()
    )
    scan_part_raw = (request.GET.get("scan_part") or "").strip()
    scan_part_id = int(scan_part_raw) if scan_part_raw.isdigit() else None
    scan_mode = (request.GET.get("scan_mode") or "").strip().lower()
    scan_reason = (request.GET.get("scan_reason") or "").strip()
    scan_qty_raw = (request.GET.get("scan_qty") or "").strip()
    try:
        scan_qty = int(scan_qty_raw) if scan_qty_raw else 1
    except ValueError:
        scan_qty = 1
    if scan_qty <= 0:
        scan_qty = 1

    if request.method == "POST":
        if active_branch is None:
            messages.error(request, "يجب اختيار الفرع النشط أولاً قبل تنفيذ العملية.")
            return redirect(_safe_next_url(request, "stock_locations_view"))
        if selected_branch and selected_branch.id != active_branch.id:
            messages.error(request, "عمليات المخزون مسموحة فقط على الفرع النشط.")
            return redirect(_safe_next_url(request, "stock_locations_view"))
        if selected_branch is None:
            selected_branch = active_branch
            selected_branch_id = selected_branch.id
            location_choices = Location.objects.filter(branch=selected_branch).order_by("code")
            part_choices = (
                Part.objects.filter(Q(stock__branch=selected_branch) | Q(stock_locations__branch=selected_branch))
                .distinct()
                .order_by("name")
            )

        action = (request.POST.get("action") or "").strip()
        reason = (request.POST.get("reason") or "").strip()
        part = Part.objects.filter(id=request.POST.get("part_id")).first()
        qty_raw = request.POST.get("quantity")

        try:
            quantity = int(qty_raw or 0)
        except (TypeError, ValueError):
            quantity = 0

        if not selected_branch:
            messages.error(request, "Branch is required.")
        elif not part:
            messages.error(request, "Part is required.")
        elif quantity <= 0:
            messages.error(request, "Quantity must be greater than zero.")
        elif not reason:
            messages.error(request, "Reason is required.")
        else:
            try:
                if action == "add":
                    to_location = location_choices.filter(id=request.POST.get("to_location_id")).first()
                    if not to_location:
                        raise ValueError("Destination location is required.")
                    add_stock_to_location(
                        part=part,
                        branch=selected_branch,
                        quantity=quantity,
                        reason=reason,
                        actor=request.user,
                        location=to_location,
                        action="add",
                    )
                    messages.success(request, "Stock added to location.")
                elif action == "remove":
                    from_location = location_choices.filter(id=request.POST.get("from_location_id")).first()
                    if not from_location:
                        raise ValueError("Source location is required.")
                    remove_stock_from_locations(
                        part=part,
                        branch=selected_branch,
                        quantity=quantity,
                        reason=reason,
                        actor=request.user,
                        from_location=from_location,
                        action="remove",
                    )
                    messages.success(request, "Stock removed from location.")
                elif action == "move":
                    from_location = location_choices.filter(id=request.POST.get("from_location_id")).first()
                    to_location = location_choices.filter(id=request.POST.get("to_location_id")).first()
                    if not from_location or not to_location:
                        raise ValueError("Source and destination locations are required.")
                    move_stock_between_locations(
                        part=part,
                        branch=selected_branch,
                        quantity=quantity,
                        from_location=from_location,
                        to_location=to_location,
                        reason=reason,
                        actor=request.user,
                        action="move",
                    )
                    messages.success(request, "Stock moved between locations.")
                else:
                    messages.error(request, "Invalid stock location action.")
            except (ValidationError, ValueError) as exc:
                messages.error(request, str(exc))

        return redirect(_redirect_with_branch("stock_locations_view", selected_branch_id))

    query = (request.GET.get("q") or "").strip()
    stock_rows = (
        StockLocation.objects.select_related("part", "branch", "location")
        .filter(branch=selected_branch)
        .order_by("part__name", "location__code")
        if selected_branch
        else StockLocation.objects.none()
    )
    if query:
        stock_rows = stock_rows.filter(
            Q(part__name__icontains=query)
            | Q(part__part_number__icontains=query)
            | Q(location__code__icontains=query)
        )

    movements = (
        StockMovement.objects.select_related("part", "branch", "from_location", "to_location", "actor")
        .filter(branch=selected_branch)
        .order_by("-created_at")[:50]
        if selected_branch
        else StockMovement.objects.none()
    )
    scan_batch_lines = _scan_batch_lines(request)

    return render(
        request,
        "inventory/stock_locations.html",
        {
            "branches": branches if is_admin else [selected_branch] if selected_branch else [],
            "selected_branch": selected_branch_id,
            "is_admin": is_admin,
            "part_choices": part_choices,
            "location_choices": location_choices,
            "stock_rows": stock_rows,
            "movements": movements,
            "query": query,
            "scan_part_id": scan_part_id,
            "scan_mode": scan_mode,
            "scan_qty": scan_qty,
            "scan_reason": scan_reason,
            "scan_batch_lines": scan_batch_lines,
            "scan_batch_total_qty": sum(int(row.get("quantity") or 0) for row in scan_batch_lines),
        },
    )


@login_required
@require_GET
def vehicle_catalog(request):
    vehicles = Vehicle.objects.all().order_by("make", "model", "-year")

    grouped_by_make: dict[str, dict[str, list[Vehicle]]] = {}
    for vehicle in vehicles:
        make = (vehicle.make or "").strip() or "Unknown Make"
        model = (vehicle.model or "").strip() or "Unknown Model"
        grouped_by_make.setdefault(make, {}).setdefault(model, []).append(vehicle)

    make_sections = []
    for make_name in sorted(grouped_by_make.keys(), key=lambda value: value.casefold()):
        models_map = grouped_by_make[make_name]
        models = []
        total_years = 0
        for model_name in sorted(models_map.keys(), key=lambda value: value.casefold()):
            rows = sorted(models_map[model_name], key=lambda row: row.year, reverse=True)
            total_years += len(rows)
            models.append(
                {
                    "name": model_name,
                    "years": rows,
                    "count": len(rows),
                }
            )
        make_sections.append(
            {
                "name": make_name,
                "slug": re.sub(r"[^a-z0-9]+", "-", make_name.casefold()).strip("-") or "make",
                "models": models,
                "models_count": len(models),
                "years_count": total_years,
            }
        )

    return render(
        request,
        "inventory/vehicle_catalog.html",
        {
            "make_sections": make_sections,
            "total_makes": len(make_sections),
            "total_models": sum(section["models_count"] for section in make_sections),
            "total_year_rows": sum(section["years_count"] for section in make_sections),
        },
    )




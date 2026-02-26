import re
from difflib import get_close_matches
from typing import Any

from django.contrib.auth.models import User
from django.db import transaction
from django.db.models import Q

from .audit import log_audit_event
from .models import (
    Branch,
    Location,
    Part,
    Stock,
    StockLocation,
    StockMovement,
    TransferRequest,
    UserProfile,
    add_stock_to_location,
    move_stock_between_locations,
    remove_stock_from_locations,
)

ARABIC_DIGITS_TRANSLATION = str.maketrans("٠١٢٣٤٥٦٧٨٩", "0123456789")

WRITE_ACTIONS = {
    "add_stock",
    "remove_stock",
    "move_stock",
    "create_transfer_request",
}

CONFIRM_WORDS = {
    "confirm",
    "yes",
    "y",
    "ok",
    "approve",
    "go",
    "execute",
    "تأكيد",
    "نعم",
    "نفذ",
    "نفّذ",
    "موافق",
    "تم",
}

CANCEL_WORDS = {
    "cancel",
    "no",
    "stop",
    "abort",
    "إلغاء",
    "الغاء",
    "لا",
    "وقف",
}

ADD_KEYWORDS = {"add", "receive", "received", "وصل", "زيادة", "زود", "تزود"}
REMOVE_KEYWORDS = {"remove", "deduct", "dispose", "damaged", "خصم", "تالف", "إتلاف", "اتلاف"}
MOVE_KEYWORDS = {"move", "relocate", "transfer between", "نقل", "حول"}
TRANSFER_REQUEST_KEYWORDS = {"transfer request", "request transfer", "طلب تحويل", "تحويل", "transfer"}

BRANCH_SYNONYMS = {
    "الصناعية القديمة": {"الصناعية القديمة", "القديمة", "old industrial"},
    "مخرج 18": {"مخرج 18", "مخرج18", "exit 18", "exit18"},
    "الجمعية": {"الجمعية", "jamiah", "al jamiah", "society"},
}


def _normalize_text(value: str) -> str:
    return " ".join((value or "").translate(ARABIC_DIGITS_TRANSLATION).lower().split())


def is_confirm_message(message: str) -> bool:
    text = _normalize_text(message)
    return text in CONFIRM_WORDS


def is_cancel_message(message: str) -> bool:
    text = _normalize_text(message)
    return text in CANCEL_WORDS


def _extract_quantity(text: str) -> int | None:
    match = re.search(r"\b(\d+)\b", text)
    if not match:
        return None
    try:
        value = int(match.group(1))
    except (TypeError, ValueError):
        return None
    return value if value > 0 else None


def _extract_location_hints(message: str) -> list[str]:
    text = message.translate(ARABIC_DIGITS_TRANSLATION)
    hints: list[str] = []
    for code in re.findall(r"\b([A-Za-z]\d{1,3})\b", text):
        hints.append(code.upper())
    for shelf_no in re.findall(r"رف\s*(\d+)", text):
        hints.append(f"رف {shelf_no}")
    deduped: list[str] = []
    seen = set()
    for item in hints:
        if item in seen:
            continue
        seen.add(item)
        deduped.append(item)
    return deduped


def _detect_action(text: str) -> str:
    if any(keyword in text for keyword in TRANSFER_REQUEST_KEYWORDS):
        return "create_transfer_request"
    if any(keyword in text for keyword in MOVE_KEYWORDS):
        return "move_stock"
    if any(keyword in text for keyword in REMOVE_KEYWORDS):
        return "remove_stock"
    if any(keyword in text for keyword in ADD_KEYWORDS):
        return "add_stock"
    return "lookup_stock"


def _extract_reason(message: str, action: str) -> str:
    text = _normalize_text(message)
    reason_match = re.search(r"(?:reason|because|سبب|note|ملاحظة)\s*[:\-]?\s*(.+)$", text)
    if reason_match:
        return reason_match.group(1).strip()[:255]
    if "تالف" in text or "damaged" in text:
        return "damaged"
    if action == "add_stock" and any(token in text for token in {"receive", "وصل", "received"}):
        return "stock_received"
    if action == "remove_stock":
        return "stock_removed"
    if action == "move_stock":
        return "location_rebalance"
    if action == "create_transfer_request":
        return "assistant_transfer_request"
    return "assistant_chat_action"


def _extract_part_query(message: str, action: str, qty: int | None, location_hints: list[str]) -> str:
    text = _normalize_text(message)
    text = re.sub(r"(?:reason|because|سبب|note|ملاحظة)\s*[:\-]?\s*.+$", " ", text)
    if qty is not None:
        text = re.sub(rf"(?<![A-Za-z0-9\-]){qty}(?![A-Za-z0-9\-])", " ", text)
    for hint in location_hints:
        text = text.replace(hint.lower(), " ")
    for canonical, synonyms in BRANCH_SYNONYMS.items():
        text = text.replace(canonical.lower(), " ")
        for alias in synonyms:
            text = text.replace(alias.lower(), " ")
    stopwords = {
        "add",
        "receive",
        "received",
        "remove",
        "deduct",
        "move",
        "transfer",
        "request",
        "lookup",
        "stock",
        "to",
        "from",
        "in",
        "for",
        "at",
        "branch",
        "location",
        "qty",
        "quantity",
        "reason",
        "note",
        "وصل",
        "زيادة",
        "خصم",
        "تالف",
        "نقل",
        "تحويل",
        "طلب",
        "من",
        "الى",
        "إلى",
        "في",
        "على",
        "فرع",
        "موقع",
        "مواقع",
        "رف",
        "show",
        "find",
        "check",
        "how",
        "many",
        "left",
        "old",
        "industrial",
        "exit",
        "jamiah",
    }
    action_words = set()
    if action == "add_stock":
        action_words = ADD_KEYWORDS
    elif action == "remove_stock":
        action_words = REMOVE_KEYWORDS
    elif action == "move_stock":
        action_words = MOVE_KEYWORDS
    elif action == "create_transfer_request":
        action_words = TRANSFER_REQUEST_KEYWORDS
    stopwords |= {word for word in action_words if " " not in word}

    tokens = re.findall(r"[A-Za-z0-9\-\u0600-\u06FF]+", text)
    cleaned = [token for token in tokens if token not in stopwords and not token.isdigit()]
    return " ".join(cleaned).strip()


def parse_chat_message(message: str) -> dict[str, Any]:
    text = _normalize_text(message)
    action = _detect_action(text)
    qty = _extract_quantity(text)
    location_hints = _extract_location_hints(message)
    include_locations = (
        "location" in text
        or "locations" in text
        or "موقع" in text
        or "مواقع" in text
        or "رف" in text
    )
    reason = _extract_reason(message, action)
    part_query = _extract_part_query(message, action, qty, location_hints)

    return {
        "message": message,
        "action": action,
        "qty": qty,
        "location_hints": location_hints,
        "include_locations": include_locations,
        "reason": reason,
        "part_query": part_query,
    }


def _active_profile(user: User) -> UserProfile:
    default_role = UserProfile.Roles.ADMIN if user.is_superuser else UserProfile.Roles.CASHIER
    profile, _ = UserProfile.objects.get_or_create(user=user, defaults={"role": default_role})
    return profile


def user_role(user: User) -> str:
    if user.is_superuser:
        return UserProfile.Roles.ADMIN
    return _active_profile(user).role


def _branch_aliases(branch: Branch) -> set[str]:
    aliases = {branch.name.lower(), branch.code.lower()}
    branch_name = branch.name.lower()
    for canonical, synonyms in BRANCH_SYNONYMS.items():
        lowered_synonyms = {token.lower() for token in synonyms}
        if canonical in branch_name or any(token in branch_name for token in lowered_synonyms):
            aliases |= {token.lower() for token in synonyms}
    return aliases


def detect_branches_in_text(message: str, queryset) -> list[Branch]:
    text = _normalize_text(message)
    matches: list[tuple[int, Branch]] = []
    for branch in queryset:
        positions = []
        for alias in _branch_aliases(branch):
            index = text.find(alias)
            if index != -1:
                positions.append(index)
        if positions:
            matches.append((min(positions), branch))
    matches.sort(key=lambda item: item[0])
    return [branch for _, branch in matches]


def _branch_scope_for_user(user: User):
    role = user_role(user)
    if role == UserProfile.Roles.ADMIN:
        return Branch.objects.all().order_by("name")
    profile = _active_profile(user)
    if not profile.branch:
        return Branch.objects.none()
    return Branch.objects.filter(id=profile.branch_id)


def resolve_branch_context(
    *,
    user: User,
    action: str,
    message: str,
) -> dict[str, Branch | None]:
    if action in {"create_transfer_request", "add_stock", "remove_stock", "move_stock"}:
        candidate_branches = list(Branch.objects.all().order_by("name"))
    else:
        candidate_branches = list(_branch_scope_for_user(user))
    mentioned = detect_branches_in_text(message, candidate_branches)
    role = user_role(user)
    own_branch = _active_profile(user).branch if role != UserProfile.Roles.ADMIN else None

    if action == "create_transfer_request":
        from_branch = mentioned[0] if mentioned else None
        to_branch = mentioned[1] if len(mentioned) > 1 else None
        if role != UserProfile.Roles.ADMIN:
            to_branch = own_branch
        return {"branch": None, "from_branch": from_branch, "to_branch": to_branch}

    if action in {"add_stock", "remove_stock", "move_stock"}:
        if mentioned:
            return {"branch": mentioned[0], "from_branch": None, "to_branch": None}
        return {"branch": own_branch, "from_branch": None, "to_branch": None}

    if mentioned:
        return {"branch": mentioned[0], "from_branch": None, "to_branch": None}
    return {"branch": own_branch if role != UserProfile.Roles.ADMIN else None, "from_branch": None, "to_branch": None}


def _part_queryset_for_branch(branch_scope: Branch | None):
    if branch_scope is None:
        return Part.objects.all()
    return Part.objects.filter(Q(stock__branch=branch_scope) | Q(stock_locations__branch=branch_scope)).distinct()


def find_part_candidates(part_query: str, branch_scope: Branch | None, *, limit: int = 5) -> list[Part]:
    query = (part_query or "").strip()
    if not query:
        return []
    part_qs = _part_queryset_for_branch(branch_scope)
    exact_part_number = part_qs.filter(part_number__iexact=query).order_by("part_number")[:limit]
    if exact_part_number:
        return list(exact_part_number)

    icontains_matches = list(
        part_qs.filter(
            Q(part_number__icontains=query)
            | Q(name__icontains=query)
            | Q(barcode__icontains=query)
        )
        .order_by("part_number")[:limit]
    )
    if icontains_matches:
        return icontains_matches

    candidates = list(part_qs.values_list("part_number", "name")[:400])
    options = []
    lookup = {}
    for part_number, name in candidates:
        options.extend([part_number.lower(), name.lower()])
        lookup[part_number.lower()] = part_number
        lookup[name.lower()] = part_number
    close = get_close_matches(query.lower(), options, n=limit, cutoff=0.45)
    if not close:
        return []
    part_numbers = {lookup[token] for token in close if token in lookup}
    return list(part_qs.filter(part_number__in=part_numbers).order_by("part_number")[:limit])


def resolve_location(branch: Branch, hint: str | None) -> Location | None:
    if not branch or not hint:
        return None
    token = _normalize_text(hint)
    code_match = re.search(r"\b([a-z]\d{1,3})\b", token)
    if code_match:
        return Location.objects.filter(branch=branch, code__iexact=code_match.group(1)).first()
    shelf_match = re.search(r"رف\s*(\d+)", token)
    if shelf_match:
        shelf_no = shelf_match.group(1)
        return (
            Location.objects.filter(branch=branch)
            .filter(
                Q(code__icontains=shelf_no)
                | Q(name_ar__icontains=f"رف {shelf_no}")
                | Q(name_en__icontains=f"shelf {shelf_no}")
            )
            .order_by("code")
            .first()
        )
    return Location.objects.filter(branch=branch, code__iexact=hint.strip()).first()


def validate_tool_permission(
    *,
    user: User,
    action: str,
    branch: Branch | None = None,
    from_branch: Branch | None = None,
    to_branch: Branch | None = None,
) -> tuple[bool, str]:
    role = user_role(user)
    profile = _active_profile(user)

    if action == "lookup_stock":
        return True, ""

    if action == "create_transfer_request":
        if role == UserProfile.Roles.ADMIN:
            return True, ""
        if not profile.branch:
            return False, "Your account has no branch assignment."
        if to_branch and to_branch.id != profile.branch_id:
            return False, "Cashier/manager transfer requests must target your own branch."
        return True, ""

    if action in {"add_stock", "remove_stock", "move_stock"}:
        if role not in {UserProfile.Roles.MANAGER, UserProfile.Roles.ADMIN}:
            return False, "Only manager/admin can adjust stock."
        if role != UserProfile.Roles.ADMIN:
            if not profile.branch:
                return False, "Your account has no branch assignment."
            if branch and branch.id != profile.branch_id:
                return False, "Managers can adjust stock only in their own branch."
        return True, ""

    return False, "Unknown action."


def lookup_stock(part_query, branch_scope, include_locations):
    query = (part_query or "").strip()
    if not query:
        return {"rows": [], "summary": "Please provide a part name or part number."}

    part_qs = Part.objects.filter(
        Q(part_number__icontains=query)
        | Q(name__icontains=query)
        | Q(barcode__icontains=query)
    ).order_by("part_number")
    if branch_scope is not None:
        part_qs = part_qs.filter(Q(stock__branch=branch_scope) | Q(stock_locations__branch=branch_scope)).distinct()
    parts = list(part_qs[:20])
    if not parts:
        return {"rows": [], "summary": f"No stock matched '{query}'."}

    if include_locations:
        stock_qs = (
            StockLocation.objects.select_related("part", "branch", "location")
            .filter(part__in=parts)
            .order_by("part__part_number", "branch__name", "location__code")
        )
        if branch_scope is not None:
            stock_qs = stock_qs.filter(branch=branch_scope)
        rows = [
            {
                "part_number": row.part.part_number,
                "part_name": row.part.name,
                "branch": row.branch.name,
                "location": row.location.code,
                "quantity": row.quantity,
            }
            for row in stock_qs
        ]
        return {"rows": rows, "summary": f"Found {len(rows)} stock-location rows for '{query}'."}

    stock_qs = Stock.objects.select_related("part", "branch").filter(part__in=parts).order_by("part__part_number", "branch__name")
    if branch_scope is not None:
        stock_qs = stock_qs.filter(branch=branch_scope)
    rows = [
        {
            "part_number": row.part.part_number,
            "part_name": row.part.name,
            "branch": row.branch.name,
            "quantity": row.quantity,
        }
        for row in stock_qs
    ]
    return {"rows": rows, "summary": f"Found {len(rows)} stock rows for '{query}'."}


def add_stock(part_number, branch, location, qty, reason, actor: User | None = None):
    part = Part.objects.filter(part_number__iexact=(part_number or "").strip()).first()
    if not part:
        raise ValueError("Part not found.")
    if not isinstance(branch, Branch):
        raise ValueError("Branch not found.")
    if not isinstance(location, Location):
        raise ValueError("Location not found.")

    before_total = Stock.objects.filter(part=part, branch=branch).values_list("quantity", flat=True).first() or 0
    movement = add_stock_to_location(
        part=part,
        branch=branch,
        location=location,
        quantity=qty,
        reason=reason,
        actor=actor,
        action="assistant_add",
    )
    after_total = Stock.objects.filter(part=part, branch=branch).values_list("quantity", flat=True).first() or 0
    log_audit_event(
        actor=actor,
        action="assistant.stock.add",
        reason=reason,
        object_type="Stock",
        object_id=part.id,
        branch=branch,
        before={"branch_total_qty": before_total, "location": location.code, "qty": int(qty)},
        after={"branch_total_qty": after_total, "location": location.code, "qty": int(qty)},
    )
    return movement


def remove_stock(part_number, branch, location, qty, reason, actor: User | None = None):
    part = Part.objects.filter(part_number__iexact=(part_number or "").strip()).first()
    if not part:
        raise ValueError("Part not found.")
    if not isinstance(branch, Branch):
        raise ValueError("Branch not found.")
    if not isinstance(location, Location):
        raise ValueError("Location not found.")

    before_total = Stock.objects.filter(part=part, branch=branch).values_list("quantity", flat=True).first() or 0
    movements = remove_stock_from_locations(
        part=part,
        branch=branch,
        from_location=location,
        quantity=qty,
        reason=reason,
        actor=actor,
        action="assistant_remove",
    )
    after_total = Stock.objects.filter(part=part, branch=branch).values_list("quantity", flat=True).first() or 0
    log_audit_event(
        actor=actor,
        action="assistant.stock.remove",
        reason=reason,
        object_type="Stock",
        object_id=part.id,
        branch=branch,
        before={"branch_total_qty": before_total, "location": location.code, "qty": int(qty)},
        after={"branch_total_qty": after_total, "location": location.code, "qty": int(qty)},
    )
    return movements


def move_stock(part_number, branch, from_location, to_location, qty, reason, actor: User | None = None):
    part = Part.objects.filter(part_number__iexact=(part_number or "").strip()).first()
    if not part:
        raise ValueError("Part not found.")
    if not isinstance(branch, Branch):
        raise ValueError("Branch not found.")
    if not isinstance(from_location, Location) or not isinstance(to_location, Location):
        raise ValueError("Location not found.")

    movement = move_stock_between_locations(
        part=part,
        branch=branch,
        from_location=from_location,
        to_location=to_location,
        quantity=qty,
        reason=reason,
        actor=actor,
        action="assistant_move",
    )
    log_audit_event(
        actor=actor,
        action="assistant.stock.move",
        reason=reason,
        object_type="StockLocation",
        object_id=part.id,
        branch=branch,
        before={"from_location": from_location.code, "to_location": to_location.code, "qty": int(qty)},
        after={"from_location": from_location.code, "to_location": to_location.code, "qty": int(qty)},
    )
    return movement


def create_transfer_request(part_number, from_branch, to_branch, qty, note, actor: User | None = None):
    part = Part.objects.filter(part_number__iexact=(part_number or "").strip()).first()
    if not part:
        raise ValueError("Part not found.")
    if not isinstance(from_branch, Branch) or not isinstance(to_branch, Branch):
        raise ValueError("Branch not found.")
    if from_branch.id == to_branch.id:
        raise ValueError("Source and destination branches must be different.")
    if actor is None:
        raise ValueError("Actor is required.")

    with transaction.atomic():
        transfer = TransferRequest.objects.create(
            part=part,
            quantity=int(qty),
            source_branch=from_branch,
            destination_branch=to_branch,
            requested_by=actor,
            notes=note,
        )
        StockMovement.objects.create(
            part=part,
            branch=from_branch,
            qty=int(qty),
            action="assistant_transfer_request",
            from_location=None,
            to_location=None,
            reason=note or "assistant_transfer_request",
            actor=actor,
        )
        log_audit_event(
            actor=actor,
            action="transfer.request",
            reason=note or "assistant_transfer_request",
            object_type="TransferRequest",
            object_id=transfer.id,
            branch=to_branch,
            before={},
            after={
                "part_id": part.id,
                "quantity": int(qty),
                "source_branch_id": from_branch.id,
                "destination_branch_id": to_branch.id,
                "status": transfer.status,
                "channel": "assistant_chat",
            },
        )
    return transfer

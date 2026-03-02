from __future__ import annotations

import os
from decimal import Decimal, ROUND_HALF_UP
from typing import Any

from django.conf import settings
from django.db import transaction
from django.template.loader import render_to_string
from django.utils import timezone

from .audit import log_audit_event
from .models import (
    BranchInvoiceSequence,
    Order,
    SmaccSyncQueue,
    StockLocation,
    TaxInvoice,
    TaxInvoiceLine,
)
from .zatca import generate_zatca_qr, qr_png_data_uri


VAT_RATE = Decimal("0.15")


def _q2(value: Decimal) -> Decimal:
    return Decimal(value).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


_AMOUNT_WORDS_SUFFIXES = (
    "ريال سعودي فقط لا غير",
    "ريال سعودي لا غير",
    "ريال سعودي فقط",
    "فقط لا غير",
    "لا غير",
    "ريال سعودي",
)


def normalize_amount_words_ar(text: str) -> str:
    normalized = " ".join((text or "").strip().split())
    if not normalized:
        return ""

    while True:
        lowered = normalized.casefold()
        removed = False
        for suffix in _AMOUNT_WORDS_SUFFIXES:
            if lowered.endswith(suffix.casefold()):
                normalized = normalized[: len(normalized) - len(suffix)].strip(" ،,")
                removed = True
                break
        if not removed:
            break
    return " ".join(normalized.split())


def _amount_in_words_ar(amount: Decimal) -> str:
    try:
        from num2words import num2words
        return num2words(float(_q2(amount)), lang="ar")
    except Exception:
        return f"{_q2(amount)} ريال سعودي"


def amount_to_words_ar(amount: Decimal) -> str:
    return normalize_amount_words_ar(_amount_in_words_ar(_q2(amount)))


def _next_invoice_number(branch) -> int:
    sequence, created = BranchInvoiceSequence.objects.select_for_update().get_or_create(branch=branch)
    if created:
        sequence.last_issued_number = 1
        sequence.next_number = 2
        sequence.save(update_fields=["last_issued_number", "next_number", "updated_at"])
        return 1
    number = int(sequence.next_number or 1)
    sequence.last_issued_number = number
    sequence.next_number = number + 1
    sequence.save(update_fields=["last_issued_number", "next_number", "updated_at"])
    return number


def _seller_name_for_qr(branch) -> str:
    return (
        os.getenv("DELTA_COMPANY_NAME_AR")
        or os.getenv("DELTA_COMPANY_NAME_EN")
        or branch.name
        or "DELTA POS"
    )


def _vat_number_for_qr(branch) -> str:
    return branch.vat_registration_number or os.getenv("DELTA_COMPANY_VAT_NUMBER", "000000000000000")


def _invoice_payload(invoice: TaxInvoice) -> dict[str, Any]:
    return {
        "originId": str(invoice.invoice_uuid),
        "invoice_number": invoice.invoice_number,
        "branch_code": invoice.branch.code if invoice.branch else "",
        "issue_date": str(invoice.issue_date),
        "issue_time": str(invoice.issue_time),
        "currency": invoice.currency,
        "payment_method": invoice.payment_method,
        "subtotal_before_vat": str(invoice.subtotal_before_vat),
        "discount_amount": str(invoice.discount_amount),
        "advance_payment": str(invoice.advance_payment),
        "vat_amount": str(invoice.vat_amount),
        "net_amount": str(invoice.net_amount),
        "customer_name": invoice.customer_name,
        "customer_vat_number": invoice.customer_vat_number,
        "qr_payload": invoice.qr_payload,
        "lines": [
            {
                "serial_no": row.serial_no,
                "item_code": row.item_code,
                "part_number": row.part_number,
                "item_name": row.item_name,
                "unit": row.unit,
                "quantity": row.quantity,
                "unit_price": str(row.unit_price),
                "total_before_vat": str(row.total_before_vat),
                "vat_amount": str(row.vat_amount),
                "total_after_vat": str(row.total_after_vat),
                "warehouse_location": row.warehouse_location,
            }
            for row in invoice.lines.all().order_by("serial_no")
        ],
    }


def create_posted_invoice_from_order(
    *,
    order: Order,
    actor=None,
    payment_method: str = TaxInvoice.PaymentMethod.CASH,
    due_date=None,
    customer_vat_number: str = "",
    advance_payment: Decimal = Decimal("0.00"),
    total_in_words_ar: str = "",
):
    if hasattr(order, "invoice"):
        return order.invoice

    with transaction.atomic():
        invoice_number = _next_invoice_number(order.branch)
        customer_name = ""
        if order.customer:
            customer_name = order.customer.name or ""
        if not customer_name:
            customer_name = "Walk-in Customer"

        subtotal = _q2(order.subtotal)
        discount = _q2(order.discount_amount)
        vat_amount = _q2(order.vat_amount)
        net_amount = _q2(order.grand_total)
        advance = _q2(advance_payment or Decimal("0.00"))

        words_value = normalize_amount_words_ar((total_in_words_ar or "").strip()) or amount_to_words_ar(net_amount)

        invoice = TaxInvoice.objects.create(
            order=order,
            branch=order.branch,
            state=TaxInvoice.State.DRAFT,
            invoice_number=invoice_number,
            issue_date=timezone.localdate(order.created_at),
            issue_time=timezone.localtime(order.created_at).time().replace(microsecond=0),
            due_date=due_date,
            payment_method=payment_method if payment_method in TaxInvoice.PaymentMethod.values else TaxInvoice.PaymentMethod.CASH,
            customer_name=customer_name,
            customer_vat_number=(customer_vat_number or "").strip(),
            subtotal_before_vat=subtotal,
            discount_amount=discount,
            advance_payment=advance,
            vat_amount=vat_amount,
            net_amount=net_amount,
            total_in_words_ar=words_value,
        )

        serial = 1
        for sale in order.items.select_related("part").all().order_by("id"):
            line_before = _q2(sale.price_at_sale * sale.quantity)
            line_vat = _q2(line_before * VAT_RATE)
            line_after = _q2(line_before + line_vat)
            location = (
                StockLocation.objects.filter(part=sale.part, branch=order.branch, quantity__gt=0)
                .order_by("-quantity", "location__code")
                .select_related("location")
                .first()
            )
            TaxInvoiceLine.objects.create(
                invoice=invoice,
                serial_no=serial,
                item_code=sale.part.category.name if sale.part and sale.part.category else "",
                part_number=sale.part.part_number if sale.part else "",
                item_name=sale.part.name if sale.part else "Deleted item",
                unit="PCS",
                quantity=sale.quantity,
                unit_price=_q2(sale.price_at_sale),
                total_before_vat=line_before,
                vat_amount=line_vat,
                total_after_vat=line_after,
                warehouse_location=location.location.code if location and location.location else "",
            )
            serial += 1

        timestamp_iso = timezone.localtime(order.created_at).isoformat()
        invoice.qr_payload = generate_zatca_qr(
            _seller_name_for_qr(order.branch),
            _vat_number_for_qr(order.branch),
            timestamp_iso,
            float(invoice.net_amount),
            float(invoice.vat_amount),
        )
        invoice.state = TaxInvoice.State.POSTED
        invoice.posted_at = timezone.now()
        invoice.save(update_fields=["qr_payload", "state", "posted_at", "updated_at"])

        queue, _ = SmaccSyncQueue.objects.get_or_create(
            idempotency_key=f"invoice:{invoice.invoice_uuid}",
            defaults={
                "object_type": SmaccSyncQueue.ObjectType.SALE_INVOICE,
                "object_id": str(invoice.id),
                "status": SmaccSyncQueue.Status.PENDING,
                "payload_json": _invoice_payload(invoice),
            },
        )
        if not queue.payload_json:
            queue.payload_json = _invoice_payload(invoice)
            queue.save(update_fields=["payload_json", "updated_at"])

        log_audit_event(
            actor=actor,
            action="invoice.post",
            reason="invoice_posted_from_checkout",
            object_type="TaxInvoice",
            object_id=invoice.id,
            branch=invoice.branch,
            before={},
            after={
                "invoice_uuid": str(invoice.invoice_uuid),
                "invoice_number": invoice.invoice_number,
                "order_id": invoice.order.order_id,
                "state": invoice.state,
                "net_amount": str(invoice.net_amount),
                "vat_amount": str(invoice.vat_amount),
            },
        )
        return invoice


def build_invoice_template_context(
    invoice: TaxInvoice,
    *,
    layout_mode: str = "a4",
    bilingual: bool = True,
) -> dict[str, Any]:
    company_name_ar = os.getenv("DELTA_COMPANY_NAME_AR", "شركة دلتا للتجارة")
    company_name_en = os.getenv("DELTA_COMPANY_NAME_EN", "DELTA Trading Company")
    company_cr = invoice.branch.commercial_registration_number or os.getenv("DELTA_COMPANY_CR", "")
    company_vat = invoice.branch.vat_registration_number or os.getenv("DELTA_COMPANY_VAT_NUMBER", "")
    branch_address = invoice.branch.address or os.getenv("DELTA_BRANCH_ADDRESS", "")
    phone_1 = invoice.branch.phone_primary or os.getenv("DELTA_PHONE_PRIMARY", "")
    phone_2 = invoice.branch.phone_secondary or os.getenv("DELTA_PHONE_SECONDARY", "")
    email = invoice.branch.email or os.getenv("DELTA_EMAIL", "")
    website = invoice.branch.website or os.getenv("DELTA_WEBSITE", "")
    return {
        "invoice": invoice,
        "company_name_ar": company_name_ar,
        "company_name_en": company_name_en,
        "company_cr": company_cr,
        "company_vat": company_vat,
        "branch_address": branch_address,
        "phone_1": phone_1,
        "phone_2": phone_2,
        "email": email,
        "website": website,
        "amount_in_words_ar": normalize_amount_words_ar(invoice.total_in_words_ar) or amount_to_words_ar(invoice.net_amount),
        "qr_image_data": qr_png_data_uri(invoice.qr_payload),
        "layout_mode": layout_mode if layout_mode in {"a4", "thermal"} else "a4",
        "bilingual": bool(bilingual),
    }


def render_invoice_pdf_bytes(
    invoice: TaxInvoice,
    *,
    layout_mode: str = "a4",
    bilingual: bool = True,
) -> bytes:
    context = build_invoice_template_context(invoice, layout_mode=layout_mode, bilingual=bilingual)
    html = render_to_string("inventory/tax_invoice_pdf.html", context)
    try:
        from weasyprint import HTML
        return HTML(string=html, base_url=str(settings.BASE_DIR)).write_pdf()
    except Exception:
        return html.encode("utf-8")

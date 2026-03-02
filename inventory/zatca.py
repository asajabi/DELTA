from __future__ import annotations

import base64
from io import BytesIO


def generate_zatca_qr(seller_name, vat_number, timestamp_iso, total_amount, vat_amount):
    import base64

    def tlv(tag, value):
        tag_bytes = bytes([tag])
        value_bytes = value.encode("utf-8")
        length_bytes = bytes([len(value_bytes)])
        return tag_bytes + length_bytes + value_bytes

    qr_bytes = (
        tlv(1, seller_name) +
        tlv(2, vat_number) +
        tlv(3, timestamp_iso) +
        tlv(4, f"{total_amount:.2f}") +
        tlv(5, f"{vat_amount:.2f}")
    )

    return base64.b64encode(qr_bytes).decode("utf-8")


def qr_png_data_uri(payload: str) -> str:
    """Render a QR image as data URI for PDF/template embedding."""
    try:
        import qrcode
    except Exception:
        return ""

    qr = qrcode.QRCode(version=3, box_size=6, border=1)
    qr.add_data(payload)
    qr.make(fit=True)
    img = qr.make_image(fill_color="black", back_color="white")
    buffer = BytesIO()
    img.save(buffer, format="PNG")
    return "data:image/png;base64," + base64.b64encode(buffer.getvalue()).decode("ascii")

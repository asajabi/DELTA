from __future__ import annotations

from datetime import timedelta

from django.core.management.base import BaseCommand
from django.db import transaction
from django.utils import timezone

from inventory.invoicing import render_invoice_pdf_bytes
from inventory.models import SmaccSyncLog, SmaccSyncQueue, TaxInvoice
from inventory.smacc_client import SmaccClient, SmaccClientError


class Command(BaseCommand):
    help = "Process pending/failed SMACC sync queue items asynchronously."

    def add_arguments(self, parser):
        parser.add_argument("--limit", type=int, default=25, help="Maximum queue items per run.")
        parser.add_argument("--max-attempts", type=int, default=10, help="Maximum retries before permanent failure.")
        parser.add_argument("--base-delay-seconds", type=int, default=60, help="Base delay for exponential backoff.")

    def _eligible_queryset(self, *, max_attempts: int, base_delay_seconds: int):
        now = timezone.now()
        qs = SmaccSyncQueue.objects.filter(
            status__in=[SmaccSyncQueue.Status.PENDING, SmaccSyncQueue.Status.FAILED]
        ).order_by("created_at")

        eligible_ids = []
        for row in qs:
            if row.status == SmaccSyncQueue.Status.PENDING:
                eligible_ids.append(row.id)
                continue
            if row.attempts >= max_attempts:
                continue
            delay = timedelta(seconds=(2 ** max(row.attempts - 1, 0)) * base_delay_seconds)
            if row.updated_at + delay <= now:
                eligible_ids.append(row.id)
        return qs.filter(id__in=eligible_ids)

    def handle(self, *args, **options):
        limit = max(1, int(options["limit"]))
        max_attempts = max(1, int(options["max_attempts"]))
        base_delay_seconds = max(1, int(options["base_delay_seconds"]))
        client = SmaccClient()

        items = list(self._eligible_queryset(max_attempts=max_attempts, base_delay_seconds=base_delay_seconds)[:limit])
        self.stdout.write(f"SMACC queue processor: found {len(items)} item(s).")

        for item in items:
            with transaction.atomic():
                locked = SmaccSyncQueue.objects.select_for_update().filter(id=item.id).first()
                if not locked:
                    continue
                locked.status = SmaccSyncQueue.Status.PROCESSING
                locked.save(update_fields=["status", "updated_at"])

            request_payload = locked.payload_json or {}
            response_payload = {}
            status_code = None
            try:
                if locked.object_type == SmaccSyncQueue.ObjectType.SALE_INVOICE:
                    invoice = TaxInvoice.objects.select_related("branch").prefetch_related("lines").get(id=int(locked.object_id))
                    pdf_bytes = render_invoice_pdf_bytes(invoice)
                    response_payload = client.upload_invoice_pdf(
                        pdf_bytes,
                        filename=f"{invoice}.pdf",
                        origin_id=str(invoice.invoice_uuid),
                    )
                    locked.smacc_job_id = str(response_payload.get("jobId") or response_payload.get("job_id") or "")
                    locked.smacc_document_id = str(
                        response_payload.get("documentId")
                        or response_payload.get("document_id")
                        or locked.smacc_document_id
                    )
                    locked.status = SmaccSyncQueue.Status.SYNCED
                    locked.last_error = ""
                    status_code = 200
                else:
                    response_payload = {"message": "Unsupported object type for current worker."}
                    locked.status = SmaccSyncQueue.Status.FAILED
                    locked.last_error = "Unsupported object type."
                    status_code = 400
            except (TaxInvoice.DoesNotExist, ValueError) as exc:
                locked.status = SmaccSyncQueue.Status.FAILED
                locked.last_error = f"Missing object: {exc}"
                status_code = 404
                response_payload = {"error": locked.last_error}
            except SmaccClientError as exc:
                locked.status = SmaccSyncQueue.Status.FAILED
                locked.last_error = str(exc)
                status_code = 502
                response_payload = {"error": str(exc)}
            except Exception as exc:  # noqa: BLE001
                locked.status = SmaccSyncQueue.Status.FAILED
                locked.last_error = f"Unexpected error: {exc}"
                status_code = 500
                response_payload = {"error": str(exc)}
            finally:
                locked.attempts = int(locked.attempts or 0) + 1
                locked.save(
                    update_fields=[
                        "status",
                        "attempts",
                        "smacc_job_id",
                        "smacc_document_id",
                        "last_error",
                        "updated_at",
                    ]
                )
                SmaccSyncLog.objects.create(
                    queue_item=locked,
                    request_payload=request_payload,
                    response_payload=response_payload,
                    http_status=status_code,
                )

            self.stdout.write(
                f"Queue#{locked.id} {locked.object_type}:{locked.object_id} -> {locked.status} "
                f"(attempt {locked.attempts})"
            )

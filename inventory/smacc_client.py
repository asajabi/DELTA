from __future__ import annotations

import hashlib
import hmac
import json
import os
from datetime import timedelta
from typing import Any

from django.utils import timezone

from .models import SmaccToken


class SmaccClientError(Exception):
    pass


def _requests_module():
    try:
        import requests  # type: ignore
    except Exception as exc:  # noqa: BLE001
        raise SmaccClientError("The 'requests' package is required for SMACC integration.") from exc
    return requests


class SmaccClient:
    def __init__(self):
        self.base_url = (os.getenv("SMACC_BASE_URL") or "").rstrip("/")
        self.username = os.getenv("SMACC_USERNAME", "")
        self.password = os.getenv("SMACC_PASSWORD", "")
        self.client_id = os.getenv("SMACC_CLIENT_ID", "")
        self.client_secret = os.getenv("SMACC_CLIENT_SECRET", "")
        self.webhook_secret = os.getenv("SMACC_WEBHOOK_SECRET", "")
        self.timeout = int(os.getenv("SMACC_HTTP_TIMEOUT_SECONDS", "25"))

    def _auth_headers(self) -> dict[str, str]:
        token = SmaccToken.objects.order_by("-updated_at").first()
        if not token or token.expires_at <= timezone.now():
            token = self.login()
        return {"Authorization": f"Bearer {token.access_token}"}

    def login(self) -> SmaccToken:
        if not self.base_url:
            raise SmaccClientError("SMACC_BASE_URL is not configured.")
        url = f"{self.base_url}/auth/login"
        payload = {
            "username": self.username,
            "password": self.password,
            "clientId": self.client_id,
            "clientSecret": self.client_secret,
        }
        requests = _requests_module()
        response = requests.post(url, json=payload, timeout=self.timeout)
        if response.status_code >= 400:
            raise SmaccClientError(f"SMACC login failed ({response.status_code}).")
        data = response.json() if response.content else {}
        access_token = data.get("access_token") or data.get("token") or ""
        expires_in = int(data.get("expires_in") or 3600)
        if not access_token:
            raise SmaccClientError("SMACC login response missing access token.")
        expires_at = timezone.now() + timedelta(seconds=max(expires_in - 60, 60))
        token, _ = SmaccToken.objects.update_or_create(
            id=1,
            defaults={"access_token": access_token, "expires_at": expires_at},
        )
        return token

    def request(self, method: str, path: str, *, json_payload: dict[str, Any] | None = None, files=None, params=None):
        if not self.base_url:
            raise SmaccClientError("SMACC_BASE_URL is not configured.")
        url = f"{self.base_url}/{path.lstrip('/')}"
        headers = self._auth_headers()
        request_kwargs: dict[str, Any] = {
            "files": files,
            "params": params,
            "headers": headers,
            "timeout": self.timeout,
        }
        if files:
            if json_payload:
                request_kwargs["data"] = {"metadata": json.dumps(json_payload, ensure_ascii=False)}
        else:
            request_kwargs["json"] = json_payload
        requests = _requests_module()
        response = requests.request(
            method.upper(),
            url,
            **request_kwargs,
        )
        return response

    def upload_invoice_pdf(self, file_bytes: bytes, *, filename: str = "invoice.pdf", origin_id: str = "") -> dict[str, Any]:
        files = {
            "file": (filename, file_bytes, "application/pdf"),
        }
        metadata = {"originId": origin_id} if origin_id else {}
        response = self.request("POST", "/documents/upload", json_payload=metadata, files=files)
        payload = response.json() if response.content else {}
        if response.status_code >= 400:
            raise SmaccClientError(f"SMACC upload failed ({response.status_code}).")
        return payload

    def get_document_by_origin_id(self, origin_id: str) -> dict[str, Any]:
        response = self.request("GET", "/documents", params={"originId": origin_id})
        payload = response.json() if response.content else {}
        if response.status_code >= 400:
            raise SmaccClientError(f"SMACC lookup failed ({response.status_code}).")
        return payload

    def create_or_update_accounting_record(self, payload: dict[str, Any]) -> dict[str, Any]:
        response = self.request("POST", "/accounting-records", json_payload=payload)
        data = response.json() if response.content else {}
        if response.status_code >= 400:
            raise SmaccClientError(f"SMACC accounting record sync failed ({response.status_code}).")
        return data

    def subscribe_webhooks(self, callback_url: str) -> dict[str, Any]:
        payload = {
            "url": callback_url,
            "events": ["document.created", "document.updated", "accountingRecord.updated"],
        }
        response = self.request("POST", "/webhooks/subscribe", json_payload=payload)
        data = response.json() if response.content else {}
        if response.status_code >= 400:
            raise SmaccClientError(f"SMACC webhook subscription failed ({response.status_code}).")
        return data

    def verify_webhook_signature(self, raw_body: bytes, signature: str) -> bool:
        if not self.webhook_secret:
            return False
        expected = hmac.new(
            self.webhook_secret.encode("utf-8"),
            raw_body,
            hashlib.sha256,
        ).hexdigest()
        return hmac.compare_digest(expected, signature or "")


def safe_json_payload(data: Any) -> str:
    try:
        return json.dumps(data, ensure_ascii=False)
    except Exception:
        return "{}"

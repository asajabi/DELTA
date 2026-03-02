import json
import logging
from typing import Any
from urllib import error as url_error
from urllib import request as url_request

from django.conf import settings

logger = logging.getLogger(__name__)


class AssistantLLMError(RuntimeError):
    pass


def _setting_str(name: str, default: str = "") -> str:
    return str(getattr(settings, name, default) or "").strip()


def assistant_llm_enabled() -> bool:
    if not bool(getattr(settings, "AI_ASSISTANT_ENABLED", True)):
        return False
    return bool(_setting_str("AI_ASSISTANT_API_KEY") and _setting_str("AI_ASSISTANT_MODEL"))


def assistant_llm_engine_label() -> str:
    if not assistant_llm_enabled():
        return "Fallback Rule Engine"
    provider = _setting_str("AI_ASSISTANT_PROVIDER", "openai")
    model = _setting_str("AI_ASSISTANT_MODEL", "gpt-4.1-mini")
    return f"{provider}:{model}"


def _sanitize_plan(raw: Any) -> dict[str, str]:
    if not isinstance(raw, dict):
        raise AssistantLLMError("Invalid AI response payload.")
    mode_raw = str(raw.get("mode", "chat")).strip().lower()
    mode = "command" if mode_raw == "command" else "chat"
    assistant_reply = str(raw.get("assistant_reply", "")).strip()
    command_text = str(raw.get("command_text", "")).strip()
    if mode == "command" and not command_text:
        mode = "chat"
    if not assistant_reply:
        assistant_reply = "I can help with stock lookup, transfers, and inventory operations."
    return {
        "mode": mode,
        "assistant_reply": assistant_reply[:1600],
        "command_text": command_text[:500],
    }


def _build_system_prompt() -> str:
    return (
        "You are DELTA POS Copilot for branch inventory and POS operations.\n"
        "You must return JSON only, no markdown, no prose outside JSON.\n"
        "Return object with keys: mode, assistant_reply, command_text.\n"
        "mode must be one of: chat, command.\n"
        "assistant_reply must be concise and practical for staff.\n"
        "command_text is required only when mode=command.\n"
        "Use command mode only for one actionable operation supported by backend parser:\n"
        "1) lookup stock\n"
        "2) add stock\n"
        "3) remove stock\n"
        "4) move stock between locations\n"
        "5) create transfer request.\n"
        "When generating command_text, use clear canonical text with part number, qty, branch/location names/codes.\n"
        "Examples of command_text:\n"
        "- show stock OIL-1 in مخرج 18 with locations\n"
        "- add 5 OIL-1 in الصناعية القديمة A3 reason receive shipment\n"
        "- remove 2 OIL-1 in الصناعية القديمة A3 reason damaged\n"
        "- move 1 OIL-1 in الصناعية القديمة from A3 to B1 reason rebalance\n"
        "- transfer 4 OIL-1 from مخرج 18 to الجمعية note urgent demand\n"
        "If the user asks greeting, policy, training, or unclear request, use mode=chat.\n"
    )


def generate_assistant_plan(
    *,
    message: str,
    chat_history: list[dict[str, str]],
    user_role: str,
    active_branch_name: str,
    branch_names: list[str],
    location_codes: list[str],
) -> dict[str, str] | None:
    if not assistant_llm_enabled():
        return None

    api_key = _setting_str("AI_ASSISTANT_API_KEY")
    model = _setting_str("AI_ASSISTANT_MODEL", "gpt-4.1-mini")
    base_url = _setting_str("AI_ASSISTANT_BASE_URL", "https://api.openai.com/v1").rstrip("/")
    timeout_seconds = int(getattr(settings, "AI_ASSISTANT_TIMEOUT_SECONDS", 20))

    compact_history = [
        {"role": row.get("role", "user"), "content": str(row.get("content", ""))[:500]}
        for row in (chat_history or [])[-8:]
        if isinstance(row, dict)
    ]
    context_payload = {
        "user_role": user_role,
        "active_branch": active_branch_name or "",
        "known_branches": branch_names[:50],
        "known_locations_for_active_branch": location_codes[:200],
        "history": compact_history,
        "current_user_message": message,
    }

    payload = {
        "model": model,
        "temperature": 0.1,
        "response_format": {"type": "json_object"},
        "messages": [
            {"role": "system", "content": _build_system_prompt()},
            {"role": "user", "content": json.dumps(context_payload, ensure_ascii=False)},
        ],
    }
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    url = f"{base_url}/chat/completions"

    body_bytes = json.dumps(payload).encode("utf-8")
    request_obj = url_request.Request(
        url=url,
        data=body_bytes,
        headers=headers,
        method="POST",
    )
    try:
        with url_request.urlopen(request_obj, timeout=timeout_seconds) as response:
            status_code = int(response.getcode() or 0)
            response_body = response.read().decode("utf-8")
    except url_error.HTTPError as exc:
        status_code = int(exc.code or 500)
        logger.warning("Assistant LLM HTTP error: status=%s", status_code)
        raise AssistantLLMError(f"AI service returned HTTP {status_code}.") from exc
    except url_error.URLError as exc:
        raise AssistantLLMError(f"AI request failed: {exc}") from exc

    if status_code >= 400:
        logger.warning("Assistant LLM HTTP error: status=%s", status_code)
        raise AssistantLLMError(f"AI service returned HTTP {status_code}.")

    try:
        data = json.loads(response_body)
        content = data["choices"][0]["message"]["content"]
        raw_plan = json.loads(content)
    except (ValueError, KeyError, IndexError, TypeError) as exc:
        raise AssistantLLMError("AI response format was invalid.") from exc

    return _sanitize_plan(raw_plan)

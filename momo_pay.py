import asyncio
import json
import logging
from typing import Any
from curl_cffi.requests import AsyncSession

from user_agent_profile import (
    CURL_IMPERSONATE_PRIMARY as _IMP,
    WINDOWS_USER_AGENT as _UA,
    SEC_CH_UA as _SEC_CH_UA,
    SEC_CH_UA_MOBILE as _SEC_CH_UA_MOBILE,
    SEC_CH_UA_PLATFORM as _SEC_CH_UA_PLATFORM,
)
from payment_link import (
    _call_stripe_init_data,
    _generate_stripe_js_id,
    _STRIPE_INIT_URL_TPL,
    _check_response_error,
)

logger = logging.getLogger("momo_pay")

_STRIPE_CONFIRM_URL_TPL = "https://api.stripe.com/v1/payment_pages/{session_id}/confirm"


def _flatten_form(data: dict[str, Any], prefix: str = "") -> dict[str, str]:
    """Chuyển dict lồng nhau thành urlencoded key:value phẳng."""
    items: dict[str, str] = {}
    for key, value in data.items():
        full_key = f"{prefix}[{key}]" if prefix else key
        if isinstance(value, dict):
            items.update(_flatten_form(value, full_key))
        elif isinstance(value, list):
            for i, elem in enumerate(value):
                arr_key = f"{full_key}[{i}]"
                if isinstance(elem, dict):
                    items.update(_flatten_form(elem, arr_key))
                elif elem is not None:
                    items[arr_key] = str(elem)
        elif value is not None:
            if isinstance(value, bool):
                items[full_key] = "true" if value else "false"
            else:
                items[full_key] = str(value)
    return items


async def get_momo_checkout_details(
    session: AsyncSession,
    checkout_session_id: str,
    publishable_key: str | None,
    email: str = "",
    *,
    timeout: float = 25.0,
    log=print,
) -> dict[str, Any]:
    """Lấy link thanh toán MoMo / tạo QR MoMo cho phiên checkout."""
    checkout_url = f"https://chatgpt.com/checkout/openai_llc/{checkout_session_id}"
    result = {
        "checkout_session_id": checkout_session_id,
        "checkout_url": checkout_url,
        "momo_url": checkout_url,
        "qr_data": checkout_url,
        "status": "ready",
    }

    if not publishable_key or not checkout_session_id.startswith("cs_"):
        return result

    try:
        init_data = await _call_stripe_init_data(
            session,
            checkout_session_id,
            publishable_key,
            timeout=timeout,
        )
    except Exception as exc:
        log(f"[momo] Stripe init warning: {exc}")
        return result

    stripe_js_id = _generate_stripe_js_id()
    init_config_id = init_data.get("config_id") or ""
    init_checksum = init_data.get("init_checksum") or ""
    ppage_id = init_data.get("id") or ""

    # Thử confirm MoMo để lấy redirect/QR URL trực tiếp nếu Stripe cho phép
    confirm_payload = {
        "_stripe_version": "2025-03-31.basil; checkout_server_update_beta=v1; checkout_manual_approval_preview=v1",
        "key": publishable_key,
        "expected_payment_method_type": "momo",
        "payment_method_data": {
            "type": "momo",
            "billing_details": {
                "name": email.split("@")[0] if email else "ChatGPT Customer",
                "email": email or "customer@example.com",
                "address": {
                    "country": "VN",
                },
            },
        },
        "client_attribution_metadata": {
            "checkout_config_id": init_config_id,
            "checkout_session_id": checkout_session_id,
            "client_session_id": stripe_js_id,
            "merchant_integration_additional_elements": ["expressCheckout", "payment", "address"],
            "merchant_integration_source": "checkout",
            "merchant_integration_subtype": "payment-element",
            "merchant_integration_version": "custom",
            "payment_intent_creation_flow": "deferred",
            "payment_method_selection_flow": "merchant_specified",
        },
        "elements_session_client": {
            "client_betas": ["custom_checkout_server_updates_1", "custom_checkout_manual_approval_1"],
            "elements_init_source": "custom_checkout",
            "is_aggregation_expected": "false",
            "locale": "en-US",
            "referrer_host": "chatgpt.com",
            "stripe_js_id": stripe_js_id,
        },
        "elements_options_client": {
            "saved_payment_method": {
                "enable_redisplay": "never",
                "enable_save": "never",
            },
        },
        "init_checksum": init_checksum,
        "return_url": "https://chatgpt.com/",
    }

    form_data = _flatten_form(confirm_payload)
    confirm_url = _STRIPE_CONFIRM_URL_TPL.format(session_id=checkout_session_id)

    headers = {
        "Content-Type": "application/x-www-form-urlencoded",
        "Accept": "application/json",
        "Origin": "https://js.stripe.com",
        "Referer": "https://js.stripe.com/",
        "User-Agent": _UA,
        "sec-ch-ua": _SEC_CH_UA,
        "sec-ch-ua-mobile": _SEC_CH_UA_MOBILE,
        "sec-ch-ua-platform": _SEC_CH_UA_PLATFORM,
    }

    try:
        resp = await session.post(
            confirm_url,
            headers=headers,
            data=form_data,
            timeout=timeout,
        )
        if resp.status_code == 200:
            cdata = resp.json()
            next_action = cdata.get("next_action") or {}
            redirect_url = (
                (next_action.get("redirect_to_url") or {}).get("url")
                or next_action.get("url")
                or cdata.get("redirect_url")
            )
            if redirect_url:
                result["momo_url"] = redirect_url
                result["qr_data"] = redirect_url
                result["status"] = "confirmed"
                log(f"[momo] Confirm OK -> MoMo URL: {redirect_url[:60]}...")
                return result
    except Exception as exc:
        log(f"[momo] Confirm step note: {exc}")

    return result

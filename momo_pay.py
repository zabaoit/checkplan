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
_STRIPE_SETUP_INTENT_CONFIRM_TPL = "https://api.stripe.com/v1/setup_intents/{id}/confirm"
_STRIPE_PAYMENT_INTENT_CONFIRM_TPL = "https://api.stripe.com/v1/payment_intents/{id}/confirm"


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
    client_secret: str | None = None,
    *,
    timeout: float = 25.0,
    log=print,
) -> dict[str, Any]:
    """Kích hoạt và lấy mã QR thanh toán MoMo chính thức từ cổng Stripe/MoMo."""
    checkout_url = f"https://chatgpt.com/checkout/openai_llc/{checkout_session_id}"
    result = {
        "checkout_session_id": checkout_session_id,
        "checkout_url": checkout_url,
        "momo_url": checkout_url,
        "qr_data": checkout_url,
        "status": "ready",
        "is_momo_native_qr": False,
    }

    if not publishable_key:
        return result

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

    # 1. Thử confirm qua SetupIntent / PaymentIntent nếu có client_secret
    if client_secret and ("_secret_" in client_secret):
        intent_id = client_secret.split("_secret_")[0]
        if intent_id.startswith("seti_"):
            confirm_url = _STRIPE_SETUP_INTENT_CONFIRM_TPL.format(id=intent_id)
        elif intent_id.startswith("pi_"):
            confirm_url = _STRIPE_PAYMENT_INTENT_CONFIRM_TPL.format(id=intent_id)
        else:
            confirm_url = None

        if confirm_url:
            intent_payload = {
                "client_secret": client_secret,
                "payment_method_data": {
                    "type": "momo",
                    "billing_details": {
                        "name": email.split("@")[0] if email else "Customer",
                        "email": email or "customer@example.com",
                        "address": {"country": "VN"},
                    },
                },
                "expected_payment_method_type": "momo",
                "use_stripe_sdk": "true",
                "return_url": "https://chatgpt.com/",
                "key": publishable_key,
                "_stripe_version": "2025-03-31.basil",
            }
            try:
                resp = await session.post(
                    confirm_url,
                    headers=headers,
                    data=_flatten_form(intent_payload),
                    timeout=timeout,
                )
                if resp.status_code == 200:
                    data = resp.json()
                    next_action = data.get("next_action") or {}
                    momo_pay_url = (
                        (next_action.get("redirect_to_url") or {}).get("url")
                        or (next_action.get("display_qr_code") or {}).get("data")
                        or (next_action.get("display_qr_code") or {}).get("image_url_png")
                        or next_action.get("url")
                    )
                    if momo_pay_url:
                        result["momo_url"] = momo_pay_url
                        result["qr_data"] = momo_pay_url
                        result["status"] = "confirmed"
                        result["is_momo_native_qr"] = True
                        log(f"[momo] Setup/PaymentIntent confirm OK -> MoMo native URL: {momo_pay_url[:80]}...")
                        return result
            except Exception as exc:
                log(f"[momo] Intent confirm warning: {exc}")

    # 2. Thử confirm qua Payment Pages nếu session_id bắt đầu bằng cs_
    if checkout_session_id.startswith("cs_"):
        try:
            init_data = await _call_stripe_init_data(
                session,
                checkout_session_id,
                publishable_key,
                timeout=timeout,
            )
            stripe_js_id = _generate_stripe_js_id()
            init_config_id = init_data.get("config_id") or ""
            init_checksum = init_data.get("init_checksum") or ""

            confirm_payload = {
                "_stripe_version": "2025-03-31.basil; checkout_server_update_beta=v1; checkout_manual_approval_preview=v1",
                "key": publishable_key,
                "expected_payment_method_type": "momo",
                "payment_method_data": {
                    "type": "momo",
                    "billing_details": {
                        "name": email.split("@")[0] if email else "Customer",
                        "email": email or "customer@example.com",
                        "address": {"country": "VN"},
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

            confirm_url = _STRIPE_CONFIRM_URL_TPL.format(session_id=checkout_session_id)
            resp = await session.post(
                confirm_url,
                headers=headers,
                data=_flatten_form(confirm_payload),
                timeout=timeout,
            )
            if resp.status_code == 200:
                cdata = resp.json()
                next_action = cdata.get("next_action") or {}
                momo_pay_url = (
                    (next_action.get("redirect_to_url") or {}).get("url")
                    or (next_action.get("display_qr_code") or {}).get("data")
                    or next_action.get("url")
                    or cdata.get("redirect_url")
                )
                if momo_pay_url:
                    result["momo_url"] = momo_pay_url
                    result["qr_data"] = momo_pay_url
                    result["status"] = "confirmed"
                    result["is_momo_native_qr"] = True
                    log(f"[momo] PaymentPages confirm OK -> MoMo native URL: {momo_pay_url[:80]}...")
                    return result
        except Exception as exc:
            log(f"[momo] PaymentPages confirm warning: {exc}")

    return result

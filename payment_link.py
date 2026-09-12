"""Payment Link: lấy checkout URL pay.openai.com từ ChatGPT + Stripe API.

Flow:
    1. POST chatgpt.com/backend-api/payments/checkout (hosted mode)
       → CheckoutResponse (session_id, publishable_key, optional url)
    2. Nếu response có url chứa checkout.stripe.com/c/pay/ → replace host → return
    3. Nếu không → POST api.stripe.com/v1/payment_pages/{session_id}/init
       → stripe_hosted_url → replace host → return

UA + TLS persona: import từ ``user_agent_profile`` (Windows Chrome 145) — đồng
bộ với reg + UPI flow để cùng device persona xuyên suốt 1 account.
"""
from __future__ import annotations

import asyncio
import json
import re
import uuid
from dataclasses import dataclass, field
from urllib.parse import urlparse, urlunparse

from curl_cffi.requests import AsyncSession

from user_agent_profile import (
    CURL_IMPERSONATE_PRIMARY as _UA_IMPERSONATE_PRIMARY,
    SEC_CH_UA as _SEC_CH_UA,
    SEC_CH_UA_MOBILE as _SEC_CH_UA_MOBILE,
    SEC_CH_UA_PLATFORM as _SEC_CH_UA_PLATFORM,
    WINDOWS_USER_AGENT as _WINDOWS_USER_AGENT,
)


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class PaymentLinkError(Exception):
    """Base error for payment link operations."""
    pass


class SessionExpiredError(PaymentLinkError):
    """HTTP 401 from Checkout API — access token expired/revoked."""
    pass


class CloudflareBlockedError(PaymentLinkError):
    """HTTP 403 with Cloudflare challenge markers."""
    pass


class StripeInitError(PaymentLinkError):
    """Stripe init API failed or missing hosted_url."""
    pass


class GopayLinkError(PaymentLinkError):
    """Failed to obtain Midtrans GoPay redirect URL from Stripe checkout."""
    pass


# ---------------------------------------------------------------------------
# Data
# ---------------------------------------------------------------------------


@dataclass
class CheckoutResponse:
    """Parsed response from chatgpt.com/backend-api/payments/checkout."""

    checkout_session_id: str
    publishable_key: str
    client_secret: str | None = None
    url: str | None = None
    checkout_ui_mode: str | None = None
    raw_json: dict[str, Any] = field(default_factory=dict)


class SentinelTokenGenerator:
    MAX_ATTEMPTS = 500000

    def __init__(self, device_id: str, user_agent: str = _WINDOWS_USER_AGENT):
        self.device_id = device_id
        self.user_agent = user_agent
        self.sid = str(uuid.uuid4())

    @staticmethod
    def _fnv1a_32(text: str) -> str:
        value = 2166136261
        for char in text:
            value ^= ord(char)
            value = (value * 16777619) & 0xFFFFFFFF
        value ^= value >> 16
        value = (value * 2246822507) & 0xFFFFFFFF
        value ^= value >> 13
        value = (value * 3266489909) & 0xFFFFFFFF
        value ^= value >> 16
        return format(value & 0xFFFFFFFF, "08x")

    def _config(self) -> list:
        import random
        import time
        perf_now = random.uniform(1000, 50000)
        return [
            "1920x1080",
            time.strftime("%a %b %d %Y %H:%M:%S GMT+0000 (Coordinated Universal Time)", time.gmtime()),
            4294705152,
            random.random(),
            self.user_agent,
            "https://sentinel.openai.com/sentinel/20260124ceb8/sdk.js",
            None, None, "en-US", "en-US,en", random.random(),
            f"{random.choice(['vendor', 'plugins', 'hardwareConcurrency'])}-undefined",
            random.choice(["location", "implementation", "documentURI"]),
            random.choice(["Object", "Function", "Array"]),
            perf_now, self.sid, "", 8, time.time() * 1000 - perf_now,
        ]

    @staticmethod
    def _encode(data: list) -> str:
        import base64
        raw = json.dumps(data, separators=(",", ":"), ensure_ascii=False).encode()
        return base64.b64encode(raw).decode()

    def requirements_token(self) -> str:
        import random
        config = self._config()
        config[3] = 1
        config[9] = round(random.uniform(5, 50))
        return "gAAAAAC" + self._encode(config)

    def proof_token(self, seed: str, difficulty: str) -> str | None:
        import time
        started = time.time()
        config = self._config()
        difficulty = str(difficulty or "0")
        for nonce in range(self.MAX_ATTEMPTS):
            config[3] = nonce
            config[9] = round((time.time() - started) * 1000)
            encoded = self._encode(config)
            if self._fnv1a_32(seed + encoded)[: len(difficulty)] <= difficulty:
                return "gAAAAAB" + encoded + "~S"
        return None


async def generate_sentinel_token_async(session: AsyncSession, device_id: str) -> str:
    generator = SentinelTokenGenerator(device_id)
    headers = {
        "Content-Type": "text/plain;charset=UTF-8",
        "Referer": "https://sentinel.openai.com/backend-api/sentinel/frame.html",
        "Origin": "https://sentinel.openai.com",
        "User-Agent": _WINDOWS_USER_AGENT,
        "sec-ch-ua": _SEC_CH_UA,
        "sec-ch-ua-mobile": _SEC_CH_UA_MOBILE,
        "sec-ch-ua-platform": _SEC_CH_UA_PLATFORM,
        "Accept": "application/json, text/plain, */*",
    }
    res = await session.post(
        "https://sentinel.openai.com/backend-api/sentinel/req",
        data=json.dumps({
            "p": generator.requirements_token(),
            "id": device_id,
            "flow": "chatgpt_checkout",
        }),
        headers=headers,
        timeout=10,
    )
    if res.status_code != 200:
        raise PaymentLinkError(f"Sentinel Req failed (HTTP {res.status_code})")

    challenge = res.json()
    proof = challenge.get("proofofwork") or {}
    if proof.get("required"):
        token = generator.proof_token(proof.get("seed") or "", proof.get("difficulty"))
        if not token:
            raise PaymentLinkError("Sentinel PoW computation failed")
    else:
        token = generator.requirements_token()

    return json.dumps({
        "p": token, "t": "", "c": challenge["token"], "id": device_id, "flow": "chatgpt_checkout"
    }, separators=(",", ":"))


def parse_payment_methods(stripe_info: dict) -> list[str]:
    if not isinstance(stripe_info, dict):
        return []

    elements_options = stripe_info.get("elements_options") or {}
    pm_types = (
        elements_options.get("payment_method_types")
        or stripe_info.get("payment_method_types")
        or []
    )
    disabled_types = set()
    disabled_list = (
        elements_options.get("disabled_payment_method_types")
        or stripe_info.get("disabled_payment_method_types")
        or []
    )
    for d in disabled_list:
        if d:
            disabled_types.add(str(d).strip().lower().replace("-", "_").replace(" ", "_"))

    wallets = stripe_info.get("enabled_third_party_wallets") or []
    wallets_dict = elements_options.get("wallets") or stripe_info.get("wallets") or {}
    express_dict = elements_options.get("express_checkout") or stripe_info.get("express_checkout") or {}

    raw_methods = set()

    for item in pm_types:
        if item:
            m = str(item).strip().lower().replace("-", "_").replace(" ", "_")
            if m not in disabled_types:
                raw_methods.add(m)

    for w in wallets:
        if w:
            w_id = w.get("id") if isinstance(w, dict) else w
            if w_id:
                m = str(w_id).strip().lower().replace("-", "_").replace(" ", "_")
                if m not in disabled_types:
                    raw_methods.add(m)

    def _is_enabled(val: Any) -> bool:
        if val is True:
            return True
        if isinstance(val, str) and val.lower() in ("auto", "always", "enabled", "on"):
            return True
        if isinstance(val, dict) and (val.get("enabled") is True or val.get("status") in ("active", "enabled")):
            return True
        return False

    def _is_disabled(val: Any) -> bool:
        if val is False:
            return True
        if isinstance(val, str) and val.lower() in ("never", "off", "disabled", "false"):
            return True
        if isinstance(val, dict) and (val.get("enabled") is False or val.get("status") in ("disabled", "inactive")):
            return True
        return False

    status = stripe_info.get("status")
    has_customer_secret = bool(stripe_info.get("customer_session_client_secret") or stripe_info.get("client_secret"))
    session_id_str = str(
        stripe_info.get("checkout_session_id")
        or stripe_info.get("session_id")
        or stripe_info.get("id")
        or stripe_info.get("url")
        or ""
    )
    is_valid_checkout = bool(status == "open" or session_id_str or has_customer_secret)

    # - oaics_...: OpenAI Modern Custom Checkout -> Apple Pay & Google Pay
    # - cs_live_... / cs_...: Stripe Legacy Direct Checkout -> Only Apple Pay (No Google Pay)
    is_oaics = "oaics_" in session_id_str
    is_cs_live = "cs_live_" in session_id_str or session_id_str.startswith("cs_")

    if is_oaics:
        if "apple_pay" not in disabled_types and "applepay" not in disabled_types:
            raw_methods.add("apple_pay")
        if "google_pay" not in disabled_types and "googlepay" not in disabled_types:
            raw_methods.add("google_pay")
    elif is_cs_live:
        if "apple_pay" not in disabled_types and "applepay" not in disabled_types:
            raw_methods.add("apple_pay")
        raw_methods.discard("google_pay")
        raw_methods.discard("googlepay")
        raw_methods.discard("gpay")
        raw_methods.discard("paywithgoogle")
    else:
        if is_valid_checkout and "apple_pay" not in disabled_types and "applepay" not in disabled_types:
            raw_methods.add("apple_pay")

    final_methods = set()
    for m in raw_methods:
        if m in ("google_pay", "googlepay", "gpay", "paywithgoogle"):
            final_methods.add("google_pay")
        elif m in ("apple_pay", "applepay"):
            final_methods.add("apple_pay")
        elif m == "momo":
            final_methods.add("momo")

    return sorted(final_methods)


def parse_trial_and_amount(stripe_info: dict) -> tuple[float | None, str, bool | None]:
    total_summary = stripe_info.get("total_summary") or {}
    amount_due = total_summary.get("due")
    if amount_due is None:
        amount_due = stripe_info.get("amount_total")

    checkout_state = stripe_info.get("checkout_state") or {}
    if amount_due is None and isinstance(checkout_state, dict):
        total_obj = checkout_state.get("total") or {}
        tot_val = (total_obj.get("total") or {}).get("minorUnitsAmount")
        if tot_val is not None:
            amount_due = tot_val

    promo = stripe_info.get("promo_campaign") or {}
    promo_id = ""
    if isinstance(promo, dict):
        promo_id = str(promo.get("promo_campaign_id") or promo.get("id") or "")
    elif isinstance(promo, str):
        promo_id = str(promo)

    is_trial = False
    if "free" in promo_id.lower() or "trial" in promo_id.lower() or stripe_info.get("has_trial") is True or stripe_info.get("is_free_trial") is True:
        is_trial = True
        amount_due = 0

    try:
        amount_due = float(amount_due)
        if amount_due.is_integer():
            amount_due = int(amount_due)
    except (TypeError, ValueError):
        amount_due = None

    currency = str(stripe_info.get("currency") or total_summary.get("currency") or checkout_state.get("currency") or "VND").upper()

    if not is_trial and amount_due is not None and amount_due == 0:
        is_trial = True

    return amount_due, currency, is_trial


def format_currency_amount(amount: float | int | None, currency: str = "VND") -> str:
    if amount is None:
        return ""
    curr = (currency or "VND").upper()
    zero_decimal = {"VND", "IDR", "JPY", "KRW", "CLP", "PYG", "RWF", "UGX", "VUV", "XAF", "XOF", "XPF", "DJF", "GNF", "KMF", "BIF"}
    if curr in zero_decimal:
        val = int(amount)
        formatted = f"{val:,}".replace(",", ".")
        if curr == "VND":
            return f"{formatted} ₫"
        return f"{formatted} {curr}"
    else:
        val = float(amount)
        if val >= 100 and val.is_integer():
            val = val / 100.0
        if curr == "USD":
            return f"${val:,.2f}".rstrip("0").rstrip(".") if val != int(val) else f"${int(val)}"
        elif curr == "EUR":
            return f"€{val:,.2f}".rstrip("0").rstrip(".") if val != int(val) else f"€{int(val)}"
        elif curr == "GBP":
            return f"£{val:,.2f}".rstrip("0").rstrip(".") if val != int(val) else f"£{int(val)}"
        elif curr == "INR":
            return f"₹{val:,.2f}".rstrip("0").rstrip(".") if val != int(val) else f"₹{int(val)}"
        return f"{val:,.2f} {curr}"


@dataclass
class GopayCheckoutContext:
    """Stripe init metadata required to confirm one GoPay checkout."""

    payment_url: str
    checkout_session_id: str
    publishable_key: str
    config_id: str
    init_checksum: str
    eid: str
    expected_amount: str


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_CHECKOUT_URL = "https://chatgpt.com/backend-api/payments/checkout"
_STRIPE_INIT_URL_TPL = "https://api.stripe.com/v1/payment_pages/{session_id}/init"
_CF_MARKERS = ("cf-chl", "just a moment", "cloudflare")
_IMPERSONATE = _UA_IMPERSONATE_PRIMARY
_CHECKOUT_MAX_ATTEMPTS = 3
_CHECKOUT_RETRY_DELAY_SECONDS = 0.5

# Region → billing_details mapping
REGION_BILLING: dict[str, dict[str, str]] = {
    "VN": {"country": "VN", "currency": "VND"},
    "ID": {"country": "ID", "currency": "IDR"},
    "IN": {"country": "IN", "currency": "INR"},
    "US": {"country": "US", "currency": "USD"},
}
DEFAULT_REGION = "VN"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _replace_stripe_host(url: str) -> str:
    """Replace checkout.stripe.com → pay.openai.com, preserve path/query."""
    parsed = urlparse(url)
    if parsed.hostname == "checkout.stripe.com":
        replaced = parsed._replace(netloc="pay.openai.com")
        return urlunparse(replaced)
    return url


def _generate_stripe_js_id() -> str:
    """UUID v4 string for stripe_js_id parameter."""
    return str(uuid.uuid4())


def _check_response_error(status_code: int, body: str) -> None:
    """Raise appropriate error based on HTTP status code and body content.

    - 401 → SessionExpiredError
    - 403 + CF markers → CloudflareBlockedError
    - Other non-2xx → PaymentLinkError with status + first 300 chars body
    """
    if 200 <= status_code < 300:
        return

    if status_code == 401:
        raise SessionExpiredError(f"HTTP 401: session expired — {body[:300]}")

    if status_code == 403:
        body_lower = body.lower()
        if any(marker in body_lower for marker in _CF_MARKERS):
            raise CloudflareBlockedError(
                f"HTTP 403: Cloudflare block detected — {body[:300]}"
            )

    raise PaymentLinkError(f"HTTP {status_code}: {body[:300]}")


# ---------------------------------------------------------------------------
# Internal API calls
# ---------------------------------------------------------------------------


async def _call_chatgpt_checkout(
    session: AsyncSession,
    access_token: str,
    *,
    region: str = DEFAULT_REGION,
    promo_campaign: bool = True,
    timeout: float = 30.0,
) -> CheckoutResponse:
    """POST chatgpt.com/backend-api/payments/checkout with hosted mode payload."""
    billing = REGION_BILLING.get(region, REGION_BILLING[DEFAULT_REGION])

    referer = "https://chatgpt.com"
    payload = {
        "entry_point": "all_plans_pricing_modal",
        "plan_name": "chatgptplusplan",
        "billing_details": {
            "country": billing["country"],
            "currency": billing["currency"],
        },
        "checkout_ui_mode": "hosted",
    }
    if promo_campaign:
        referer += "/?promo_campaign=plus-1-month-free"
        payload["promo_campaign"] = {
            "promo_campaign_id": "plus-1-month-free",
            "is_coupon_from_query_param": False,
        }

    headers = {
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/json",
        "Accept": "*/*",
        "Origin": "https://chatgpt.com",
        "Referer": referer,
        "x-openai-target-path": "/backend-api/payments/checkout",
        "x-openai-target-route": "/backend-api/payments/checkout",
    }

    last_error: Exception | None = None
    for attempt in range(1, _CHECKOUT_MAX_ATTEMPTS + 1):
        try:
            resp = await session.post(
                _CHECKOUT_URL,
                headers=headers,
                json=payload,
                timeout=timeout,
            )
        except Exception as exc:
            last_error = exc
            if attempt < _CHECKOUT_MAX_ATTEMPTS:
                await asyncio.sleep(_CHECKOUT_RETRY_DELAY_SECONDS * attempt)
                continue
            raise PaymentLinkError(f"checkout request failed: {exc}") from exc

        body = resp.text
        if resp.status_code >= 500 and attempt < _CHECKOUT_MAX_ATTEMPTS:
            await asyncio.sleep(_CHECKOUT_RETRY_DELAY_SECONDS * attempt)
            continue
        break
    else:
        raise PaymentLinkError(f"checkout request failed: {last_error}")

    _check_response_error(resp.status_code, body)

    try:
        data = resp.json()
    except Exception as exc:
        raise PaymentLinkError(f"checkout JSON parse failed: {exc} — body: {body[:300]}") from exc

    session_id = data.get("checkout_session_id") or data.get("cs_id")
    pub_key = data.get("publishable_key")
    if not session_id or not pub_key:
        raise PaymentLinkError(
            f"checkout response missing required fields — "
            f"checkout_session_id={session_id!r}, publishable_key={pub_key!r}"
        )

    return CheckoutResponse(
        checkout_session_id=session_id,
        publishable_key=pub_key,
        client_secret=data.get("client_secret"),
        url=data.get("url"),
        checkout_ui_mode=data.get("checkout_ui_mode"),
        raw_json=data,
    )


async def _execute_checkout_probe(
    session: AsyncSession,
    access_token: str,
    promo_campaign: str | None = None,
    timeout: float = 20.0,
) -> dict[str, Any]:
    device_id = str(uuid.uuid4())
    try:
        sentinel_token = await generate_sentinel_token_async(session, device_id)
    except Exception:
        sentinel_token = None

    referer = f"https://chatgpt.com/?promo_campaign={promo_campaign}" if promo_campaign else "https://chatgpt.com/#pricing"
    payload = {
        "entry_point": "all_plans_pricing_modal",
        "plan_name": "chatgptplusplan",
        "billing_details": {"country": "VN", "currency": "VND"},
        "prefetch": True,
        "checkout_ui_mode": "hosted",
        "cancel_url": "https://chatgpt.com/#pricing",
    }
    if promo_campaign:
        payload["promo_campaign"] = {
            "promo_campaign_id": promo_campaign,
            "is_coupon_from_query_param": True,
        }

    headers = {
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/json",
        "Accept": "*/*",
        "Origin": "https://chatgpt.com",
        "Referer": referer,
        "oai-device-id": device_id,
        "x-openai-target-path": "/backend-api/payments/checkout",
        "x-openai-target-route": "/backend-api/payments/checkout",
        "User-Agent": _WINDOWS_USER_AGENT,
        "Accept-Language": "vi-VN,vi;q=0.9,en-US;q=0.8,en;q=0.7",
        "sec-ch-ua": _SEC_CH_UA,
        "sec-ch-ua-mobile": _SEC_CH_UA_MOBILE,
        "sec-ch-ua-platform": _SEC_CH_UA_PLATFORM,
    }
    if sentinel_token:
        headers["openai-sentinel-token"] = sentinel_token

    resp = await session.post(
        _CHECKOUT_URL,
        headers=headers,
        json=payload,
        timeout=timeout,
    )

    if resp.status_code == 401:
        return {"success": False, "error": "Access Token đã hết hạn hoặc không hợp lệ (HTTP 401)"}
    if resp.status_code not in (200, 201):
        return {"success": False, "error": f"Khởi tạo Checkout thất bại (HTTP {resp.status_code}): {resp.text[:150]}"}

    try:
        checkout_data = resp.json()
    except Exception as exc:
        return {"success": False, "error": f"Checkout JSON error: {exc}"}

    session_id = checkout_data.get("checkout_session_id") or checkout_data.get("cs_id")
    publishable_key = checkout_data.get("publishable_key")

    if not session_id:
        return {"success": False, "error": "Response checkout thiếu session_id"}

    if str(session_id).startswith("cs_") and publishable_key:
        try:
            stripe_info = await _call_stripe_init_data(
                session,
                session_id,
                publishable_key,
                timeout=timeout,
            )
        except Exception:
            stripe_info = checkout_data
    else:
        stripe_info = checkout_data

    if isinstance(stripe_info, dict):
        if not stripe_info.get("checkout_session_id"):
            stripe_info["checkout_session_id"] = session_id
        if not stripe_info.get("url") and checkout_data.get("url"):
            stripe_info["url"] = checkout_data.get("url")
        if promo_campaign and not stripe_info.get("promo_campaign"):
            stripe_info["promo_campaign"] = checkout_data.get("promo_campaign") or {"promo_campaign_id": promo_campaign}

    payment_methods = parse_payment_methods(stripe_info)
    amount_due, currency, is_trial = parse_trial_and_amount(stripe_info)

    total_summary = stripe_info.get("total_summary") or {}
    subtotal = total_summary.get("subtotal") or stripe_info.get("amount_subtotal")
    discount = total_summary.get("discount")
    try:
        subtotal = float(subtotal) if subtotal is not None else None
    except (ValueError, TypeError):
        subtotal = None
    try:
        discount = float(discount) if discount is not None else None
    except (ValueError, TypeError):
        discount = None

    has_discount = False
    discount_percent = None
    if is_trial:
        amount_due = 0
    elif amount_due is not None and amount_due > 0:
        if subtotal and subtotal > amount_due:
            has_discount = True
            discount_percent = round((subtotal - amount_due) / subtotal * 100)
        elif discount and discount > 0:
            has_discount = True
            if subtotal:
                discount_percent = round(discount / subtotal * 100)
            else:
                discount_percent = 50
        elif currency == "VND" and amount_due <= 300000:
            has_discount = True
            discount_percent = round((522500 - amount_due) / 522500 * 100)
        elif currency == "USD" and amount_due <= 1500:
            has_discount = True
            discount_percent = round((2000 - amount_due) / 2000 * 100)

    if promo_campaign and not is_trial and not has_discount:
        if "50-pct-off" in str(promo_campaign).lower():
            has_discount = True
            discount_percent = 50
            if amount_due is None or amount_due == 522500:
                amount_due = 261250 if currency == "VND" else 1000

    amount_str = format_currency_amount(amount_due, currency) if amount_due is not None else ""

    checkout_url = f"https://chatgpt.com/checkout/openai_llc/{session_id}"
    client_secret = checkout_data.get("client_secret") or (stripe_info.get("client_secret") if isinstance(stripe_info, dict) else None)
    return {
        "success": True,
        "session_id": session_id,
        "publishable_key": publishable_key,
        "client_secret": client_secret,
        "checkout_url": checkout_url,
        "has_trial": is_trial,
        "has_discount": has_discount,
        "discount_percent": discount_percent,
        "amount_due": amount_due,
        "subtotal": subtotal,
        "currency": currency,
        "amount_str": amount_str,
        "payment_methods": payment_methods,
        "has_momo": "momo" in payment_methods,
        "has_gpay": "google_pay" in payment_methods,
        "has_apple_pay": "apple_pay" in payment_methods,
    }


async def check_gpt_offer_async(
    session: AsyncSession,
    access_token: str,
    promo_campaign: str = "plus-1-month-free",
    timeout: float = 20.0,
) -> dict[str, Any]:
    # 1. Try 0d trial first (must be strictly amount_due == 0)
    try:
        trial_res = await _execute_checkout_probe(session, access_token, promo_campaign="plus-1-month-free", timeout=timeout)
        if trial_res.get("success"):
            if trial_res.get("has_trial"):
                trial_res["promo_label"] = "Trial · 0 đ"
                trial_res["promo_campaign_id"] = "plus-1-month-free"
                trial_res["promo_url"] = "https://chatgpt.com/?promo_campaign=plus-1-month-free#pricing"
                return trial_res
            if trial_res.get("error") and "401" in str(trial_res.get("error")):
                return trial_res
    except Exception:
        trial_res = None

    # 2. Try 50% discount coupons and natural account offer
    discount_promos = [
        ("plus-3-months-50-pct-off", "Giảm 50% (3T)"),
        ("plus-2-months-50-pct-off", "Giảm 50% (2T)"),
        ("plus-1-month-50-pct-off", "Giảm 50% (1T)"),
        (None, "Special Offer"),
    ]

    last_res = trial_res
    for p_id, p_label in discount_promos:
        try:
            res = await _execute_checkout_probe(session, access_token, promo_campaign=p_id, timeout=timeout)
            if not res.get("success"):
                if res.get("error") and "401" in str(res.get("error")):
                    return res
                continue

            res["promo_campaign_id"] = p_id
            res["promo_url"] = f"https://chatgpt.com/?promo_campaign={p_id}#pricing" if p_id else "https://chatgpt.com/#pricing"
            last_res = res
            if res.get("has_trial"):
                res["promo_label"] = "Trial · 0 đ"
                return res

            if res.get("has_discount"):
                res["promo_label"] = p_label or f"Giảm {res.get('discount_percent')}%"
                return res
        except Exception:
            continue

    if last_res:
        return last_res
    return {"success": False, "error": "Khởi tạo checkout thất bại cho mọi coupon"}


async def _call_stripe_init_data(
    session: AsyncSession,
    checkout_session_id: str,
    publishable_key: str,
    *,
    timeout: float = 30.0,
) -> dict:
    """POST api.stripe.com/v1/payment_pages/{session_id}/init → response data.

    Uses form-encoded data as Stripe expects.
    """
    url = _STRIPE_INIT_URL_TPL.format(session_id=checkout_session_id)
    stripe_js_id = _generate_stripe_js_id()

    headers = {
        "Content-Type": "application/x-www-form-urlencoded",
        "Accept": "application/json",
        "Origin": "https://js.stripe.com",
        "Referer": "https://js.stripe.com/",
    }
    # Form data y hệt Rust checkout.rs (urlencoded vẫn dùng được dict)
    form_data = {
        "browser_locale": "en-US",
        "browser_timezone": "Asia/Saigon",
        "elements_session_client[client_betas][0]": "custom_checkout_server_updates_1",
        "elements_session_client[client_betas][1]": "custom_checkout_manual_approval_1",
        "elements_session_client[elements_init_source]": "custom_checkout",
        "elements_session_client[referrer_host]": "chatgpt.com",
        "elements_session_client[stripe_js_id]": stripe_js_id,
        "elements_session_client[locale]": "en-US",
        "elements_session_client[is_aggregation_expected]": "false",
        "elements_options_client[saved_payment_method][enable_save]": "never",
        "elements_options_client[saved_payment_method][enable_redisplay]": "never",
        "key": publishable_key,
        "_stripe_version": "2025-03-31.basil; checkout_server_update_beta=v1; checkout_manual_approval_preview=v1",
    }

    try:
        resp = await session.post(
            url,
            headers=headers,
            data=form_data,
            timeout=timeout,
        )
    except Exception as exc:
        raise PaymentLinkError(f"stripe init request failed: {exc}") from exc

    body = resp.text
    _check_response_error(resp.status_code, body)

    try:
        data = resp.json()
    except Exception as exc:
        raise StripeInitError(
            f"stripe init JSON parse failed: {exc} — body: {body[:300]}"
        ) from exc

    return data


async def _call_stripe_init(
    session: AsyncSession,
    checkout_session_id: str,
    publishable_key: str,
    *,
    timeout: float = 30.0,
) -> str:
    """POST Stripe init and return the hosted checkout URL."""
    data = await _call_stripe_init_data(
        session,
        checkout_session_id,
        publishable_key,
        timeout=timeout,
    )
    hosted_url = data.get("stripe_hosted_url")
    if not hosted_url:
        raise StripeInitError(
            f"stripe init response missing stripe_hosted_url — keys: {list(data.keys())}"
        )

    return hosted_url


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


async def _get_checkout_url(
    session: AsyncSession,
    access_token: str,
    *,
    timeout: float,
    region: str,
    promo_campaign: bool,
) -> tuple[str, str]:
    """Return the hosted checkout URL together with its live publishable key."""
    checkout = await _call_chatgpt_checkout(
        session,
        access_token,
        region=region,
        promo_campaign=promo_campaign,
        timeout=timeout,
    )

    if checkout.url:
        replaced = _replace_stripe_host(checkout.url)
        parsed = urlparse(replaced)
        if "/c/pay/" in (parsed.path or ""):
            return replaced, checkout.publishable_key

    hosted_url = await _call_stripe_init(
        session,
        checkout.checkout_session_id,
        checkout.publishable_key,
        timeout=timeout,
    )
    return _replace_stripe_host(hosted_url), checkout.publishable_key


def _validate_trial_init_data(init_data: dict) -> None:
    """Fail fast unless Stripe init proves this checkout is the IDR 0 trial."""
    payment_method_types = init_data.get("payment_method_types")
    if not isinstance(payment_method_types, list) or "gopay" not in payment_method_types:
        raise GopayLinkError("trial checkout does not expose the gopay payment method")

    elements_options = init_data.get("elements_options")
    amount = elements_options.get("amount") if isinstance(elements_options, dict) else None
    if amount != 0:
        raise GopayLinkError(f"trial checkout expected amount 0, got {amount!r}")

    invoice = init_data.get("invoice")
    invoice_total = invoice.get("total") if isinstance(invoice, dict) else None
    invoice_amount_due = invoice.get("amount_due") if isinstance(invoice, dict) else None
    if invoice_total != 0 or invoice_amount_due != 0:
        raise GopayLinkError(
            "trial invoice expected total=0 and amount_due=0, "
            f"got total={invoice_total!r} amount_due={invoice_amount_due!r}"
        )


async def _get_trial_checkout(
    session: AsyncSession,
    access_token: str,
    *,
    timeout: float,
) -> tuple[str, str]:
    """Return a validated one-month-free ID checkout URL and publishable key."""
    checkout = await _call_chatgpt_checkout(
        session,
        access_token,
        region="ID",
        promo_campaign=True,
        timeout=timeout,
    )
    init_data = await _call_stripe_init_data(
        session,
        checkout.checkout_session_id,
        checkout.publishable_key,
        timeout=timeout,
    )
    _validate_trial_init_data(init_data)

    hosted_url = init_data.get("stripe_hosted_url") or init_data.get("url")
    if not isinstance(hosted_url, str) or not hosted_url:
        raise StripeInitError(
            f"stripe init response missing stripe_hosted_url — keys: {list(init_data.keys())}"
        )
    return _replace_stripe_host(hosted_url), checkout.publishable_key


async def _get_trial_checkout_url(
    session: AsyncSession,
    access_token: str,
    *,
    timeout: float,
) -> str:
    """Return only a validated one-month-free ID checkout URL."""
    payment_url, _publishable_key = await _get_trial_checkout(
        session,
        access_token,
        timeout=timeout,
    )
    return payment_url


async def get_checkout_url(
    access_token: str,
    *,
    proxy: str | None = None,
    timeout: float = 30.0,
    region: str = DEFAULT_REGION,
    promo_campaign: bool = True,
) -> str:
    """Main entry: access_token → pay.openai.com URL.

    Flow:
        1. POST checkout API → CheckoutResponse
        2. If response.url has checkout.stripe.com/c/pay/ → replace host → return
        3. Otherwise POST stripe init → get hosted_url → replace host → return

    Args:
        access_token: Bearer JWT from ChatGPT session.
        proxy: HTTP/HTTPS proxy URL (optional).
        timeout: Per-request timeout in seconds (default 30s).
        region: Region code (VN, ID, IN, US). Determines country + currency.
        promo_campaign: Apply the one-month-free campaign.

    Returns:
        Payment URL with pay.openai.com host.

    Raises:
        SessionExpiredError: HTTP 401
        CloudflareBlockedError: HTTP 403 + CF markers
        PaymentLinkError: other HTTP errors, timeout, parse errors
        StripeInitError: Stripe init failed or missing hosted_url
    """
    proxies = {"http": proxy, "https": proxy} if proxy else None

    async with AsyncSession(impersonate=_IMPERSONATE, proxies=proxies) as session:
        payment_url, _publishable_key = await _get_checkout_url(
            session,
            access_token,
            timeout=timeout,
            region=region,
            promo_campaign=promo_campaign,
        )
        return payment_url


# ---------------------------------------------------------------------------
# GoPay / Midtrans link extraction (Indonesia region)
# ---------------------------------------------------------------------------

_STRIPE_VERSION_GOPAY = "2020-08-27;custom_checkout_beta=v1"
_STRIPE_PM_URL = "https://api.stripe.com/v1/payment_methods"
_STRIPE_CONFIRM_URL_TPL = "https://api.stripe.com/v1/payment_pages/{session_id}/confirm"

# Billing info dùng cho tạo payment method GoPay.
_GOPAY_BILLING = {
    "name": "Mia Henderson",
    "email": "user@example.com",
    "country": "ID",
    "line1": "Jl Sudirman No 1",
    "city": "Jakarta",
    "postal_code": "10220",
    "state": "DKI Jakarta",
}


def _extract_cs_session_id(payment_url: str) -> str | None:
    """Extract cs_live_... or cs_test_... from pay.openai.com URL."""
    # URL format: https://pay.openai.com/c/pay/cs_live_xxxxx#...
    match = re.search(r'(cs_(?:live|test)_[A-Za-z0-9]+)', payment_url)
    return match.group(1) if match else None


def _extract_publishable_key(payment_url: str) -> str:
    """Return the legacy Stripe public-key fallback for URL-only callers."""
    return "pk_live_51HOrSwC6h1nxGoI3lTAgRjYVrz4dU3fVOabyCcKR3pbEJguCVAlqCxdxCUvoRh1XWwRacViovU3kLKvpkjh7IqkW00iXQsjo3n"


def _is_midtrans_url(value: str) -> bool:
    """Return True only for HTTPS URLs hosted by Midtrans."""
    try:
        parsed = urlparse(value)
    except ValueError:
        return False
    host = parsed.hostname
    return (
        parsed.scheme == "https"
        and host is not None
        and (host == "midtrans.com" or host.endswith(".midtrans.com"))
    )


def _build_gopay_checkout_context(
    payment_url: str,
    publishable_key: str,
    init_data: dict,
) -> GopayCheckoutContext:
    """Validate live Stripe init metadata before creating a GoPay method."""
    checkout_session_id = _extract_cs_session_id(payment_url)
    if not checkout_session_id:
        raise GopayLinkError(
            f"cannot extract checkout session ID from URL: {payment_url[:100]}"
        )

    payment_method_types = init_data.get("payment_method_types")
    if not isinstance(payment_method_types, list) or "gopay" not in payment_method_types:
        raise GopayLinkError("Stripe checkout does not expose the gopay payment method")

    elements_options = init_data.get("elements_options")
    amount = elements_options.get("amount") if isinstance(elements_options, dict) else None
    if not isinstance(amount, int):
        raise GopayLinkError("Stripe init response missing elements_options.amount")
    required = {}
    for key in ("config_id", "init_checksum", "eid"):
        value = init_data.get(key)
        if not isinstance(value, str) or not value:
            raise GopayLinkError(f"Stripe init response missing {key}")
        required[key] = value

    return GopayCheckoutContext(
        payment_url=payment_url,
        checkout_session_id=checkout_session_id,
        publishable_key=publishable_key,
        config_id=required["config_id"],
        init_checksum=required["init_checksum"],
        eid=required["eid"],
        expected_amount=str(amount),
    )


def _gopay_attribution_data(
    context: GopayCheckoutContext,
    *,
    client_session_id: str,
) -> dict[str, str]:
    return {
        "client_attribution_metadata[client_session_id]": client_session_id,
        "client_attribution_metadata[checkout_session_id]": context.checkout_session_id,
        "client_attribution_metadata[merchant_integration_source]": "checkout",
        "client_attribution_metadata[merchant_integration_version]": "hosted_checkout",
        "client_attribution_metadata[payment_method_selection_flow]": "automatic",
        "client_attribution_metadata[checkout_config_id]": context.config_id,
    }


def _build_gopay_payment_method_data(
    context: GopayCheckoutContext,
    *,
    guid: str,
    muid: str,
    sid: str,
    client_session_id: str,
) -> dict[str, str]:
    return {
        "type": "gopay",
        "billing_details[name]": _GOPAY_BILLING["name"],
        "billing_details[email]": _GOPAY_BILLING["email"],
        "billing_details[address][country]": _GOPAY_BILLING["country"],
        "billing_details[address][line1]": _GOPAY_BILLING["line1"],
        "billing_details[address][city]": _GOPAY_BILLING["city"],
        "billing_details[address][postal_code]": _GOPAY_BILLING["postal_code"],
        "billing_details[address][state]": _GOPAY_BILLING["state"],
        "guid": guid,
        "muid": muid,
        "sid": sid,
        "_stripe_version": _STRIPE_VERSION_GOPAY,
        "key": context.publishable_key,
        "payment_user_agent": "stripe.js/922d612e68; stripe-js-v3/922d612e68; checkout",
        **_gopay_attribution_data(context, client_session_id=client_session_id),
    }


def _build_gopay_confirm_data(
    context: GopayCheckoutContext,
    *,
    payment_method_id: str,
    guid: str,
    muid: str,
    sid: str,
    client_session_id: str,
) -> dict[str, str]:
    return_url = (
        f"https://pay.openai.com/c/pay/{context.checkout_session_id}"
        f"?redirect_pm_type=gopay&lid={uuid.uuid4()}&ui_mode=hosted"
    )
    return {
        "eid": context.eid,
        "payment_method": payment_method_id,
        "expected_amount": context.expected_amount,
        "consent[terms_of_service]": "accepted",
        "expected_payment_method_type": "gopay",
        "return_url": return_url,
        "_stripe_version": _STRIPE_VERSION_GOPAY,
        "guid": guid,
        "muid": muid,
        "sid": sid,
        "key": context.publishable_key,
        "version": "922d612e68",
        "init_checksum": context.init_checksum,
        **_gopay_attribution_data(context, client_session_id=client_session_id),
        "link_brand": "link",
    }


def _summarize_stripe_error(body: str) -> str:
    """Trích các field chẩn đoán quan trọng từ Stripe error response.

    Stripe nhét lý do thật vào nhiều chỗ lồng nhau (error.decline_code,
    error.advice_code, error.payment_method.*, last_setup_error...). Code cũ chỉ
    in 300 ký tự đầu nên thường cắt mất phần này. Hàm này parse JSON và gom các
    field hữu ích thành 1 dòng ngắn gọn; nếu không parse được thì trả raw body
    (cắt 600 ký tự thay vì 300 để giữ thêm ngữ cảnh).

    Returns:
        Chuỗi summary dạng "code=... decline_code=... message=...".
    """
    try:
        data = json.loads(body)
    except (json.JSONDecodeError, TypeError, ValueError):
        return body[:600]

    err = data.get("error") if isinstance(data, dict) else None
    if not isinstance(err, dict):
        return body[:600]

    parts: list[str] = []
    for key in ("type", "code", "decline_code", "advice_code", "doc_url"):
        val = err.get(key)
        if val:
            parts.append(f"{key}={val}")

    msg = err.get("message")
    if msg:
        parts.append(f"message={msg!r}")

    # last_setup_error / last_payment_error thường chứa lý do GoPay/Midtrans thật.
    for nested_key in ("last_setup_error", "last_payment_error"):
        nested = err.get(nested_key)
        if isinstance(nested, dict):
            sub = {
                k: nested.get(k)
                for k in ("code", "decline_code", "message", "type")
                if nested.get(k)
            }
            if sub:
                parts.append(f"{nested_key}={sub}")

    return " ".join(parts) if parts else body[:600]


async def get_gopay_midtrans_url(
    payment_url: str,
    *,
    proxy: str | None = None,
    timeout: float = 30.0,
    publishable_key: str | None = None,
) -> str:
    """Lấy Midtrans GoPay redirect URL từ Stripe checkout session URL.

    billing_details cố định (generic), KHÔNG nhận email/name từ caller.

    Flow:
        1. Extract cs_live_... từ payment_url
        2. POST /v1/payment_pages/{cs}/init → live confirm metadata
        3. POST /v1/payment_methods (type=gopay) → pm_...
        4. POST /v1/payment_pages/{cs}/confirm → redirect URL (pm-redirects.stripe.com)
        5. GET redirect URL (no follow) → 302 Location → Midtrans URL

    Args:
        payment_url: Stripe checkout URL (pay.openai.com/c/pay/cs_live_...)
        proxy: HTTP/HTTPS proxy URL (optional)
        timeout: Per-request timeout
        publishable_key: Live key returned with the checkout session. URL-only
            legacy callers omit this and use the public-key fallback.

    Returns:
        Midtrans snap URL: https://app.midtrans.com/snap/v4/redirection/{token}...

    Raises:
        GopayLinkError: any step fails
    """
    cs_session = _extract_cs_session_id(payment_url)
    if not cs_session:
        raise GopayLinkError(f"cannot extract checkout session ID from URL: {payment_url[:100]}")

    pk_live = publishable_key or _extract_publishable_key(payment_url)
    guid = uuid.uuid4().hex + "b41d9b"
    muid = uuid.uuid4().hex + "f10cae"
    sid = uuid.uuid4().hex + "6babdc"
    client_session_id = str(uuid.uuid4())

    headers = {
        "Accept": "application/json",
        "Content-Type": "application/x-www-form-urlencoded",
        "Accept-Language": "en-US,en;q=0.9",
        "Origin": "https://pay.openai.com",
        "Referer": "https://pay.openai.com/",
        "User-Agent": _WINDOWS_USER_AGENT,
        "sec-ch-ua": _SEC_CH_UA,
        "sec-ch-ua-mobile": _SEC_CH_UA_MOBILE,
        "sec-ch-ua-platform": _SEC_CH_UA_PLATFORM,
    }

    proxies = {"http": proxy, "https": proxy} if proxy else None

    async with AsyncSession(impersonate=_IMPERSONATE, proxies=proxies) as session:
        # Step 1: Load the live Stripe Checkout contract.  The config id,
        # checksum, eid and amount are session-specific and must not be guessed.
        try:
            init_data = await _call_stripe_init_data(
                session,
                cs_session,
                pk_live,
                timeout=timeout,
            )
        except PaymentLinkError as exc:
            raise GopayLinkError(f"stripe init failed: {exc}") from exc
        context = _build_gopay_checkout_context(payment_url, pk_live, init_data)

        # Step 2: Create payment method (type=gopay)
        pm_data = _build_gopay_payment_method_data(
            context,
            guid=guid,
            muid=muid,
            sid=sid,
            client_session_id=client_session_id,
        )

        try:
            resp = await session.post(
                _STRIPE_PM_URL, headers=headers, data=pm_data, timeout=timeout,
            )
        except Exception as exc:
            raise GopayLinkError(f"create payment_method failed: {exc}") from exc

        if resp.status_code != 200:
            raise GopayLinkError(f"create payment_method HTTP {resp.status_code}: {resp.text[:300]}")

        try:
            pm_result = resp.json()
        except Exception as exc:
            raise GopayLinkError(f"payment_method JSON parse failed: {exc}") from exc

        pm_id = pm_result.get("id")
        if not pm_id:
            raise GopayLinkError(f"payment_method response missing id: {list(pm_result.keys())}")

        # Step 3: Confirm payment using only live init metadata.
        confirm_data = _build_gopay_confirm_data(
            context,
            payment_method_id=pm_id,
            guid=guid,
            muid=muid,
            sid=sid,
            client_session_id=client_session_id,
        )

        confirm_url = _STRIPE_CONFIRM_URL_TPL.format(session_id=cs_session)
        try:
            resp = await session.post(
                confirm_url, headers=headers, data=confirm_data, timeout=timeout,
            )
        except Exception as exc:
            raise GopayLinkError(f"confirm request failed: {exc}") from exc

        if resp.status_code != 200:
            raise GopayLinkError(
                f"confirm HTTP {resp.status_code}: {_summarize_stripe_error(resp.text)}"
            )

        try:
            confirm_result = resp.json()
        except Exception as exc:
            raise GopayLinkError(f"confirm JSON parse failed: {exc}") from exc

        # Stripe có thể trả HTTP 200 nhưng body chứa error object (setup decline).
        # Phải fail-fast với lý do đầy đủ, không để rơi xuống nhánh "missing
        # pm-redirects URL" gây hiểu nhầm.
        if isinstance(confirm_result, dict) and confirm_result.get("error"):
            raise GopayLinkError(
                f"confirm declined: {_summarize_stripe_error(resp.text)}"
            )

        # Extract pm-redirects URL from response
        redirect_url: str | None = None
        confirm_str = resp.text
        match = re.search(r'https://pm-redirects\.stripe\.com/[^"\\]+', confirm_str)
        if match:
            redirect_url = match.group(0)

        if not redirect_url:
            raise GopayLinkError(
                f"confirm response missing pm-redirects URL — keys: {list(confirm_result.keys())[:10]}"
            )

        # Step 3: Follow redirect to get Midtrans URL
        try:
            resp = await session.get(
                redirect_url,
                headers={"User-Agent": headers["User-Agent"]},
                allow_redirects=False,
                timeout=timeout,
            )
        except Exception as exc:
            raise GopayLinkError(f"follow redirect failed: {exc}") from exc

        if resp.status_code in (301, 302, 303, 307, 308):
            midtrans_url = resp.headers.get("Location") or resp.headers.get("location")
            if midtrans_url and _is_midtrans_url(midtrans_url):
                return midtrans_url
            raise GopayLinkError(
                f"redirect Location not Midtrans: {midtrans_url}"
            )

        # Fallback: parse body for midtrans URL
        body = resp.text[:3000]
        match = re.search(r'https://app\.midtrans\.com/snap/v[^\s"\'<>]+', body)
        if match:
            return match.group(0)

        raise GopayLinkError(
            f"cannot extract Midtrans URL from redirect response (status={resp.status_code})"
        )


async def get_gopay_url_from_access_token(
    access_token: str,
    *,
    proxy: str | None = None,
    timeout: float = 30.0,
) -> tuple[str, str | None]:
    """Return a validated promo trial URL plus a separate GoPay URL.

    The returned ``payment_url`` is always a validated amount=0 trial checkout.
    Do not confirm GoPay against that checkout here: Stripe rejects zero-amount
    GoPay setup for this flow.  The Midtrans ``gopay_url`` is created from a
    separate non-promo ID checkout first, then a fresh trial checkout is created
    last so the returned trial URL remains active.
    """
    proxies = {"http": proxy, "https": proxy} if proxy else None
    async with AsyncSession(impersonate=_IMPERSONATE, proxies=proxies) as session:
        paid_payment_url, paid_publishable_key = await _get_checkout_url(
            session,
            access_token,
            timeout=timeout,
            region="ID",
            promo_campaign=False,
        )

    gopay_url = await get_gopay_midtrans_url(
        paid_payment_url,
        proxy=proxy,
        timeout=timeout,
        publishable_key=paid_publishable_key,
    )

    async with AsyncSession(impersonate=_IMPERSONATE, proxies=proxies) as session:
        payment_url, _trial_publishable_key = await _get_trial_checkout(
            session,
            access_token,
            timeout=timeout,
        )
    return payment_url, gopay_url

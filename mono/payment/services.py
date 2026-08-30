# payment/services.py

from __future__ import annotations

import requests
from dataclasses import dataclass
from typing import Optional, List, Dict, Any
from django.conf import settings
from django.db import transaction
from django.contrib.auth import get_user_model

from acm.exceptions import CustomAPIException
from acm import error_codes as EC

from .models import Payment
from .domain_hooks import on_payment_success, on_payment_failure

User = get_user_model()

# ---- Zarinpal endpoints ----
Z_BASE = "https://payment.zarinpal.com/pg/v4/payment"
HEADERS = {"accept": "application/json", "content-type": "application/json"}


@dataclass
class StartPayResult:
    url: str
    payment: Payment
    authority: str


# ---- Helpers ----
def _callback_url() -> str:
    """
    Backend callback URL that Zarinpal will redirect the user to.
    This endpoint should redirect to the frontend (PAYMENT_FRONTEND_RETURN) with ?authority=...
    """
    return settings.PAYMENT_CALLBACK_BASE


def _unverified_list() -> List[Dict]:
    """
    Fetch the list of unverified authorities from Zarinpal.
    Returns [] on non-100 or network error.
    """
    url = f"{Z_BASE}/unVerified.json"
    try:
        r = requests.get(url, headers={"accept": "application/json"}, timeout=15)
        r.raise_for_status()
        data = r.json().get("data", {}) or {}
        if str(data.get("code")) != "100":
            return []
        return data.get("authorities", []) or []
    except requests.RequestException:
        return []


def _request_payment(
    *,
    merchant_id: str,
    amount: int,
    currency: str,
    description: str,
    email: str,
    mobile: Optional[str] = None,
) -> dict:
    url = f"{Z_BASE}/request.json"
    payload = {
        "merchant_id": merchant_id,
        "amount": amount,
        "currency": currency,
        "callback_url": _callback_url(),
        "description": description or "ACM purchase",
        "metadata": {"email": email, **({"mobile": mobile} if mobile else {})},
    }
    r = requests.post(url, json=payload, headers=HEADERS, timeout=20)
    r.raise_for_status()
    return r.json()


def _verify_payment(*, merchant_id: str, amount: int, authority: str) -> dict:
    url = f"{Z_BASE}/verify.json"
    payload = {
        "merchant_id": merchant_id,
        "amount": amount,
        "authority": authority,
    }
    r = requests.post(url, json=payload, headers=HEADERS, timeout=20)
    r.raise_for_status()
    return r.json()


# ---- Public API ----
@transaction.atomic
def initiate_payment_for_target(
    *,
    user: User,
    target_type: str,
    target_id: str,
    amount: int,
    currency: str | None = None,
    description: str = "",
    extra_metadata: Optional[Dict[str, Any]] = None,
) -> StartPayResult:
    """
    1) Check existing PENDING payments for the user; if any authority is still unverified at the gateway,
       attempt to verify it. If verification succeeds for SAME target, raise conflict.
    2) Create a new Zarinpal request and persist a PENDING Payment with the returned authority.
    3) Return StartPay URL.

    ``target_id`` is stored as a string so each domain can use its native ID.
    """
    if not user or not user.is_authenticated:
        raise CustomAPIException(
            code=EC.PAY_AUTH_REQUIRED,
            message="Authentication required",
            status_code=401,
        )

    merchant = settings.ZARINPAL_MERCHANT_ID
    currency = currency or settings.PAYMENT_CURRENCY
    if not merchant:
        raise CustomAPIException(
            code=EC.PAY_MERCHANT_NOT_CONFIGURED,
            message="Payment merchant id not configured",
            status_code=400,
        )

    # Step 1: verify any outstanding PENDING payments present in unVerified list
    pending = list(Payment.objects.select_for_update().filter(user=user, status=Payment.Status.PENDING))
    if pending:
        authorities_map = {item.get("authority"): item for item in _unverified_list()}
        for p in pending:
            if p.authority and p.authority in authorities_map:
                v = _verify_payment(
                    merchant_id=merchant,
                    amount=p.amount,
                    authority=p.authority,
                )
                d = v.get("data", {}) or {}
                code = d.get("code")
                same_target = (
                    p.target_type == target_type
                    and str(p.target_id) == str(target_id)
                )
                p.zarinpal_code = str(code)
                p.zarinpal_message = d.get("message", "") or ""
                if code in (100, 101):
                    p.status = Payment.Status.SUCCESSFUL
                    p.ref_id = str(d.get("ref_id", "") or "")
                    p.card_pan = str(d.get("card_pan", "") or "")
                    p.card_hash = str(d.get("card_hash", "") or "")
                    p.save()
                    on_payment_success(p)
                    # conflict if same purchase already paid
                    if same_target:
                        raise CustomAPIException(
                            code=EC.PAY_EXISTING_SUCCESS,
                            message="Existing successful payment found for this purchase",
                            status_code=409,
                        )
                else:
                    p.status = Payment.Status.FAILED
                    p.save()
                    # A replacement for the same target keeps its existing seat hold.
                    if not same_target:
                        on_payment_failure(p)

    # Step 2: request payment
    try:
        res = _request_payment(
            merchant_id=merchant,
            amount=amount,
            currency=currency,
            description=description or f"{target_type}:{target_id}",
            email=user.email,
            mobile=getattr(user, "phone_number", None),
        )
    except requests.RequestException as e:
        Payment.objects.create(
            user=user,
            target_type=target_type,
            target_id=str(target_id),
            amount=amount,
            currency=currency,
            status=Payment.Status.PG_INITIATE_ERROR,
            zarinpal_message=str(e),
            description=description or "",
            metadata={"stage": "request", "exc": str(e), **(extra_metadata or {})},
        )
        raise CustomAPIException(
            code=EC.PAY_INIT_FAILED,
            message="Payment gateway error while initiating",
            status_code=409,
        )

    d = res.get("data", {}) or {}
    code = d.get("code")
    if code != 100:
        Payment.objects.create(
            user=user,
            target_type=target_type,
            target_id=str(target_id),
            amount=amount,
            currency=currency,
            status=Payment.Status.PG_INITIATE_ERROR,
            zarinpal_code=str(code),
            zarinpal_message=d.get("message", "") or "",
            description=description or "",
            metadata={"stage": "request", "resp": d, **(extra_metadata or {})},
        )
        raise CustomAPIException(
            code=EC.PAY_GATEWAY_REFUSED,
            message=f"Gateway refused: {d.get('message', '')}",
            status_code=409,
        )

    authority = d.get("authority")
    pay = Payment.objects.create(
        user=user,
        target_type=target_type,
        target_id=str(target_id),   # store as string
        amount=amount,
        currency=currency,
        status=Payment.Status.PENDING,
        authority=authority,
        description=description or "",
        metadata={"fee_type": d.get("fee_type"), "fee": d.get("fee"), **(extra_metadata or {})},
    )
    startpay_url = f"https://payment.zarinpal.com/pg/StartPay/{authority}"
    return StartPayResult(url=startpay_url, payment=pay, authority=authority)


@transaction.atomic
def verify_by_authority(*, user: User, authority: str) -> Payment:
    """
    Verify a payment by authority for the given user (frontend passes authority after redirect).
    On success/failure, updates Payment and triggers domain hooks.

    If metadata includes 'reg_id', finalize that registration (parent+children) immediately.
    """
    if not user or not user.is_authenticated:
        raise CustomAPIException(
            code=EC.PAY_AUTH_REQUIRED,
            message="Authentication required",
            status_code=401,
        )

    try:
        p = Payment.objects.select_for_update().get(user=user, authority=authority)
    except Payment.DoesNotExist:
        raise CustomAPIException(
            code=EC.PAY_NOT_FOUND_FOR_USER,
            message="Payment not found for this user/authority",
            status_code=404,
        )

    return _verify_pending_payment(p)


@transaction.atomic
def process_gateway_callback(*, authority: str, gateway_status: str) -> Payment:
    """Process Zarinpal's server callback without relying on frontend authentication."""
    try:
        p = Payment.objects.select_for_update().get(authority=authority)
    except Payment.DoesNotExist:
        raise CustomAPIException(
            code=EC.PAY_NOT_FOUND_FOR_USER,
            message="Payment not found for this authority",
            status_code=404,
        )

    if p.status != Payment.Status.PENDING:
        return p

    # Zarinpal sends Status=NOK when the customer cancels or the payment fails.
    if gateway_status.upper() != "OK":
        p.status = Payment.Status.FAILED
        p.zarinpal_message = f"Gateway callback status: {gateway_status or 'missing'}"
        p.save(update_fields=["status", "zarinpal_message", "updated_at"])
        on_payment_failure(p)
        return p

    return _verify_pending_payment(p)


def _verify_pending_payment(p: Payment) -> Payment:
    """Verify and finalize a locked pending payment."""
    if p.status != Payment.Status.PENDING:
        # Already processed; return as-is
        return p

    merchant = settings.ZARINPAL_MERCHANT_ID

    try:
        res = _verify_payment(
            merchant_id=merchant,
            amount=p.amount,
            authority=p.authority,
        )
    except requests.RequestException as e:
        # A transient gateway/network error is not proof that the payment failed.
        # Leave it pending so the callback or authenticated verify endpoint can retry.
        p.zarinpal_message = str(e)
        p.save(update_fields=["zarinpal_message", "updated_at"])
        return p

    d = res.get("data", {}) or {}
    code = d.get("code")
    p.zarinpal_code = str(code)
    p.zarinpal_message = d.get("message", "") or ""

    if code in (100, 101):
        p.status = Payment.Status.SUCCESSFUL
        p.ref_id = str(d.get("ref_id", "") or "")
        p.card_pan = str(d.get("card_pan", "") or "")
        p.card_hash = str(d.get("card_hash", "") or "")
        p.save()
        on_payment_success(p)
    else:
        p.status = Payment.Status.FAILED
        p.save()
        on_payment_failure(p)
        return p

    return p

def startpay(authority: str) -> str:
    current_payment = Payment.objects.filter(authority=authority).last()
    if not current_payment:
        raise CustomAPIException(
            code=EC.PAY_NOT_FOUND_FOR_USER,
            message="Payment not found for this user/authority",
            status_code=404)
    try:
        if current_payment.target_type == Payment.TargetType.COURSE:
            from presentations.models import Registration
            from presentations.services import set_status_approved

            reg_id = (current_payment.metadata or {}).get("reg_id")
            reg = Registration.objects.get(
                id=int(reg_id),
                user=current_payment.user,
            )
            if reg.status != Registration.Status.APPROVED:
                return set_status_approved(reg).payment_link

        new_payment = initiate_payment_for_target(
            user=current_payment.user,
            target_type=current_payment.target_type,
            target_id=current_payment.target_id,
            amount=current_payment.amount,
            currency=current_payment.currency,
            description=current_payment.description,
            extra_metadata=current_payment.metadata,
        )
    except Exception as e:
        raise CustomAPIException(
            message=f"Failed to initiate payment: {e}",
            code=EC.COMP_PAYMENT_INIT_FAILED,
            status_code=409
        )
    return new_payment.url

# payment/domain_hooks.py
from .models import Payment


def _registration_id(payment: Payment) -> int | None:
    value = (payment.metadata or {}).get("reg_id")
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def on_payment_success(payment: Payment):
    if payment.target_type == Payment.TargetType.COMPETITION:
        # TODO: Fix this
        from competitions.models import TeamRequest
        from competitions.services import mark_payment_final
        tr = TeamRequest.objects.get(id=payment.target_id)
        mark_payment_final(tr)
    elif payment.target_type == Payment.TargetType.COURSE:
        from presentations.models import Registration
        from presentations.services import set_status_final

        reg_id = _registration_id(payment)
        if reg_id is None:
            return
        # The presentation service acquires locks in course-then-registration
        # order, matching registration, capacity update, and promotion flows.
        reg = Registration.objects.only("id", "course_id").get(
            id=reg_id,
            user=payment.user,
        )
        set_status_final([reg])

def on_payment_failure(payment: Payment):
    if payment.target_type == Payment.TargetType.COMPETITION:
        from competitions.models import TeamRequest
        from competitions.services import mark_payment_rejected
        tr = TeamRequest.objects.get(id=payment.target_id)
        mark_payment_rejected(tr)
    elif payment.target_type == Payment.TargetType.COURSE:
        from presentations.services import cancel_registration_for_failed_payment

        reg_id = _registration_id(payment)
        if reg_id is not None:
            cancel_registration_for_failed_payment(reg_id, user=payment.user)

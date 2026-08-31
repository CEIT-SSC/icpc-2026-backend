from django.template import Template, Context
from django.utils import timezone
from django.db import transaction
from .models import EmailTemplate, Notification, BulkJob, BulkRecipient
from .providers import get_email_provider


def render_email(template: EmailTemplate, ctx: dict) -> tuple[str, str, str | None]:
    subject = Template(template.subject).render(Context(ctx))
    html = Template(template.html).render(Context(ctx))
    text = Template(template.text).render(Context(ctx)) if template.text else None
    return subject, html, text


def queue_single_email(
    *,
    to: str,
    template_code: str,
    context: dict,
    subject_override: str | None = None,
    deduplication_key: str | None = None,
) -> Notification:
    tpl = EmailTemplate.objects.get(code=template_code)
    values = {
        "channel": "email",
        "to": to,
        "template": tpl,
        "context": context,
        "subject_override": subject_override or "",
    }
    if deduplication_key:
        n, created = Notification.objects.get_or_create(
            deduplication_key=deduplication_key,
            defaults=values,
        )
    else:
        n = Notification.objects.create(**values)
        created = True
    from .tasks import send_notification_task
    if created:
        send_notification_task.delay(n.id)
    return n

@transaction.atomic
def create_bulk_job(*, template_code: str, recipients: list[dict], job_type: str = "generic") -> BulkJob:
    tpl = EmailTemplate.objects.get(code=template_code)
    job = BulkJob.objects.create(job_type=job_type, template=tpl, total=len(recipients), status="queued")
    BulkRecipient.objects.bulk_create([
        BulkRecipient(job=job, to=item["to"], context=item.get("context", {})) for item in recipients
    ])
    from .tasks import dispatch_bulk_job
    dispatch_bulk_job.delay(job.id)
    return job

def send_otp(destination: str, code: str, channel: str = "email") -> None:
    if channel != "email":
        # Future: integrate SMS
        raise NotImplementedError("SMS not yet implemented")
    queue_single_email(
        to=destination,
        template_code="otp_email",
        context={"code": code},
    )


def send_status_change_email(
    to: str,
    *,
    status_code: str,
    extra: dict | None = None,
    deduplication_key: str | None = None,
) -> None:
    ctx = {"status": status_code, **(extra or {})}
    queue_single_email(
        to=to,
        template_code="status_change",
        context=ctx,
        deduplication_key=deduplication_key,
    )

def send_email_with_custom_template(
    to: str,
    template: str,
    status_code: str,
    extra: dict | None = None,
    deduplication_key: str | None = None,
) -> None:
    ctx = {"status": status_code, **(extra or {})}
    queue_single_email(
        to=to,
        template_code=template,
        context=ctx,
        deduplication_key=deduplication_key,
    )

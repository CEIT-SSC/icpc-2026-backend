from celery import shared_task
from celery.exceptions import Retry
from django.conf import settings
from django.db import transaction
from django.utils import timezone
from .models import Notification, BulkJob, BulkRecipient
from .services import render_email
from .providers import get_email_provider

RATE = settings.NOTIF_EMAIL_RATE
MAX_RETRY = settings.NOTIF_BULK_RETRY_MAX
BACKOFF = settings.NOTIF_BULK_RETRY_BACKOFF
CHUNK = settings.NOTIF_BULK_CHUNK_SIZE

@shared_task(bind=True, rate_limit=RATE, autoretry_for=(Exception,), retry_backoff=True, retry_backoff_max=600, retry_jitter=True, max_retries=MAX_RETRY)
def send_notification_task(self, notification_id: int):
    try:
        # A duplicated Celery delivery must not concurrently send the same
        # logical notification twice. The unique deduplication key prevents
        # duplicate rows; this row lock serializes duplicate task deliveries.
        with transaction.atomic():
            n = (
                Notification.objects.select_for_update()
                .select_related("template")
                .get(id=notification_id)
            )
            if n.status == "sent":
                return
            provider = get_email_provider()
            subject, html, text = render_email(n.template, n.context)
            if n.subject_override:
                subject = n.subject_override
            provider.send(to=n.to, subject=subject, html=html, text=text)
            n.status = "sent"
            n.sent_at = timezone.now()
            n.error = ""
            n.save(update_fields=["status", "sent_at", "error"])
    except Exception as e:
        Notification.objects.exclude(status="sent").filter(id=notification_id).update(
            status="failed",
            error=str(e),
        )
        raise  # let Celery retry

@shared_task(bind=True)
def dispatch_bulk_job(self, job_id: int):
    job = BulkJob.objects.get(id=job_id)
    job.status = "running"
    job.started_at = timezone.now()
    job.save(update_fields=["status", "started_at"])

    # Chunk recipients to avoid huge fan-out and memory
    qs = job.recipients.filter(state="queued").values_list("id", flat=True)
    batch = []
    for rid in qs.iterator(chunk_size=1000):
        batch.append(rid)
        if len(batch) >= CHUNK:
            process_bulk_chunk.delay(job.id, batch)
            batch = []
    if batch:
        process_bulk_chunk.delay(job.id, batch)

@shared_task(bind=True, rate_limit=RATE, autoretry_for=(Exception,), retry_backoff=True, retry_jitter=True, max_retries=MAX_RETRY)
def process_bulk_chunk(self, job_id: int, recipient_ids: list[int]):
    job = BulkJob.objects.select_related("template").get(id=job_id)
    provider = get_email_provider()
    template = job.template

    ok = 0
    fail = 0

    for rid in recipient_ids:
        br = BulkRecipient.objects.get(id=rid)
        try:
            subject, html, text = render_email(template, br.context)
            provider.send(to=br.to, subject=subject, html=html, text=text)
            br.state = "sent"
            br.error = ""
            ok += 1
        except Exception as e:
            br.state = "failed"
            br.error = str(e)
            fail += 1
        br.save(update_fields=["state", "error"])

    # update aggregates atomically
    with transaction.atomic():
        job.sent = job.sent + ok
        job.failed = job.failed + fail
        # mark done if no queued left
        remaining = job.recipients.filter(state="queued").exists()
        if not remaining:
            job.status = "done"
            job.finished_at = timezone.now()
        job.save(update_fields=["sent", "failed", "status", "finished_at"])

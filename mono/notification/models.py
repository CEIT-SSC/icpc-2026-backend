from django.db import models
from django.utils import timezone

# Create your models here.

class EmailTemplate(models.Model):
    """Reusable email templates."""
    code = models.CharField(max_length=64, unique=True, db_index=True)
    subject = models.CharField(max_length=200)
    html = models.TextField()
    text = models.TextField(blank=True)
    created_at = models.DateTimeField(default=timezone.now)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return self.code

class Notification(models.Model):
    """Single-shot notification (email or sms)."""
    CHANNELS = (("email", "Email"), ("sms", "SMS"))
    channel = models.CharField(max_length=8, choices=CHANNELS, default="email")
    to = models.CharField(max_length=255)  # email or phone
    template = models.ForeignKey(EmailTemplate, null=True, blank=True, on_delete=models.SET_NULL)
    context = models.JSONField(default=dict, blank=True)
    subject_override = models.CharField(max_length=200, blank=True)
    deduplication_key = models.CharField(
        max_length=160,
        null=True,
        blank=True,
        unique=True,
        help_text="Optional idempotency key for a logical notification.",
    )
    sent_at = models.DateTimeField(null=True, blank=True)
    status = models.CharField(max_length=16, default="queued")  # queued/sent/failed
    error = models.TextField(blank=True)
    created_at = models.DateTimeField(default=timezone.now)

    def __str__(self):
        return f"{self.channel}:{self.to}:{self.status}"

class BulkJob(models.Model):
    TYPES = (
        ("generic", "Generic"),
        ("reminder", "Reminder"),
        ("invite", "Invite"),
    )
    job_type = models.CharField(max_length=16, choices=TYPES, default="generic")
    template = models.ForeignKey(EmailTemplate, on_delete=models.PROTECT)
    created_at = models.DateTimeField(default=timezone.now)
    started_at = models.DateTimeField(null=True, blank=True)
    finished_at = models.DateTimeField(null=True, blank=True)
    total = models.PositiveIntegerField(default=0)
    sent = models.PositiveIntegerField(default=0)
    failed = models.PositiveIntegerField(default=0)
    status = models.CharField(max_length=16, default="queued")  # queued/running/done/failed

    def __str__(self):
        return f"BulkJob<{self.id}:{self.job_type}:{self.status}>"

class BulkRecipient(models.Model):
    job = models.ForeignKey(BulkJob, on_delete=models.CASCADE, related_name="recipients")
    to = models.CharField(max_length=255)
    context = models.JSONField(default=dict, blank=True)
    state = models.CharField(max_length=16, default="queued")  # queued/sent/failed
    error = models.TextField(blank=True)

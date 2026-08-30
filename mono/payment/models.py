from django.conf import settings
from django.db import models
from django.utils import timezone

class Payment(models.Model):
    class Status(models.TextChoices):
        PENDING = "PENDING", "Pending"
        SUCCESSFUL = "SUCCESSFUL", "Successful"
        FAILED = "FAILED", "Failed"
        PG_INITIATE_ERROR = "PG_INITIATE_ERROR", "Gateway initiate error"

    class TargetType(models.TextChoices):
        COURSE = "COURSE", "CourseRegistration"
        COMPETITION = "COMPETITION", "CompetitionTeamRequest"

    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="payments")
    target_type = models.CharField(max_length=16, choices=TargetType.choices)
    target_id = models.CharField(max_length=256, blank=True, default="")

    amount = models.PositiveIntegerField(help_text="Amount in the selected currency.")
    currency = models.CharField(max_length=3, default="IRT")

    status = models.CharField(max_length=24, choices=Status.choices, default=Status.PENDING)

    authority = models.CharField(max_length=64, blank=True, db_index=True)
    ref_id = models.CharField(max_length=64, blank=True)
    card_pan = models.CharField(max_length=32, blank=True)
    card_hash = models.CharField(max_length=128, blank=True)
    zarinpal_code = models.CharField(max_length=8, blank=True)
    zarinpal_message = models.CharField(max_length=200, blank=True)

    description = models.CharField(max_length=255, blank=True)
    metadata = models.JSONField(default=dict, blank=True)

    created_at = models.DateTimeField(default=timezone.now)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        indexes = [
            models.Index(fields=["user", "status"]),
            models.Index(fields=["authority"]),
            models.Index(fields=["target_type", "target_id", "status"]),
        ]

    def __str__(self):
        return f"Payment<{self.id}:{self.status}:{self.amount}>"

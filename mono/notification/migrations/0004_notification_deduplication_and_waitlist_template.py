from django.db import migrations, models


def seed_waitlist_template(apps, schema_editor):
    EmailTemplate = apps.get_model("notification", "EmailTemplate")
    EmailTemplate.objects.update_or_create(
        code="course_waitlist_promoted",
        defaults={
            "subject": "A slot is now available for {{ course }}",
            "html": (
                "<html><body><p>Hello,</p>"
                "<p>A slot is now available for <strong>{{ course }}</strong>. "
                "You have been moved off the waitlist and can now complete "
                "your registration.</p>"
                "{% if payment_link %}<p><a href=\"{{ payment_link }}\">"
                "Continue to payment</a></p>{% endif %}"
                "</body></html>"
            ),
            "text": (
                "A slot is now available for {{ course }}. You have been moved "
                "off the waitlist and can now complete your registration. "
                "{{ payment_link }}"
            ),
        },
    )


def remove_waitlist_template(apps, schema_editor):
    EmailTemplate = apps.get_model("notification", "EmailTemplate")
    EmailTemplate.objects.filter(code="course_waitlist_promoted").delete()


class Migration(migrations.Migration):
    dependencies = [
        ("notification", "0003_seed_competition_templates"),
    ]

    operations = [
        migrations.AddField(
            model_name="notification",
            name="deduplication_key",
            field=models.CharField(
                blank=True,
                help_text="Optional idempotency key for a logical notification.",
                max_length=160,
                null=True,
                unique=True,
            ),
        ),
        migrations.RunPython(
            seed_waitlist_template,
            reverse_code=remove_waitlist_template,
        ),
    ]

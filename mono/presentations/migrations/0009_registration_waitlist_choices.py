from django.db import migrations, models


def disable_manual_approval(apps, schema_editor):
    Course = apps.get_model("presentations", "Course")
    Course.objects.filter(requires_approval=True).update(requires_approval=False)


class Migration(migrations.Migration):
    dependencies = [
        ("presentations", "0008_individual_offering_types"),
    ]

    operations = [
        migrations.AlterField(
            model_name="course",
            name="requires_approval",
            field=models.BooleanField(
                default=False,
                help_text=(
                    "Legacy setting; registrations now progress automatically "
                    "when capacity is available."
                ),
            ),
        ),
        migrations.RunPython(
            disable_manual_approval,
            reverse_code=migrations.RunPython.noop,
        ),
        migrations.AlterField(
            model_name="registration",
            name="status",
            field=models.CharField(
                choices=[
                    ("SUBMITTED", "Submitted"),
                    ("RESERVED", "Waitlisted"),
                    ("QUEUED", "Queued"),
                    ("APPROVED", "Approved"),
                    ("FINAL", "Finalized"),
                    ("REJECTED", "Rejected"),
                    ("CANCELLED", "Cancelled"),
                ],
                default="SUBMITTED",
                max_length=12,
            ),
        ),
        migrations.AddIndex(
            model_name="registration",
            index=models.Index(
                fields=["course", "status", "submitted_at", "id"],
                name="pres_reg_wait_fifo_idx",
            ),
        ),
    ]

from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ("presentations", "0012_discountcode_registration_discount_code"),
    ]

    operations = [
        migrations.RemoveField(
            model_name="coursesession",
            name="date",
        ),
        migrations.RemoveField(
            model_name="coursesession",
            name="end_time",
        ),
        migrations.RemoveField(
            model_name="coursesession",
            name="is_online",
        ),
        migrations.RemoveField(
            model_name="coursesession",
            name="is_onsite",
        ),
        migrations.RemoveField(
            model_name="coursesession",
            name="start_time",
        ),
    ]

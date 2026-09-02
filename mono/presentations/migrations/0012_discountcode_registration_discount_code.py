import django.core.validators
import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("presentations", "0011_seed_all_access_bundles"),
    ]

    operations = [
        migrations.CreateModel(
            name="DiscountCode",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name="ID",
                    ),
                ),
                ("code", models.CharField(max_length=32, unique=True)),
                (
                    "percent_off",
                    models.PositiveSmallIntegerField(
                        blank=True,
                        null=True,
                        validators=[
                            django.core.validators.MinValueValidator(1),
                            django.core.validators.MaxValueValidator(100),
                        ],
                    ),
                ),
                (
                    "amount_off",
                    models.PositiveIntegerField(
                        blank=True,
                        null=True,
                        validators=[django.core.validators.MinValueValidator(1)],
                    ),
                ),
                (
                    "max_uses",
                    models.PositiveIntegerField(
                        blank=True,
                        null=True,
                        validators=[django.core.validators.MinValueValidator(1)],
                    ),
                ),
                ("used_count", models.PositiveIntegerField(default=0)),
                ("is_active", models.BooleanField(default=True)),
                ("valid_from", models.DateTimeField(blank=True, null=True)),
                ("valid_until", models.DateTimeField(blank=True, null=True)),
                (
                    "course",
                    models.ForeignKey(
                        blank=True,
                        help_text="Leave blank to allow this code on every current bundle.",
                        null=True,
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="discount_codes",
                        to="presentations.course",
                    ),
                ),
            ],
            options={
                "ordering": ["code"],
                "constraints": [
                    models.CheckConstraint(
                        condition=(
                            models.Q(
                                ("amount_off__isnull", True),
                                ("percent_off__isnull", False),
                            )
                            | models.Q(
                                ("amount_off__isnull", False),
                                ("percent_off__isnull", True),
                            )
                        ),
                        name="pres_discount_exactly_one_value",
                    ),
                    models.CheckConstraint(
                        condition=(
                            models.Q(("valid_from__isnull", True))
                            | models.Q(("valid_until__isnull", True))
                            | models.Q(("valid_until__gt", models.F("valid_from")))
                        ),
                        name="pres_discount_valid_window",
                    ),
                    models.CheckConstraint(
                        condition=(
                            models.Q(("max_uses__isnull", True))
                            | models.Q(("used_count__lte", models.F("max_uses")))
                        ),
                        name="pres_discount_usage_within_limit",
                    ),
                ],
            },
        ),
        migrations.AddField(
            model_name="registration",
            name="discount_code",
            field=models.ForeignKey(
                blank=True,
                help_text=(
                    "Discount reserved when this registration price was snapshotted."
                ),
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="registrations",
                to="presentations.discountcode",
            ),
        ),
    ]

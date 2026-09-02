from datetime import time

from django.db import migrations


BUNDLES = (
    {
        "bundle_type": "ALL_ONLINE_PRESENTATIONS",
        "name": "All online presentations",
        "slug": "online-presentations-bundle",
        "price": 599_000,
        "offering_type": "ONLINE_PRESENTATION",
        "member_count": 6,
        "online": True,
        "onsite": False,
        "schedule": ((6, time(17, 0), time(20, 0)), (1, time(17, 0), time(20, 0))),
    },
    {
        "bundle_type": "ALL_IN_PERSON_WORKSHOPS",
        "name": "All in-person workshops",
        "slug": "in-person-workshops-bundle",
        "price": 419_000,
        "offering_type": "IN_PERSON_WORKSHOP",
        "member_count": 3,
        "online": False,
        "onsite": True,
        "schedule": ((3, time(9, 0), time(12, 0)),),
    },
    {
        "bundle_type": "ALL_ONLINE_WORKSHOPS",
        "name": "All online workshops",
        "slug": "online-workshops-bundle",
        "price": 299_000,
        "offering_type": "ONLINE_WORKSHOP",
        "member_count": 3,
        "online": True,
        "onsite": False,
        "schedule": ((3, time(9, 0), time(12, 0)),),
    },
)


def seed_bundles(apps, schema_editor):
    Course = apps.get_model("presentations", "Course")
    ScheduleRule = apps.get_model("presentations", "ScheduleRule")

    if not Course.objects.exists():
        return

    components_by_type = {}
    errors = []
    for config in BUNDLES:
        components = list(
            Course.objects.filter(offering_type=config["offering_type"]).order_by("id")
        )
        components_by_type[config["offering_type"]] = components
        if len(components) != config["member_count"]:
            errors.append(
                f"{config['offering_type']}: expected {config['member_count']}, "
                f"found {len(components)}"
            )
        existing = Course.objects.filter(slug=config["slug"]).first()
        if existing is not None and existing.bundle_type != config["bundle_type"]:
            existing_child_ids = set(existing.children.values_list("id", flat=True))
            expected_child_ids = {component.id for component in components}
            if existing.registrations.exists() or (
                existing_child_ids and existing_child_ids != expected_child_ids
            ):
                errors.append(
                    f"{config['slug']}: existing legacy course has registrations "
                    "or a different child composition"
                )
    if errors:
        raise RuntimeError(
            "Cannot seed ICPC 2026 bundles because the component catalogue is "
            "ambiguous (" + "; ".join(errors) + "). No bundle data was written."
        )

    for config in BUNDLES:
        bundle, _ = Course.objects.get_or_create(
            slug=config["slug"],
            defaults={"name": config["name"]},
        )
        bundle.name = config["name"]
        bundle.bundle_type = config["bundle_type"]
        bundle.offering_type = None
        bundle.capacity = None
        bundle.price = config["price"]
        bundle.online = config["online"]
        bundle.onsite = config["onsite"]
        bundle.requires_approval = False
        bundle.is_active = True
        bundle.save(
            update_fields=[
                "name",
                "bundle_type",
                "offering_type",
                "capacity",
                "price",
                "online",
                "onsite",
                "requires_approval",
                "is_active",
                "updated_at",
            ]
        )
        components = components_by_type[config["offering_type"]]
        # Components remain addressable by the existing session/Skyroom APIs;
        # QuerySet.update deliberately leaves their updated_at timestamps intact.
        Course.objects.filter(id__in=[course.id for course in components]).update(
            is_active=True
        )
        bundle.children.set(components)
        for course in [bundle, *components]:
            for weekday, start_time, end_time in config["schedule"]:
                ScheduleRule.objects.get_or_create(
                    course=course,
                    weekday=weekday,
                    start_time=start_time,
                    end_time=end_time,
                )


def preserve_seeded_bundles(apps, schema_editor):
    # Reversing must never delete a bundle (or its registrations/history).
    pass


class Migration(migrations.Migration):
    atomic = True

    dependencies = [
        ("presentations", "0010_course_bundle_type_and_registration_protection"),
    ]

    operations = [
        migrations.RunPython(seed_bundles, reverse_code=preserve_seeded_bundles),
    ]

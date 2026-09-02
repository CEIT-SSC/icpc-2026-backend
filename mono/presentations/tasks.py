from celery import shared_task


@shared_task(
    autoretry_for=(Exception,),
    retry_backoff=True,
    retry_backoff_max=600,
    retry_jitter=True,
    max_retries=5,
)
def promote_waitlist_task(course_ids):
    """Promote affected direct/bundle queues once per deduplicated product."""
    from .services import promote_waitlists

    if isinstance(course_ids, int):
        course_ids = [course_ids]
    return [reg.id for reg in promote_waitlists(course_ids=course_ids)]

from celery import shared_task


@shared_task(
    autoretry_for=(Exception,),
    retry_backoff=True,
    retry_backoff_max=600,
    retry_jitter=True,
    max_retries=5,
)
def promote_waitlist_task(course_id: int):
    """Retry transient gateway/database failures during automatic promotion."""
    from .services import promote_waitlist

    return [reg.id for reg in promote_waitlist(course_id=course_id)]

from unittest.mock import patch

from django.test import TestCase

from .models import Notification
from .services import queue_single_email
from .tasks import send_notification_task


class NotificationIdempotencyTests(TestCase):
    @patch("notification.tasks.send_notification_task.delay")
    def test_deduplication_key_creates_and_queues_one_notification(self, delay):
        first = queue_single_email(
            to="waitlisted@example.com",
            template_code="course_waitlist_promoted",
            context={"course": "Workshop", "payment_link": "https://pay.example"},
            deduplication_key="course-waitlist-promoted:42",
        )
        second = queue_single_email(
            to="waitlisted@example.com",
            template_code="course_waitlist_promoted",
            context={"course": "Workshop", "payment_link": "https://pay.example"},
            deduplication_key="course-waitlist-promoted:42",
        )

        self.assertEqual(first.id, second.id)
        self.assertEqual(Notification.objects.count(), 1)
        delay.assert_called_once_with(first.id)

    @patch("notification.tasks.get_email_provider")
    def test_repeated_task_delivery_does_not_resend_a_sent_notification(
        self, get_provider
    ):
        with patch("notification.tasks.send_notification_task.delay"):
            notification = queue_single_email(
                to="waitlisted@example.com",
                template_code="course_waitlist_promoted",
                context={
                    "course": "Workshop",
                    "payment_link": "https://pay.example",
                },
                deduplication_key="course-waitlist-promoted:43",
            )

        send_notification_task.run(notification.id)
        send_notification_task.run(notification.id)

        self.assertEqual(get_provider.return_value.send.call_count, 1)
        notification.refresh_from_db()
        self.assertEqual(notification.status, "sent")

from django.db.models import Q
from django.utils import timezone
from django.core.management.base import BaseCommand
from automation.models import DeliveryStatus, OutboxEvent, OutboxStatus, WebhookDelivery
from automation.services.webhooks import dispatch_outbox_event


class Command(BaseCommand):
    help = "Dispatches pending transactional outbox events to registered webhooks (e.g. n8n)."

    def add_arguments(self, parser):
        parser.add_argument(
            "--limit", type=int, default=100, help="Max number of events to process"
        )

    def handle(self, *args, **options):
        limit = options.get("limit", 100)
        now = timezone.now()
        due_failed_event_ids = WebhookDelivery.objects.filter(
            status=DeliveryStatus.FAILED,
        ).filter(Q(next_retry_at__isnull=True) | Q(next_retry_at__lte=now)).values(
            "outbox_event_id"
        )
        events = OutboxEvent.objects.filter(
            Q(status=OutboxStatus.PENDING)
            | Q(
                status__in=(OutboxStatus.FAILED, OutboxStatus.PROCESSED),
                id__in=due_failed_event_ids,
            )
        ).order_by("occurred_at")[:limit]

        count = 0
        for event in events:
            deliveries = dispatch_outbox_event(event)
            count += 1
            self.stdout.write(
                self.style.SUCCESS(
                    f"Processed event {event.event_id} [{event.event_type}]: {len(deliveries)} webhook deliveries dispatched."
                )
            )

        self.stdout.write(self.style.SUCCESS(f"Finished processing {count} outbox events."))

import time
from django.core.management.base import BaseCommand
from automation.models import OutboxEvent, OutboxStatus
from automation.services.webhooks import dispatch_outbox_event


class Command(BaseCommand):
    help = "Dispatches pending transactional outbox events to registered webhooks (e.g. n8n)."

    def add_arguments(self, parser):
        parser.add_arguments = parser.add_argument(
            "--limit", type=int, default=100, help="Max number of events to process"
        )

    def handle(self, *args, **options):
        limit = options.get("limit", 100)
        pending_events = OutboxEvent.objects.filter(status=OutboxStatus.PENDING).order_by("occurred_at")[:limit]

        count = 0
        for event in pending_events:
            deliveries = dispatch_outbox_event(event)
            count += 1
            self.stdout.write(
                self.style.SUCCESS(
                    f"Processed event {event.event_id} [{event.event_type}]: {len(deliveries)} webhook deliveries dispatched."
                )
            )

        self.stdout.write(self.style.SUCCESS(f"Finished processing {count} outbox events."))

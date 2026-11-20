from django.core.management.base import BaseCommand
from chats.utils import purge_expired_disappearing_messages

class Command(BaseCommand):
    help = 'Purges messages and attachments that have exceeded their conversation disappearing duration timer.'

    def add_arguments(self, parser):
        parser.add_argument(
            '--dry-run',
            action='store_true',
            help='Simulate the purge without deleting any messages or files.',
        )

    def handle(self, *args, **options):
        dry_run = options.get('dry_run', False)
        mode_str = "[DRY-RUN] " if dry_run else ""
        self.stdout.write(f"{mode_str}Starting purge of expired disappearing messages...")

        result = purge_expired_disappearing_messages(dry_run=dry_run)

        self.stdout.write(
            self.style.SUCCESS(
                f"{mode_str}Purge completed successfully! "
                f"Conversations scanned: {result['conversations_checked']}, "
                f"Messages purged: {result['messages_purged']}"
            )
        )


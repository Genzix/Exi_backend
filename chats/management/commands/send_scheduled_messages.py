from django.core.management.base import BaseCommand
from chats.tasks import check_and_deliver_time_capsules

class Command(BaseCommand):
    help = 'Sends scheduled messages and delivers unlocked Time Capsules & Secret Letters that are due.'

    def handle(self, *args, **options):
        count = check_and_deliver_time_capsules()
        self.stdout.write(self.style.SUCCESS(f'Successfully processed and sent {count} scheduled messages/capsules.'))

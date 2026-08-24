from django.core.management.base import BaseCommand
from django.utils import timezone
from asgiref.sync import async_to_sync
from channels.layers import get_channel_layer
from chats.models import Message, ConversationParticipant
from chats.serializers import MessageSerializer
from chats.utils import notify_new_message
from django.db.models import F

class Command(BaseCommand):
    help = 'Sends scheduled messages that are due.'

    def handle(self, *args, **options):
        now = timezone.now()
        messages_to_send = Message.objects.filter(
            is_scheduled=True,
            scheduled_for__lte=now,
            status='scheduled'
        )

        count = 0
        channel_layer = get_channel_layer()

        for msg in messages_to_send:
            # Mark as sent
            msg.status = 'sent'
            msg.is_scheduled = False
            msg.save(update_fields=['status', 'is_scheduled'])
            
            # Update conversation timestamp
            msg.conversation.updated_at = timezone.now()
            msg.conversation.save(update_fields=['updated_at'])

            # Increment unread count for other participants
            ConversationParticipant.objects.filter(
                conversation_id=msg.conversation_id
            ).exclude(
                user_id=msg.sender_id
            ).update(unread_count=F('unread_count') + 1, deleted_at=None)

            # Broadcast over WebSocket
            message_data = MessageSerializer(msg).data
            async_to_sync(channel_layer.group_send)(
                f"chat_{msg.conversation_id}",
                {
                    "type": "chat_message",
                    "message": message_data,
                }
            )

            # Push notifications
            notify_new_message(msg)

            count += 1

        self.stdout.write(self.style.SUCCESS(f'Successfully sent {count} scheduled messages.'))

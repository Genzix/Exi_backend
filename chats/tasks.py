"""
Celery / Background tasks for chats application.
Handles automated checking and delivery of scheduled Time Capsules & Secret Letters.
"""
import json
from django.utils import timezone
from asgiref.sync import async_to_sync
from channels.layers import get_channel_layer
from django.db.models import F

try:
    from celery import shared_task
except ImportError:
    # Fallback decorator if celery is not installed in the environment
    def shared_task(func):
        func.delay = lambda *args, **kwargs: func(*args, **kwargs)
        return func


@shared_task
def check_and_deliver_time_capsules():
    """
    Step 8 & 9 of Time Capsule & Secret Letter journey:
    Automatically checks scheduled Time Capsules & Secret Letters whose unlock
    time has arrived, transitions them from 'scheduled' to 'sent', marks them unlocked,
    broadcasts WebSocket events, and sends notifications to recipients.
    """
    from chats.models import Message, ConversationParticipant, Notification
    from chats.serializers import MessageSerializer
    from chats.utils import notify_new_message, _sender_display_name

    now = timezone.now()
    due_messages = Message.objects.filter(
        is_scheduled=True,
        scheduled_for__lte=now,
        status='scheduled'
    ).select_related('conversation', 'sender')

    count = 0
    channel_layer = get_channel_layer()

    for msg in due_messages:
        # Mark message as sent/delivered
        msg.status = 'sent'
        msg.is_scheduled = False
        msg.save(update_fields=['status', 'is_scheduled'])

        # Update JSON payload is_unlocked
        try:
            cdata = json.loads(msg.content)
            if isinstance(cdata, dict):
                cdata['is_unlocked'] = True
                cdata['unlocked_at'] = now.isoformat()
                msg.content = json.dumps(cdata)
                msg.save(update_fields=['content'])
        except Exception:
            pass

        # Update conversation timestamp
        msg.conversation.updated_at = now
        msg.conversation.save(update_fields=['updated_at'])

        # Increment unread count for other participants
        ConversationParticipant.objects.filter(
            conversation_id=msg.conversation_id
        ).exclude(
            user_id=msg.sender_id
        ).update(unread_count=F('unread_count') + 1, deleted_at=None)

        # Broadcast message update over WebSocket
        if channel_layer:
            message_data = MessageSerializer(msg).data
            async_to_sync(channel_layer.group_send)(
                f"chat_{msg.conversation_id}",
                {
                    "type": "chat_message",
                    "message": message_data,
                }
            )

            # Step 9: Broadcast real-time capsule_unlocked alert
            if msg.message_type == 'capsule':
                try:
                    cdata = json.loads(msg.content)
                    title = cdata.get('title') or ('Secret Letter' if cdata.get('capsule_type') == 'secret_letter' else 'Time Capsule')
                    capsule_type = cdata.get('capsule_type', 'time_capsule')
                except Exception:
                    title = 'Time Capsule'
                    capsule_type = 'time_capsule'

                async_to_sync(channel_layer.group_send)(
                    f"chat_{msg.conversation_id}",
                    {
                        "type": "capsule_unlocked",
                        "message_id": str(msg.id),
                        "title": title,
                        "capsule_type": capsule_type,
                        "unlocked_at": now.isoformat(),
                        "sender_id": msg.sender_id,
                    }
                )

        # Step 9: Notify recipient with push and in-app alerts
        notify_new_message(msg)

        # Create system notification record for unlocked capsule
        if msg.message_type == 'capsule':
            try:
                cdata = json.loads(msg.content)
                title = cdata.get('title') or 'Time Capsule'
            except Exception:
                title = 'Time Capsule'

            recipients = ConversationParticipant.objects.filter(
                conversation_id=msg.conversation_id
            ).exclude(user_id=msg.sender_id).select_related('user')

            sender_name = _sender_display_name(msg.sender)
            for part in recipients:
                Notification.objects.create(
                    recipient=part.user,
                    sender=msg.sender,
                    conversation=msg.conversation,
                    notification_type='system',
                    title="Time Capsule ready to unlock! 🎁",
                    body=f"{sender_name}'s scheduled {title} is now ready to open.",
                )

        count += 1

    return count


@shared_task
def deliver_scheduled_messages():
    """Alias for check_and_deliver_time_capsules."""
    return check_and_deliver_time_capsules()

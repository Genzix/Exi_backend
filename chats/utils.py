import json
import logging
import urllib.error
import urllib.request

from asgiref.sync import async_to_sync
from channels.layers import get_channel_layer
from django.conf import settings
from django.db import models
from django.db.models import Sum

from accounts.models import BlockedUser, UserDevice, UserPrivacySettings

from .serializers import MessageSerializer, CallSerializer

logger = logging.getLogger(__name__)


def broadcast_chat_message(conversation_id, message):
    channel_layer = get_channel_layer()
    if channel_layer is not None:
        async_to_sync(channel_layer.group_send)(
            f"chat_{conversation_id}",
            {
                "type": "chat_message",
                "message": MessageSerializer(message).data,
            },
        )
    notify_new_message(message)


def broadcast_message_status(conversation_id, message_id, status_value):
    channel_layer = get_channel_layer()
    if channel_layer is None:
        return
    async_to_sync(channel_layer.group_send)(
        f"chat_{conversation_id}",
        {
            "type": "message_status",
            "message_id": str(message_id),
            "status": status_value,
        },
    )


def broadcast_typing_status(conversation_id, user_id, is_typing):
    channel_layer = get_channel_layer()
    if channel_layer is None:
        return
    async_to_sync(channel_layer.group_send)(
        f"chat_{conversation_id}",
        {
            "type": "typing_status",
            "user_id": user_id,
            "is_typing": is_typing,
        },
    )


TYPING_TTL_SECONDS = 5


def update_typing_cache(conversation_id, user_id, is_typing):
    from django.core.cache import cache
    import time

    key = f"typing:{conversation_id}"
    typers = cache.get(key, {})
    user_key = str(user_id)

    if is_typing:
        typers[user_key] = time.time()
    else:
        typers.pop(user_key, None)

    cache.set(key, typers, timeout=60)


def get_active_typers(conversation_id):
    from django.core.cache import cache
    import time

    key = f"typing:{conversation_id}"
    typers = cache.get(key, {})
    now = time.time()
    return [
        int(user_id)
        for user_id, timestamp in typers.items()
        if now - timestamp < TYPING_TTL_SECONDS
    ]


def set_typing_status(conversation_id, user_id, is_typing):
    update_typing_cache(conversation_id, user_id, is_typing)
    broadcast_typing_status(conversation_id, user_id, is_typing)


def is_blocked(user_a, user_b):
    """True if either user has blocked the other."""
    if not user_a or not user_b:
        return False
    return BlockedUser.objects.filter(
        models.Q(blocker=user_a, blocked=user_b)
        | models.Q(blocker=user_b, blocked=user_a)
    ).exists()


def blocked_user_ids(user):
    """User IDs that should be hidden from search/interaction."""
    blocked_by_me = BlockedUser.objects.filter(blocker=user).values_list('blocked_id', flat=True)
    blocked_me = BlockedUser.objects.filter(blocked=user).values_list('blocker_id', flat=True)
    return set(blocked_by_me) | set(blocked_me)


# ---------- Abuse protection ----------

MESSAGE_RATE_LIMIT = 30
MESSAGE_RATE_WINDOW = 60


def other_direct_participant(conversation, user):
    if conversation.type != 'direct':
        return None
    return conversation.participants.exclude(id=user.id).first()


def check_send_permission(user, conversation):
    """Enforce blocks and the recipient's message privacy. Returns (ok, detail)."""
    from accounts.utils import can_message

    other = other_direct_participant(conversation, user)
    if other is None:
        return True, None

    if is_blocked(user, other):
        return False, 'You can no longer send messages to this user.'
    if not can_message(user, other):
        return False, 'This user does not accept messages from you.'
    return True, None


def check_message_flood(user):
    """Throttle outbound messages per user. Returns (allowed, retry_after)."""
    from accounts.utils import rate_limit

    return rate_limit(f'msgflood:{user.id}', MESSAGE_RATE_LIMIT, MESSAGE_RATE_WINDOW)


def read_receipts_enabled(user):
    from accounts.utils import privacy_settings_or_default

    return privacy_settings_or_default(user).read_receipts_enabled


def _serialize_call(call, request=None):
    context = {'request': request} if request else {}
    return CallSerializer(call, context=context).data


def broadcast_to_user(user_id, event_type, payload):
    channel_layer = get_channel_layer()
    if channel_layer is None:
        return
    async_to_sync(channel_layer.group_send)(
        f'user_calls_{user_id}',
        {
            'type': 'call_event',
            'event_type': event_type,
            'payload': payload,
        },
    )


def broadcast_call_event(call, event_type, request=None):
    data = _serialize_call(call, request)
    broadcast_to_user(call.caller_id, event_type, data)
    broadcast_to_user(call.callee_id, event_type, data)


def broadcast_call_signal(call_id, sender_id, signal_type, data):
    channel_layer = get_channel_layer()
    if channel_layer is None:
        return
    async_to_sync(channel_layer.group_send)(
        f'call_{call_id}',
        {
            'type': 'webrtc_signal',
            'sender_id': sender_id,
            'signal_type': signal_type,
            'data': data,
        },
    )


def get_badge_count(user):
    """App-icon badge: sum of unread messages across non-deleted, non-archived chats."""
    from .models import ConversationParticipant

    return (
        ConversationParticipant.objects.filter(
            user=user,
            deleted_at__isnull=True,
            is_archived=False,
        ).aggregate(total=Sum('unread_count'))['total']
        or 0
    )


def get_notification_unread_count(user):
    from .models import Notification

    return Notification.objects.filter(recipient=user, is_read=False).count()


def _user_notification_prefs(user):
    prefs, _ = UserPrivacySettings.objects.get_or_create(user=user)
    return prefs


def _message_preview(message):
    if message.message_type == 'text':
        text = (message.content or '').strip()
        return text[:120] if text else 'New message'
    labels = {
        'image': 'Sent a photo',
        'video': 'Sent a video',
        'document': 'Sent a document',
        'audio': 'Sent an audio file',
        'voice': 'Sent a voice note',
    }
    return labels.get(message.message_type, 'New message')


def _sender_display_name(user):
    return user.display_name or user.full_name or user.username or 'Someone'


def broadcast_badge_update(user_id, badge_count):
    channel_layer = get_channel_layer()
    if channel_layer is None:
        return
    async_to_sync(channel_layer.group_send)(
        f'user_calls_{user_id}',
        {
            'type': 'notification_event',
            'event_type': 'badge_update',
            'payload': {'badge_count': badge_count},
            'badge_count': badge_count,
        },
    )


def broadcast_notification(user_id, notification_payload, badge_count):
    channel_layer = get_channel_layer()
    if channel_layer is None:
        return
    async_to_sync(channel_layer.group_send)(
        f'user_calls_{user_id}',
        {
            'type': 'notification_event',
            'event_type': 'new_notification',
            'payload': notification_payload,
            'badge_count': badge_count,
        },
    )
    broadcast_badge_update(user_id, badge_count)


def send_push_to_user(user, title, body, data=None, badge_count=0):
    """Send FCM push to all active devices with a push_token. No-op without FCM_SERVER_KEY."""
    server_key = getattr(settings, 'FCM_SERVER_KEY', '') or ''
    if not server_key:
        logger.debug('FCM_SERVER_KEY not set; skipping push for user %s', user.id)
        return 0

    tokens = list(
        UserDevice.objects.filter(
            user=user,
            is_active=True,
        )
        .exclude(push_token='')
        .values_list('push_token', flat=True)
    )
    if not tokens:
        return 0

    data = {str(k): str(v) for k, v in (data or {}).items()}
    data.setdefault('badge_count', str(badge_count))

    payload = {
        'registration_ids': tokens,
        'notification': {
            'title': title,
            'body': body,
            'sound': 'default',
            'badge': badge_count,
        },
        'data': data,
        'priority': 'high',
        'content_available': True,
    }

    request = urllib.request.Request(
        'https://fcm.googleapis.com/fcm/send',
        data=json.dumps(payload).encode('utf-8'),
        headers={
            'Authorization': f'key={server_key}',
            'Content-Type': 'application/json',
        },
        method='POST',
    )

    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            result = json.loads(response.read().decode('utf-8'))
            # Clear invalid tokens
            results = result.get('results') or []
            for token, item in zip(tokens, results):
                if item.get('error') in ('NotRegistered', 'InvalidRegistration'):
                    UserDevice.objects.filter(push_token=token).update(push_token='')
            return int(result.get('success') or 0)
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, ValueError) as exc:
        logger.warning('FCM push failed for user %s: %s', user.id, exc)
        return 0


def create_notification(
    *,
    recipient,
    sender=None,
    notification_type='new_message',
    title,
    body,
    conversation=None,
    message=None,
    data=None,
    send_push=True,
    send_in_app=True,
):
    from .serializers import NotificationSerializer
    from .models import Notification

    prefs = _user_notification_prefs(recipient)
    notification = None

    if send_in_app and prefs.in_app_notifications_enabled:
        notification = Notification.objects.create(
            recipient=recipient,
            sender=sender,
            notification_type=notification_type,
            title=title,
            body=body,
            conversation=conversation,
            message=message,
            data=data or {},
        )
        payload = NotificationSerializer(notification).data
        badge = get_badge_count(recipient)
        broadcast_notification(recipient.id, payload, badge)

    if send_push and prefs.push_notifications_enabled:
        badge = get_badge_count(recipient)
        push_data = {
            'type': notification_type,
            'notification_id': str(notification.id) if notification else '',
            'conversation_id': str(conversation.id) if conversation else '',
            'message_id': str(message.id) if message else '',
            **{k: v for k, v in (data or {}).items()},
        }
        send_push_to_user(recipient, title, body, data=push_data, badge_count=badge)

    return notification


def notify_new_message(message):
    """In-app + push alerts for recipients of a new chat message."""
    from .models import ConversationParticipant

    sender = message.sender
    conversation = message.conversation
    title = _sender_display_name(sender)
    body = _message_preview(message)

    participants = (
        ConversationParticipant.objects.filter(conversation=conversation)
        .exclude(user_id=sender.id)
        .select_related('user')
    )

    for participant in participants:
        if participant.is_muted:
            continue
        create_notification(
            recipient=participant.user,
            sender=sender,
            notification_type='new_message',
            title=title,
            body=body,
            conversation=conversation,
            message=message,
            data={
                'conversation_id': str(conversation.id),
                'message_id': str(message.id),
                'message_type': message.message_type,
            },
        )


def notify_missed_call(call):
    title = 'Missed call'
    body = f'Missed {_sender_display_name(call.caller)} call'
    create_notification(
        recipient=call.callee,
        sender=call.caller,
        notification_type='missed_call',
        title=title,
        body=body,
        conversation=call.conversation,
        data={
            'call_id': str(call.id),
            'call_type': call.call_type,
            'conversation_id': str(call.conversation_id),
        },
    )

import json

from asgiref.sync import sync_to_async
from channels.generic.websocket import AsyncWebsocketConsumer
from django.core.serializers.json import DjangoJSONEncoder
from django.utils import timezone
from django.db import models

from .utils import broadcast_call_signal


class ChatConsumer(AsyncWebsocketConsumer):
    async def connect(self):
        self.conversation_id = self.scope["url_route"]["kwargs"]["chat_id"]
        self.room_group_name = f"chat_{self.conversation_id}"
        user = self.scope["user"]

        if user.is_anonymous:
            await self.close()
            return

        is_participant = await self._is_participant(user.id, self.conversation_id)
        if not is_participant:
            await self.close()
            return

        await self.channel_layer.group_add(self.room_group_name, self.channel_name)
        await self.accept()

        await sync_to_async(user.set_online)()
        self.shares_presence = await self._shares_presence(user.id)
        if self.shares_presence:
            await self.channel_layer.group_send(
                self.room_group_name,
                {
                    "type": "presence_update",
                    "user_id": user.id,
                    "status": "online",
                },
            )

    async def disconnect(self, close_code):
        user = self.scope.get("user")
        if user and not user.is_anonymous:
            # Room leave ≠ global logout; only refresh last_seen
            await sync_to_async(self._touch_last_seen)(user)
            if getattr(self, "shares_presence", False):
                await self.channel_layer.group_send(
                    self.room_group_name,
                    {
                        "type": "presence_update",
                        "user_id": user.id,
                        "status": "offline",
                    },
                )
            await self.channel_layer.group_discard(
                self.room_group_name, self.channel_name
            )

    async def receive(self, text_data):
        data = json.loads(text_data)
        action = data.get("action")

        if action == "typing":
            is_typing = data.get("is_typing", False)
            from .utils import set_typing_status

            await sync_to_async(set_typing_status)(
                self.conversation_id, self.scope["user"].id, is_typing
            )
        elif action == "send_message":
            content = (data.get("content") or "").strip()
            ciphertext = (data.get("ciphertext") or "").strip()
            if not content and not ciphertext:
                await self._send_error("Content or ciphertext is required.")
                return

            allowed, detail = await self._check_send_allowed(
                self.scope["user"].id, self.conversation_id
            )
            if not allowed:
                await self._send_error(detail)
                return

            message_data = await self._create_message(
                self.scope["user"].id,
                self.conversation_id,
                content,
                ciphertext=ciphertext,
                sender_device_id=(data.get("sender_device_id") or "").strip(),
                encryption_version=data.get("encryption_version") or 1,
            )
            await self.channel_layer.group_send(
                self.room_group_name,
                {
                    "type": "chat_message",
                    "message": message_data,
                },
            )
        elif action == "mark_delivered":
            message_id = data.get("message_id")
            if message_id:
                result = await self._update_message_status(
                    self.scope["user"].id, message_id, "delivered"
                )
                if result:
                    await self.channel_layer.group_send(
                        self.room_group_name,
                        {
                            "type": "message_status",
                            "message_id": str(result["id"]),
                            "status": result["status"],
                        },
                    )
        elif action == "mark_read":
            message_id = data.get("message_id")
            if message_id:
                result = await self._update_message_status(
                    self.scope["user"].id, message_id, "read"
                )
                if result:
                    await self.channel_layer.group_send(
                        self.room_group_name,
                        {
                            "type": "message_status",
                            "message_id": str(result["id"]),
                            "status": result["status"],
                        },
                    )

    async def chat_message(self, event):
        await self.send(
            text_data=json.dumps(
                {
                    "type": "chat_message",
                    "message": event["message"],
                },
                cls=DjangoJSONEncoder,
            )
        )

    async def message_status(self, event):
        await self.send(
            text_data=json.dumps(
                {
                    "type": "message_status",
                    "message_id": event["message_id"],
                    "status": event["status"],
                },
                cls=DjangoJSONEncoder,
            )
        )

    async def typing_status(self, event):
        if event["user_id"] != self.scope["user"].id:
            await self.send(
                text_data=json.dumps(
                    {
                        "type": "typing_status",
                        "user_id": event["user_id"],
                        "is_typing": event["is_typing"],
                    },
                    cls=DjangoJSONEncoder,
                )
            )

    async def presence_update(self, event):
        if event["user_id"] != self.scope["user"].id:
            await self.send(
                text_data=json.dumps(
                    {
                        "type": "presence_update",
                        "user_id": event["user_id"],
                        "status": event["status"],
                    },
                    cls=DjangoJSONEncoder,
                )
            )

    async def reaction_update(self, event):
        await self.send(
            text_data=json.dumps(
                {
                    "type": "reaction_update",
                    "message_id": event["message_id"],
                    "user_id": event["user_id"],
                    "reaction": event["reaction"],
                    "action": event["action"],
                },
                cls=DjangoJSONEncoder,
            )
        )

    async def _send_error(self, detail):
        await self.send(
            text_data=json.dumps(
                {"type": "error", "detail": detail},
                cls=DjangoJSONEncoder,
            )
        )

    @staticmethod
    def _touch_last_seen(user):
        user.last_seen = timezone.now()
        user.save(update_fields=["last_seen"])

    @sync_to_async
    def _check_send_allowed(self, user_id, conversation_id):
        from django.contrib.auth import get_user_model

        from .models import Conversation
        from .utils import check_message_flood, check_send_permission

        user = get_user_model().objects.get(id=user_id)

        allowed, retry_after = check_message_flood(user)
        if not allowed:
            return False, f"You are sending messages too quickly. Retry in {retry_after}s."

        conversation = Conversation.objects.get(id=conversation_id)
        return check_send_permission(user, conversation)

    @sync_to_async
    def _is_participant(self, user_id, conversation_id):
        from .models import ConversationParticipant

        return ConversationParticipant.objects.filter(
            user_id=user_id, conversation_id=conversation_id
        ).exists()

    @sync_to_async
    def _shares_presence(self, user_id):
        """Honour the user's 'show online status' privacy control."""
        from django.contrib.auth import get_user_model

        from accounts.utils import privacy_settings_or_default

        user = get_user_model().objects.get(id=user_id)
        return privacy_settings_or_default(user).show_online_status

    @sync_to_async
    def _create_message(
        self,
        user_id,
        conversation_id,
        content,
        ciphertext="",
        sender_device_id="",
        encryption_version=1,
    ):
        from django.db.models import F

        from .models import Conversation, ConversationParticipant, Message
        from .serializers import MessageSerializer

        is_encrypted = bool(ciphertext)
        msg = Message.objects.create(
            conversation_id=conversation_id,
            sender_id=user_id,
            content="" if is_encrypted else content,
            ciphertext=ciphertext,
            is_encrypted=is_encrypted,
            encryption_version=encryption_version if is_encrypted else 0,
            sender_device_id=sender_device_id if is_encrypted else "",
        )
        Conversation.objects.filter(id=conversation_id).update(
            updated_at=timezone.now()
        )
        ConversationParticipant.objects.filter(conversation_id=conversation_id).exclude(
            user_id=user_id
        ).update(unread_count=F("unread_count") + 1, deleted_at=None)
        from .utils import notify_new_message

        notify_new_message(msg)
        return MessageSerializer(msg).data

    @sync_to_async
    def _update_message_status(self, user_id, message_id, new_status):
        from django.contrib.auth import get_user_model

        from .models import ConversationParticipant, Message
        from .serializers import MessageSerializer
        from .utils import broadcast_badge_update, get_badge_count, read_receipts_enabled

        try:
            msg = Message.objects.get(id=message_id)
        except Message.DoesNotExist:
            return None

        if msg.sender_id == user_id:
            return None
        if not ConversationParticipant.objects.filter(
            user_id=user_id, conversation_id=msg.conversation_id
        ).exists():
            return None

        user = get_user_model().objects.get(id=user_id)
        share_receipt = new_status == "delivered" or read_receipts_enabled(user)

        if share_receipt:
            msg.status = new_status
            msg.save(update_fields=["status"])

        if new_status == "read":
            ConversationParticipant.objects.filter(
                user_id=user_id, conversation_id=msg.conversation_id
            ).update(unread_count=0)
            broadcast_badge_update(user_id, get_badge_count(user))

        if not share_receipt:
            return None

        return MessageSerializer(msg).data


class UserCallConsumer(AsyncWebsocketConsumer):
    """Receive incoming/outgoing call notifications for the authenticated user."""

    async def connect(self):
        user = self.scope['user']
        if user.is_anonymous:
            await self.close()
            return

        self.user_group = f'user_calls_{user.id}'
        await self.channel_layer.group_add(self.user_group, self.channel_name)
        await self.accept()

    async def disconnect(self, close_code):
        if hasattr(self, 'user_group'):
            await self.channel_layer.group_discard(self.user_group, self.channel_name)

    async def call_event(self, event):
        await self.send(
            text_data=json.dumps(
                {
                    'type': event['event_type'],
                    'call': event['payload'],
                },
                cls=DjangoJSONEncoder,
            )
        )

    async def notification_event(self, event):
        await self.send(
            text_data=json.dumps(
                {
                    'type': event['event_type'],
                    'notification': event.get('payload'),
                    'badge_count': event.get('badge_count', 0),
                },
                cls=DjangoJSONEncoder,
            )
        )


class CallSignalingConsumer(AsyncWebsocketConsumer):
    """WebRTC signaling: offer, answer, ICE candidates."""

    async def connect(self):
        self.call_id = self.scope['url_route']['kwargs']['call_id']
        self.room_group = f'call_{self.call_id}'
        user = self.scope['user']

        if user.is_anonymous:
            await self.close()
            return

        is_participant = await self._is_call_participant(user.id, self.call_id)
        if not is_participant:
            await self.close()
            return

        await self.channel_layer.group_add(self.room_group, self.channel_name)
        await self.accept()

    async def disconnect(self, close_code):
        if hasattr(self, 'room_group'):
            await self.channel_layer.group_discard(self.room_group, self.channel_name)

    async def receive(self, text_data):
        data = json.loads(text_data)
        action = data.get('action')

        if action in ('offer', 'answer', 'ice_candidate'):
            payload = data.get('sdp') if action in ('offer', 'answer') else data.get('candidate')
            if not payload:
                await self.send(
                    text_data=json.dumps(
                        {'type': 'error', 'detail': 'Missing SDP or candidate.'},
                        cls=DjangoJSONEncoder,
                    )
                )
                return

            signal_type = 'ice_candidate' if action == 'ice_candidate' else action
            await sync_to_async(broadcast_call_signal)(
                self.call_id,
                self.scope['user'].id,
                signal_type,
                payload,
            )

    async def webrtc_signal(self, event):
        if event['sender_id'] == self.scope['user'].id:
            return
        await self.send(
            text_data=json.dumps(
                {
                    'type': event['signal_type'],
                    'sender_id': event['sender_id'],
                    'data': event['data'],
                },
                cls=DjangoJSONEncoder,
            )
        )

    @sync_to_async
    def _is_call_participant(self, user_id, call_id):
        from .models import Call

        return Call.objects.filter(id=call_id).filter(
            models.Q(caller_id=user_id) | models.Q(callee_id=user_id)
        ).exists()

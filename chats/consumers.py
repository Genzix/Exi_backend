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
        elif action in ("set_permissions", "update_permissions"):
            from .chat_permissions import update_chat_permissions
            perms = await sync_to_async(update_chat_permissions)(
                self.conversation_id, self.scope["user"].id, data
            )
            await self.send(
                text_data=json.dumps({
                    "type": "chat_permissions_updated",
                    "chat_id": str(self.conversation_id),
                    "permissions": perms,
                })
            )
            await self.channel_layer.group_send(
                self.room_group_name,
                {
                    "type": "chat_permissions_broadcast",
                    "user_id": self.scope["user"].id,
                    "permissions": perms,
                },
            )
        elif action in ("get_permissions",):
            from .chat_permissions import get_chat_permissions_display
            perms = await sync_to_async(get_chat_permissions_display)(
                self.conversation_id, self.scope["user"].id
            )
            await self.send(
                text_data=json.dumps({
                    "type": "chat_permissions",
                    "chat_id": str(self.conversation_id),
                    "permissions": perms,
                })
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
        elif action == "send_attachment":
            att_type = data.get("attachment_type") or data.get("type")
            att_payload = data.get("data") or data.get("payload") or {}
            allowed, detail = await self._check_send_allowed(
                self.scope["user"].id, self.conversation_id
            )
            if not allowed:
                await self._send_error(detail)
                return

            message_data = await self._create_attachment_message(
                self.scope["user"].id,
                self.conversation_id,
                att_type,
                att_payload,
            )
            if message_data:
                await self.channel_layer.group_send(
                    self.room_group_name,
                    {
                        "type": "chat_message",
                        "message": message_data,
                    },
                )
        elif action == "open_exi_pop":
            msg_id = data.get("message_id")
            if msg_id:
                pop_info = await self._mark_exi_pop_opened(
                    self.scope["user"].id, self.conversation_id, msg_id
                )
                if pop_info:
                    await self.channel_layer.group_send(
                        self.room_group_name,
                        {
                            "type": "exi_pop_opened",
                            **pop_info,
                        },
                    )
        elif action == "vote_poll":
            msg_id = data.get("message_id")
            option_ids = data.get("option_ids") or []
            if msg_id and option_ids:
                poll_info = await self._record_poll_vote(
                    self.scope["user"].id, self.conversation_id, msg_id, option_ids
                )
                if poll_info:
                    await self.channel_layer.group_send(
                        self.room_group_name,
                        {
                            "type": "poll_updated",
                            **poll_info,
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

    async def chat_permissions_broadcast(self, event):
        await self.send(
            text_data=json.dumps(
                {
                    "type": "chat_permissions_updated",
                    "chat_id": str(self.conversation_id),
                    "user_id": event.get("user_id"),
                    "permissions": event.get("permissions"),
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

    async def disappearing_timer_updated(self, event):
        await self.send(
            text_data=json.dumps(
                {
                    "type": "disappearing_timer_updated",
                    "chat_id": event["chat_id"],
                    "duration": event["duration"],
                    "updated_by": event["updated_by"],
                },
                cls=DjangoJSONEncoder,
            )
        )

    async def exi_pop_opened(self, event):
        await self.send(
            text_data=json.dumps(
                {
                    "type": "exi_pop_opened",
                    "message_id": event["message_id"],
                    "opened_by": event["opened_by"],
                    "opened_by_name": event.get("opened_by_name", ""),
                    "pop_type": event.get("pop_type", "confetti"),
                    "effect": event.get("effect", "burst"),
                    "sound": event.get("sound", "pop"),
                },
                cls=DjangoJSONEncoder,
            )
        )

    async def poll_updated(self, event):
        await self.send(
            text_data=json.dumps(
                {
                    "type": "poll_updated",
                    "message_id": event.get("message_id"),
                    "chat_id": event.get("chat_id"),
                    "poll": event.get("poll"),
                    "voted_by": event.get("voted_by"),
                    "voted_by_name": event.get("voted_by_name", ""),
                },
                cls=DjangoJSONEncoder,
            )
        )

    async def poll_closed(self, event):
        await self.send(
            text_data=json.dumps(
                {
                    "type": "poll_closed",
                    "message_id": event.get("message_id"),
                    "chat_id": event.get("chat_id"),
                    "closed_by": event.get("closed_by"),
                    "poll": event.get("poll"),
                },
                cls=DjangoJSONEncoder,
            )
        )

    async def capsule_unlocked(self, event):
        await self.send(
            text_data=json.dumps(
                {
                    "type": "capsule_unlocked",
                    "message_id": event.get("message_id"),
                    "chat_id": event.get("chat_id"),
                    "message": event.get("message"),
                    "capsule_type": event.get("capsule_type", "time_capsule"),
                    "title": event.get("title", ""),
                },
                cls=DjangoJSONEncoder,
            )
        )

    async def capsule_opened(self, event):
        await self.send(
            text_data=json.dumps(
                {
                    "type": "capsule_opened",
                    "message_id": event.get("message_id"),
                    "chat_id": event.get("chat_id"),
                    "opened_by": event.get("opened_by"),
                    "opened_by_name": event.get("opened_by_name", ""),
                    "capsule_type": event.get("capsule_type", "time_capsule"),
                    "reveal_style": event.get("reveal_style", "animated"),
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
    def _create_attachment_message(self, user_id, conversation_id, att_type, att_payload):
        import json
        from django.db.models import F
        from django.utils import timezone
        from .models import Conversation, ConversationParticipant, Message
        from .serializers import MessageSerializer

        att_type = (att_type or "").lower()
        if att_type in ("contacts", "contact"):
            msg = Message.objects.create(
                conversation_id=conversation_id,
                sender_id=user_id,
                content=json.dumps({
                    "attachment_type": "contact",
                    "name": att_payload.get("name", ""),
                    "phone": att_payload.get("phone", ""),
                    "exi_id": att_payload.get("exi_id", ""),
                    "email": att_payload.get("email", ""),
                    "organization": att_payload.get("organization", ""),
                    "notes": att_payload.get("notes", ""),
                }),
                message_type="contact",
                status="sent",
            )
        elif att_type in ("location", "geo"):
            msg = Message.objects.create(
                conversation_id=conversation_id,
                sender_id=user_id,
                content=json.dumps({
                    "attachment_type": "location",
                    "latitude": float(att_payload.get("latitude", 0)),
                    "longitude": float(att_payload.get("longitude", 0)),
                    "name": att_payload.get("name", ""),
                    "address": att_payload.get("address", ""),
                    "is_live": bool(att_payload.get("is_live", False)),
                    "live_duration_minutes": int(att_payload.get("live_duration_minutes", 0)),
                }),
                message_type="location",
                status="sent",
            )
        elif att_type in ("time_capsule", "capsule", "secret_letter", "letter"):
            from datetime import datetime, timedelta
            preset = att_payload.get("unlock_preset") or "custom"
            unlock_str = att_payload.get("unlock_at")
            now = timezone.now()

            if preset == "tomorrow":
                unlock_at = now + timedelta(days=1)
            elif preset == "one_week":
                unlock_at = now + timedelta(days=7)
            elif preset == "one_month":
                unlock_at = now + timedelta(days=30)
            elif unlock_str:
                try:
                    unlock_at = datetime.fromisoformat(unlock_str.replace("Z", "+00:00"))
                    if timezone.is_naive(unlock_at):
                        unlock_at = timezone.make_aware(unlock_at)
                except Exception:
                    unlock_at = now + timedelta(days=1)
            else:
                unlock_at = now + timedelta(days=1)

            capsule_type = "secret_letter" if att_type in ("secret_letter", "letter") else att_payload.get("capsule_type", "time_capsule")
            if att_payload.get("capsule_type") in ("secret_letter", "letter"):
                capsule_type = "secret_letter"

            passcode = (att_payload.get("passcode") or "").strip()
            secret_note = att_payload.get("content") or att_payload.get("note") or att_payload.get("secret_content") or ""

            msg = Message.objects.create(
                conversation_id=conversation_id,
                sender_id=user_id,
                content=json.dumps({
                    "attachment_type": "time_capsule",
                    "capsule_type": capsule_type,
                    "title": att_payload.get("title") or ("Secret Letter" if capsule_type == "secret_letter" else "Time Capsule"),
                    "occasion": att_payload.get("occasion", "secret"),
                    "secret_content": secret_note,
                    "note": secret_note,
                    "memories": att_payload.get("memories", []),
                    "unlock_preset": preset,
                    "reveal_style": att_payload.get("reveal_style", "animated"),
                    "letter_theme": att_payload.get("letter_theme", "classic"),
                    "passcode": passcode,
                    "has_passcode": bool(passcode),
                    "passcode_hint": att_payload.get("passcode_hint", ""),
                    "lock_hint": att_payload.get("lock_hint", ""),
                    "theme": att_payload.get("theme", "golden"),
                    "unlock_at": unlock_at.isoformat(),
                    "sealed_at": now.isoformat(),
                    "is_unlocked": False,
                    "is_opened": False,
                }),
                message_type="capsule",
                scheduled_for=unlock_at,
                is_scheduled=True,
                status="scheduled",
            )
        elif att_type in ("exi_pop", "pop"):
            msg = Message.objects.create(
                conversation_id=conversation_id,
                sender_id=user_id,
                content=json.dumps({
                    "attachment_type": "exi_pop",
                    "pop_type": att_payload.get("pop_type", "confetti"),
                    "text": att_payload.get("content", ""),
                    "effect": att_payload.get("effect", "burst"),
                    "sound": att_payload.get("sound", "pop"),
                    "auto_dismiss_seconds": int(att_payload.get("auto_dismiss_seconds", 5)),
                    "interactive_data": att_payload.get("interactive_data", {}),
                    "is_opened": False,
                    "created_at": timezone.now().isoformat(),
                }),
                message_type="exi_pop",
                status="sent",
            )
        elif att_type in ("poll", "polls"):
            import uuid
            options_list = []
            raw_options = att_payload.get("options", [])
            for idx, opt_text in enumerate(raw_options, start=1):
                options_list.append({
                    "id": f"opt-{idx}-{uuid.uuid4().hex[:6]}",
                    "text": str(opt_text),
                    "voter_ids": [],
                })
            expires_at = att_payload.get("expires_at")
            msg = Message.objects.create(
                conversation_id=conversation_id,
                sender_id=user_id,
                content=json.dumps({
                    "attachment_type": "poll",
                    "question": att_payload.get("question", "Poll"),
                    "options": options_list,
                    "allow_multiple_answers": bool(att_payload.get("allow_multiple_answers", False)),
                    "is_anonymous": bool(att_payload.get("is_anonymous", False)),
                    "is_closed": False,
                    "expires_at": expires_at,
                    "created_at": timezone.now().isoformat(),
                    "created_by": user_id,
                }),
                message_type="poll",
                status="sent",
            )
        elif att_type in ("business", "business_card"):
            biz_id = att_payload.get("business_id")
            from accounts.user_preferences import get_section_preferences, update_section_preferences
            from django.contrib.auth import get_user_model
            User = get_user_model()
            biz_user = User.objects.filter(id=biz_id).first() if biz_id else None
            biz_prefs = {}
            if biz_user:
                biz_prefs = get_section_preferences(biz_user.id, "business") or {}
                biz_prefs["recommendation_count"] = biz_prefs.get("recommendation_count", 0) + 1
                update_section_preferences(biz_user.id, "business", biz_prefs)

            share_ident = (biz_user.exi_id if biz_user else None) or att_payload.get("business_exi_id") or str(biz_user.id if biz_user else biz_id or "")
            note_str = att_payload.get("personal_message") or att_payload.get("note", "")

            msg = Message.objects.create(
                conversation_id=conversation_id,
                sender_id=user_id,
                content=json.dumps({
                    "attachment_type": "business",
                    "business_id": biz_id,
                    "business_exi_id": biz_user.exi_id if biz_user else att_payload.get("business_exi_id", ""),
                    "business_name": (biz_user.business_name if biz_user else None) or att_payload.get("business_name", "Business"),
                    "category": att_payload.get("category", "General"),
                    "address": (biz_user.business_address if biz_user else None) or att_payload.get("address", ""),
                    "website": (biz_user.business_website if biz_user else None) or att_payload.get("website", ""),
                    "note": note_str,
                    "personal_message": note_str,
                    "share_url": f"https://exi.chat/biz/{share_ident}" if share_ident else "",
                    "profile_url": f"/api/business/{share_ident}/public/" if share_ident else "",
                    "enquiry_url": f"/api/business/{share_ident}/enquire/" if share_ident else "",
                    "can_chat": True,
                    "can_enquire": True,
                    "recommended_by_id": user_id,
                    "rating": biz_prefs.get("rating", 4.9),
                    "is_verified": biz_prefs.get("verified", True),
                    "created_at": timezone.now().isoformat(),
                }),
                message_type="business",
                status="sent",
            )
        else:
            return None

        Conversation.objects.filter(id=conversation_id).update(updated_at=timezone.now())
        ConversationParticipant.objects.filter(conversation_id=conversation_id).exclude(
            user_id=user_id
        ).update(unread_count=F("unread_count") + 1, deleted_at=None)

        from .utils import notify_new_message
        notify_new_message(msg)
        return MessageSerializer(msg).data

    @sync_to_async
    def _mark_exi_pop_opened(self, user_id, conversation_id, message_id):
        import json
        from django.contrib.auth import get_user_model
        from .models import Message

        try:
            msg = Message.objects.get(id=message_id, conversation_id=conversation_id, message_type="exi_pop")
            try:
                data = json.loads(msg.content)
            except Exception:
                data = {}
            data["is_opened"] = True
            data["opened_by"] = user_id
            msg.content = json.dumps(data)
            msg.save(update_fields=["content"])

            User = get_user_model()
            u = User.objects.filter(id=user_id).first()
            name = getattr(u, "display_name", None) or getattr(u, "username", "User")
            return {
                "message_id": str(msg.id),
                "opened_by": user_id,
                "opened_by_name": name,
                "pop_type": data.get("pop_type", "confetti"),
                "effect": data.get("effect", "burst"),
                "sound": data.get("sound", "pop"),
            }
        except Exception:
            return None

    @sync_to_async
    def _record_poll_vote(self, user_id, conversation_id, message_id, option_ids):
        import json
        from django.contrib.auth import get_user_model
        from .models import Message
        from .serializers import MessageSerializer

        try:
            msg = Message.objects.get(id=message_id, conversation_id=conversation_id, message_type="poll")
            data = json.loads(msg.content)
            if data.get("is_closed"):
                return None
            options = data.get("options", [])
            allow_multi = data.get("allow_multiple_answers", False)
            selected_set = set(option_ids)
            for opt in options:
                voters = opt.setdefault("voter_ids", [])
                if opt["id"] in selected_set:
                    if user_id not in voters:
                        voters.append(user_id)
                else:
                    if not allow_multi and user_id in voters:
                        voters.remove(user_id)
            msg.content = json.dumps(data)
            msg.save(update_fields=["content"])

            User = get_user_model()
            u = User.objects.filter(id=user_id).first()
            name = getattr(u, "display_name", None) or getattr(u, "username", "User")
            msg_ser = MessageSerializer(msg)
            return {
                "message_id": str(msg.id),
                "chat_id": str(conversation_id),
                "poll": msg_ser.data.get("attachment_data") or {},
                "voted_by": user_id,
                "voted_by_name": name,
            }
        except Exception:
            return None

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

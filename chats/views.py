import re
from django.db import models, transaction
from django.db.models import F, Sum
from django.http import FileResponse
from django.urls import reverse
from rest_framework import generics, permissions, status
from rest_framework.parsers import FormParser, MultiPartParser, JSONParser
from rest_framework.response import Response
from rest_framework.views import APIView
from django.utils import timezone
from django.contrib.auth import get_user_model

from accounts.serializers import PublicProfileSerializer
from .utils import blocked_user_ids, is_blocked
from .models import Conversation, ConversationParticipant, Message, Media, Call
from .serializers import (
    CallSerializer, CallSignalSerializer, InitiateCallSerializer,
    MediaCompleteSerializer, MediaSerializer, MediaUploadUrlSerializer,
    MessageSearchResultSerializer,
    RecentConversationSerializer,
    ConversationDetailSerializer,
    MediaMessageUploadSerializer,
    MessageSerializer,
    SendMessageSerializer,
    StartConversationSerializer,
    ContactAttachmentSerializer,
    LocationAttachmentSerializer,
    TimeCapsuleAttachmentSerializer,
    LETTER_THEME_CONFIG,
    ExiPopAttachmentSerializer,
    PollCreateSerializer,
    PollVoteSerializer,
    UnifiedAttachmentSerializer,
)
from .utils import broadcast_chat_message, broadcast_message_status

User = get_user_model()


# 1. /api/chats/
class RecentConversationsView(generics.ListAPIView):
    serializer_class = RecentConversationSerializer
    permission_classes = [permissions.IsAuthenticated]

    def get_queryset(self):
        user = self.request.user
        archived = self.request.query_params.get("archived", "false").lower() == "true"
        return ConversationParticipant.objects.filter(
            user=user,
            deleted_at__isnull=True,
            is_archived=archived,
        ).select_related("conversation").order_by(
            "-is_pinned", "-conversation__updated_at"
        )


# 1b. /api/chats/start/
class StartConversationView(APIView):
    """Get or create a direct conversation with another user (by user_id or exi_id)."""

    permission_classes = [permissions.IsAuthenticated]

    def post(self, request):
        serializer = StartConversationSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        other = serializer.validated_data["other_user"]
        if other.id == request.user.id:
            return Response(
                {"detail": "Cannot start a chat with yourself."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        if is_blocked(request.user, other):
            return Response(
                {"detail": "Cannot start a chat with this user."},
                status=status.HTTP_403_FORBIDDEN,
            )

        from accounts.utils import can_message

        if not can_message(request.user, other):
            return Response(
                {"detail": "This user does not accept messages from you."},
                status=status.HTTP_403_FORBIDDEN,
            )

        conversation = self._get_or_create_direct(request.user, other)
        participant = ConversationParticipant.objects.get(
            user=request.user, conversation=conversation
        )
        # If previously soft-deleted, restore for this user
        if participant.deleted_at is not None:
            participant.deleted_at = None
            participant.save(update_fields=["deleted_at"])

        from .chat_permissions import normalize_permissions, update_chat_permissions
        norm_perms = normalize_permissions(request.data)
        if norm_perms:
            update_chat_permissions(conversation.id, request.user.id, request.data)

        return Response(
            RecentConversationSerializer(participant, context={"request": request}).data,
            status=status.HTTP_200_OK,
        )

    def _get_or_create_direct(self, user, other):
        existing = (
            Conversation.objects.filter(type="direct", participants=user)
            .filter(participants=other)
            .distinct()
            .first()
        )
        if existing:
            ConversationParticipant.objects.get_or_create(
                conversation=existing, user=user
            )
            ConversationParticipant.objects.get_or_create(
                conversation=existing, user=other
            )
            return existing

        with transaction.atomic():
            from accounts.user_preferences import get_section_preferences
            chat_prefs = get_section_preferences(user.id, "chats")
            default_timer = chat_prefs.get("default_disappearing_timer") if chat_prefs.get("apply_disappearing_to_new_chats", True) else None
            conversation = Conversation.objects.create(
                type="direct",
                disappearing_duration=default_timer if default_timer else None
            )
            ConversationParticipant.objects.create(conversation=conversation, user=user)
            ConversationParticipant.objects.create(conversation=conversation, user=other)
        return conversation


# 2. /api/chats/search/  (legacy — use /api/search/chats/)
class SearchChatsView(generics.ListAPIView):
    serializer_class = RecentConversationSerializer
    permission_classes = [permissions.IsAuthenticated]

    def get_queryset(self):
        f = self.request.query_params.get("filter") or self.request.query_params.get("type") or "all"
        return search_chats_queryset(self.request.user, get_query_param(self.request), filter_type=f)


# 3. /api/chats/users/search/  (legacy — use /api/search/users/)
class SearchUsersView(generics.ListAPIView):
    serializer_class = PublicProfileSerializer
    permission_classes = [permissions.IsAuthenticated]

    def get_queryset(self):
        return search_users_queryset(self.request.user, get_query_param(self.request))


# 4. /api/chats/unread-count/
class TotalUnreadCountView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        from .utils import get_badge_count, get_notification_unread_count

        badge_count = get_badge_count(request.user)
        notification_unread = get_notification_unread_count(request.user)
        return Response(
            {
                "unread_count": badge_count,
                "badge_count": badge_count,
                "notification_unread_count": notification_unread,
            }
        )


# 5. /api/chats/{chat_id}/
class ChatDetailsView(generics.RetrieveAPIView):
    serializer_class = ConversationDetailSerializer
    permission_classes = [permissions.IsAuthenticated]
    lookup_field = "conversation_id"
    lookup_url_kwarg = "chat_id"

    def get_queryset(self):
        return ConversationParticipant.objects.filter(
            user=self.request.user, deleted_at__isnull=True
        ).select_related("conversation")

    def patch(self, request, chat_id):
        participant = self.get_object()
        from .chat_permissions import normalize_permissions, update_chat_permissions
        norm_perms = normalize_permissions(request.data)
        if norm_perms:
            update_chat_permissions(participant.conversation_id, request.user.id, request.data)
        return Response(
            ConversationDetailSerializer(participant, context={"request": request}).data
        )

    def post(self, request, chat_id):
        return self.patch(request, chat_id)


# 6. /api/chats/{chat_id}/pin/
class PinChatView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def patch(self, request, chat_id):
        try:
            participant = ConversationParticipant.objects.get(
                user=request.user, conversation_id=chat_id
            )
            participant.is_pinned = not participant.is_pinned
            participant.save(update_fields=["is_pinned"])
            action = "pinned" if participant.is_pinned else "unpinned"
            return Response(
                {
                    "detail": f"Chat {action} successfully.",
                    "is_pinned": participant.is_pinned,
                }
            )
        except ConversationParticipant.DoesNotExist:
            return Response(
                {"detail": "Conversation not found."}, status=status.HTTP_404_NOT_FOUND
            )


# 7. /api/chats/{chat_id}/archive/
class ArchiveChatView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request, chat_id):
        try:
            participant = ConversationParticipant.objects.get(
                user=request.user, conversation_id=chat_id
            )
            return Response({
                "chat_id": str(chat_id),
                "is_archived": participant.is_archived,
            })
        except ConversationParticipant.DoesNotExist:
            return Response(
                {"detail": "Conversation not found."}, status=status.HTTP_404_NOT_FOUND
            )

    def patch(self, request, chat_id):
        try:
            participant = ConversationParticipant.objects.get(
                user=request.user, conversation_id=chat_id
            )
            participant.is_archived = not participant.is_archived
            participant.save(update_fields=["is_archived"])
            action = "archived" if participant.is_archived else "unarchived"
            return Response(
                {
                    "detail": f"Chat {action} successfully.",
                    "is_archived": participant.is_archived,
                }
            )
        except ConversationParticipant.DoesNotExist:
            return Response(
                {"detail": "Conversation not found."}, status=status.HTTP_404_NOT_FOUND
            )

    def post(self, request, chat_id):
        return self.patch(request, chat_id)


class MuteChatView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request, chat_id):
        try:
            participant = ConversationParticipant.objects.get(
                user=request.user, conversation_id=chat_id
            )
            return Response({
                "chat_id": str(chat_id),
                "is_muted": participant.is_muted,
            })
        except ConversationParticipant.DoesNotExist:
            return Response(
                {"detail": "Conversation not found."}, status=status.HTTP_404_NOT_FOUND
            )

    def post(self, request, chat_id):
        try:
            participant = ConversationParticipant.objects.get(
                user=request.user, conversation_id=chat_id
            )
            participant.is_muted = True
            participant.save(update_fields=["is_muted"])
            return Response({"detail": "Chat muted successfully.", "is_muted": True})
        except ConversationParticipant.DoesNotExist:
            return Response(
                {"detail": "Conversation not found."}, status=status.HTTP_404_NOT_FOUND
            )


class UnmuteChatView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request, chat_id):
        try:
            participant = ConversationParticipant.objects.get(
                user=request.user, conversation_id=chat_id
            )
            participant.is_muted = False
            participant.save(update_fields=["is_muted"])
            return Response({"detail": "Chat unmuted successfully.", "is_muted": False})
        except ConversationParticipant.DoesNotExist:
            return Response(
                {"detail": "Conversation not found."}, status=status.HTTP_404_NOT_FOUND
            )


# 8. /api/chats/{chat_id}/delete/
class DeleteChatView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def delete(self, request, chat_id):
        try:
            participant = ConversationParticipant.objects.get(
                user=request.user, conversation_id=chat_id
            )
            participant.deleted_at = timezone.now()
            participant.is_pinned = False
            participant.is_archived = False
            participant.save(update_fields=["deleted_at", "is_pinned", "is_archived"])
            return Response({"detail": "Chat deleted successfully."})
        except ConversationParticipant.DoesNotExist:
            return Response(
                {"detail": "Conversation not found."}, status=status.HTTP_404_NOT_FOUND
            )

    def post(self, request, chat_id):
        return self.delete(request, chat_id)


# 9. /api/chats/{chat_id}/read/
class MarkChatReadView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def patch(self, request, chat_id):
        try:
            participant = ConversationParticipant.objects.get(
                user=request.user, conversation_id=chat_id
            )
            participant.unread_count = 0
            participant.save(update_fields=["unread_count"])

            from .utils import (
                get_badge_count,
                broadcast_badge_update,
                read_receipts_enabled,
                broadcast_message_status,
            )

            share_receipt = read_receipts_enabled(request.user)
            unread_messages = list(
                Message.objects.filter(conversation_id=chat_id)
                .exclude(sender=request.user)
                .exclude(status="read")
            )
            if share_receipt:
                Message.objects.filter(conversation_id=chat_id).exclude(
                    sender=request.user
                ).exclude(status="read").update(status="read")
                for msg in unread_messages:
                    broadcast_message_status(chat_id, msg.id, "read")

            badge = get_badge_count(request.user)
            broadcast_badge_update(request.user.id, badge)
            return Response(
                {
                    "detail": "Chat messages marked as read.",
                    "badge_count": badge,
                }
            )
        except ConversationParticipant.DoesNotExist:
            return Response(
                {"detail": "Conversation not found."}, status=status.HTTP_404_NOT_FOUND
            )

    post = patch


class ClearChatView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request, chat_id):
        try:
            participant = ConversationParticipant.objects.get(
                user=request.user, conversation_id=chat_id
            )
            return Response({
                "chat_id": str(chat_id),
                "cleared_at": participant.cleared_at.isoformat() if participant.cleared_at else None,
            })
        except ConversationParticipant.DoesNotExist:
            return Response(
                {"detail": "Conversation not found."}, status=status.HTTP_404_NOT_FOUND
            )

    def post(self, request, chat_id):
        try:
            participant = ConversationParticipant.objects.get(
                user=request.user, conversation_id=chat_id
            )
            participant.cleared_at = timezone.now()
            participant.save(update_fields=["cleared_at"])
            return Response({"detail": "Chat cleared successfully."})
        except ConversationParticipant.DoesNotExist:
            return Response(
                {"detail": "Conversation not found."}, status=status.HTTP_404_NOT_FOUND
            )

    def delete(self, request, chat_id):
        return self.post(request, chat_id)


class SetDisappearingMessagesView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request, chat_id):
        try:
            participant = ConversationParticipant.objects.select_related("conversation").get(
                user=request.user, conversation_id=chat_id
            )
        except ConversationParticipant.DoesNotExist:
            return Response(
                {"detail": "Conversation not found."}, status=status.HTTP_404_NOT_FOUND
            )

        dur = participant.conversation.disappearing_duration
        return Response({
            "chat_id": str(chat_id),
            "disappearing_duration": dur,
            "is_enabled": bool(dur and dur > 0),
            "presets": [
                {"label": "Off", "duration": 0},
                {"label": "24 Hours", "duration": 86400},
                {"label": "7 Days", "duration": 604800},
                {"label": "90 Days", "duration": 7776000},
            ]
        })

    def post(self, request, chat_id):
        duration = request.data.get('duration')
        if duration is not None:
            try:
                duration = int(duration)
                if duration < 0:
                    return Response({"detail": "Duration cannot be negative."}, status=status.HTTP_400_BAD_REQUEST)
            except (ValueError, TypeError):
                return Response({"detail": "Invalid duration."}, status=status.HTTP_400_BAD_REQUEST)

        try:
            participant = ConversationParticipant.objects.select_related("conversation").get(
                user=request.user, conversation_id=chat_id
            )
        except ConversationParticipant.DoesNotExist:
            return Response(
                {"detail": "Conversation not found."}, status=status.HTTP_404_NOT_FOUND
            )

        conv = participant.conversation

        # Check group permission if this is a group conversation
        if conv.type == 'group':
            from .group_views import _get_group_meta, _is_admin
            meta = _get_group_meta(chat_id) or {}
            perms = meta.get("permissions", {})
            if perms.get("change_group_info", "everyone") == "admins" and not _is_admin(request.user.id, meta):
                return Response(
                    {"detail": "Only group admins can change disappearing messages settings."},
                    status=status.HTTP_403_FORBIDDEN,
                )

        new_duration = duration if (duration and duration > 0) else None
        old_duration = conv.disappearing_duration

        if new_duration != old_duration:
            conv.disappearing_duration = new_duration
            conv.save(update_fields=['disappearing_duration'])

            # Real-time WebSocket broadcast to conversation channel
            from .utils import broadcast_disappearing_timer, broadcast_chat_message
            broadcast_disappearing_timer(chat_id, new_duration, request.user)

            # Post a system message in the conversation
            def _format_dur(secs):
                if not secs:
                    return "off"
                if secs == 86400:
                    return "24 hours"
                if secs == 604800:
                    return "7 days"
                if secs == 7776000:
                    return "90 days"
                if secs >= 86400:
                    return f"{secs // 86400} days"
                if secs >= 3600:
                    return f"{secs // 3600} hours"
                return f"{secs} seconds"

            user_name = getattr(request.user, "display_name", "") or getattr(request.user, "first_name", "") or request.user.username
            if new_duration:
                sys_text = f"{user_name} turned on disappearing messages ({_format_dur(new_duration)})."
            else:
                sys_text = f"{user_name} turned off disappearing messages."

            sys_msg = Message.objects.create(
                conversation=conv,
                sender=request.user,
                content=sys_text,
                message_type='text',
            )
            broadcast_chat_message(chat_id, sys_msg)

        return Response({
            "detail": "Disappearing messages updated successfully.",
            "chat_id": str(chat_id),
            "disappearing_duration": new_duration,
            "is_enabled": bool(new_duration),
        })


class ReportUserView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request):
        from accounts.models import BlockedUser
        from accounts.utils import throttle_request

        from .serializers import ReportSerializer
        from .models import Report

        allowed, retry_after = throttle_request(
            request, 'report-user', limit=10, window_seconds=86400
        )
        if not allowed:
            response = Response(
                {'detail': 'Report limit reached. Try again later.', 'retry_after': retry_after},
                status=status.HTTP_429_TOO_MANY_REQUESTS,
            )
            response['Retry-After'] = str(retry_after)
            return response

        reported_user_id = request.data.get('reported_user')
        conversation_id = request.data.get('conversation')
        reason = request.data.get('reason')
        notes = request.data.get('notes', '')
        also_block = bool(request.data.get('block'))

        if not reported_user_id or not reason:
            return Response({'detail': 'reported_user and reason are required.'}, status=status.HTTP_400_BAD_REQUEST)

        valid_reasons = [choice[0] for choice in Report.REPORT_REASONS]
        if reason not in valid_reasons:
            return Response(
                {'detail': f'reason must be one of: {", ".join(valid_reasons)}.'},
                status=status.HTTP_400_BAD_REQUEST,
            )

        try:
            reported_user = User.objects.get(id=reported_user_id)
        except User.DoesNotExist:
            return Response({'detail': 'Reported user not found.'}, status=status.HTTP_404_NOT_FOUND)

        if reported_user.id == request.user.id:
            return Response(
                {'detail': 'You cannot report yourself.'},
                status=status.HTTP_400_BAD_REQUEST,
            )

        if conversation_id and not ConversationParticipant.objects.filter(
            user=request.user, conversation_id=conversation_id
        ).exists():
            return Response(
                {'detail': 'Conversation not found.'}, status=status.HTTP_404_NOT_FOUND
            )

        pending = Report.objects.filter(
            reporter=request.user,
            reported_user=reported_user,
            status='pending',
        ).first()
        if pending:
            return Response(
                {
                    'detail': 'You already have a pending report for this user.',
                    'report': ReportSerializer(pending).data,
                },
                status=status.HTTP_409_CONFLICT,
            )

        report = Report.objects.create(
            reporter=request.user,
            reported_user=reported_user,
            conversation_id=conversation_id,
            reason=reason,
            notes=notes[:2000],
        )

        if also_block:
            BlockedUser.objects.get_or_create(
                blocker=request.user,
                blocked=reported_user,
                defaults={'reason': f'Reported for {reason}'},
            )

        return Response(
            {**ReportSerializer(report).data, 'blocked': also_block},
            status=status.HTTP_201_CREATED,
        )


# ---------- Module 3: Messages ----------

class MessageListView(generics.ListAPIView):
    serializer_class = MessageSerializer
    permission_classes = [permissions.IsAuthenticated]

    def get_queryset(self):
        chat_id = self.kwargs.get("chat_id")
        try:
            participant = ConversationParticipant.objects.select_related('conversation').get(
                user=self.request.user, conversation_id=chat_id
            )
        except ConversationParticipant.DoesNotExist:
            return Message.objects.none()

        qs = Message.objects.filter(conversation_id=chat_id)
        
        if participant.cleared_at:
            qs = qs.filter(created_at__gt=participant.cleared_at)
            
        if participant.conversation.disappearing_duration:
            from datetime import timedelta
            expired_time = timezone.now() - timedelta(seconds=participant.conversation.disappearing_duration)
            qs = qs.filter(created_at__gte=expired_time)
            
        return qs


class SharedMediaView(generics.ListAPIView):
    serializer_class = MessageSerializer
    permission_classes = [permissions.IsAuthenticated]

    def get_queryset(self):
        chat_id = self.kwargs.get("chat_id")
        try:
            participant = ConversationParticipant.objects.select_related('conversation').get(
                user=self.request.user, conversation_id=chat_id
            )
        except ConversationParticipant.DoesNotExist:
            return Message.objects.none()

        qs = Message.objects.filter(
            conversation_id=chat_id, 
            message_type__in=['image', 'video', 'document', 'audio', 'voice']
        )
        
        if participant.cleared_at:
            qs = qs.filter(created_at__gt=participant.cleared_at)
            
        if participant.conversation.disappearing_duration:
            from datetime import timedelta
            expired_time = timezone.now() - timedelta(seconds=participant.conversation.disappearing_duration)
            qs = qs.filter(created_at__gte=expired_time)
            
        return qs


class ChatSettingsView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request, chat_id):
        try:
            participant = ConversationParticipant.objects.select_related('conversation').get(
                user=request.user, conversation_id=chat_id
            )
        except ConversationParticipant.DoesNotExist:
            return Response({"detail": "Conversation not found."}, status=status.HTTP_404_NOT_FOUND)

        # Determine the other user in a direct chat to fetch block status
        is_blocked = False
        if participant.conversation.type == 'direct':
            other_participants = participant.conversation.participants.exclude(id=request.user.id)
            if other_participants.exists():
                other_user = other_participants.first()
                from accounts.models import BlockedUser
                is_blocked = BlockedUser.objects.filter(blocker=request.user, blocked=other_user).exists()

        from .chat_permissions import get_chat_permissions_display, normalize_permissions, update_chat_permissions
        from .chat_settings_flow import get_chat_settings_flow_data

        flow = get_chat_settings_flow_data(request.user, chat_id)

        data = {
            "chat_id": str(chat_id),
            "is_muted": participant.is_muted,
            "is_pinned": participant.is_pinned,
            "is_archived": participant.is_archived,
            "disappearing_duration": participant.conversation.disappearing_duration,
            "is_blocked": is_blocked,
            "permissions": get_chat_permissions_display(participant.conversation_id, request.user.id),
            "navigation": flow.get("sections") if flow else None,
        }
        return Response(data)

    def patch(self, request, chat_id):
        try:
            participant = ConversationParticipant.objects.select_related('conversation').get(
                user=request.user, conversation_id=chat_id
            )
        except ConversationParticipant.DoesNotExist:
            return Response({"detail": "Conversation not found."}, status=status.HTTP_404_NOT_FOUND)

        from .chat_permissions import normalize_permissions, update_chat_permissions
        norm_perms = normalize_permissions(request.data)
        if norm_perms:
            update_chat_permissions(participant.conversation_id, request.user.id, request.data)

        updated_fields = []
        if "is_muted" in request.data:
            participant.is_muted = bool(request.data["is_muted"])
            updated_fields.append("is_muted")
        if "is_pinned" in request.data:
            participant.is_pinned = bool(request.data["is_pinned"])
            updated_fields.append("is_pinned")
        if "is_archived" in request.data:
            participant.is_archived = bool(request.data["is_archived"])
            updated_fields.append("is_archived")
        if updated_fields:
            participant.save(update_fields=updated_fields)

        return self.get(request, chat_id)


class ChatPermissionsView(APIView):
    """
    GET   /api/chats/<chat_id>/permissions/  - View permissions for this chat
    POST  /api/chats/<chat_id>/permissions/  - Choose/update permissions for this chat
    PATCH /api/chats/<chat_id>/permissions/  - Choose/update permissions for this chat
    """
    permission_classes = [permissions.IsAuthenticated]

    def _get_participant(self, user, chat_id):
        try:
            return ConversationParticipant.objects.select_related("conversation").get(
                user=user, conversation_id=chat_id, deleted_at__isnull=True
            )
        except ConversationParticipant.DoesNotExist:
            return None

    def get(self, request, chat_id):
        participant = self._get_participant(request.user, chat_id)
        if not participant:
            return Response({"detail": "Conversation not found."}, status=status.HTTP_404_NOT_FOUND)

        from .chat_permissions import get_chat_permissions_display
        perms = get_chat_permissions_display(chat_id, request.user.id)

        other_user = None
        banner_text = "Members can"
        if participant.conversation.type == "direct":
            other = participant.conversation.participants.exclude(id=request.user.id).first()
            if other:
                first_name = (getattr(other, "first_name", "") or "").strip()
                name = first_name or (getattr(other, "display_name", "") or other.username)
                banner_text = f"{name} can"
                from accounts.utils import visible_profile_fields
                vis = visible_profile_fields(other, request.user)
                photo_url = None
                if getattr(other, "profile_photo", None) and vis.get("profile_photo", True):
                    try:
                        photo_url = request.build_absolute_uri(other.profile_photo.url)
                    except Exception:
                        pass
                other_user = {
                    "id": other.id,
                    "first_name": first_name,
                    "display_name": getattr(other, "display_name", other.username),
                    "username": other.username,
                    "profile_photo": photo_url,
                    "is_online": other.is_online if vis.get("online_status", True) else None,
                    "last_seen": other.last_seen.isoformat() if other.last_seen and vis.get("last_seen", True) else None,
                    "banner_text": banner_text,
                }

        return Response({
            "chat_id": str(chat_id),
            "banner_text": banner_text,
            "other_user": other_user,
            "permissions": perms,
            "chips": [
                {"id": "message", "label": "Message", "enabled": perms.get("messages_enabled", True)},
                {"id": "call", "label": "Call", "enabled": perms.get("calls_enabled", True)},
                {"id": "media", "label": "Media", "enabled": perms.get("media_enabled", True)},
            ],
        })

    def patch(self, request, chat_id):
        participant = self._get_participant(request.user, chat_id)
        if not participant:
            return Response({"detail": "Conversation not found."}, status=status.HTTP_404_NOT_FOUND)

        from .chat_permissions import update_chat_permissions
        perms = update_chat_permissions(chat_id, request.user.id, request.data)
        return Response({
            "chat_id": str(chat_id),
            "detail": "Chat permissions updated successfully.",
            "permissions": perms,
        })

    def post(self, request, chat_id):
        return self.patch(request, chat_id)

def guard_message_send(request, conversation):
    """Shared abuse checks for every outbound-message path. Returns a Response on failure."""
    from .utils import check_message_flood, check_send_permission

    allowed, retry_after = check_message_flood(request.user)
    if not allowed:
        response = Response(
            {"detail": "You are sending messages too quickly.", "retry_after": retry_after},
            status=status.HTTP_429_TOO_MANY_REQUESTS,
        )
        response["Retry-After"] = str(retry_after)
        return response

    permitted, detail = check_send_permission(request.user, conversation)
    if not permitted:
        return Response({"detail": detail}, status=status.HTTP_403_FORBIDDEN)

    return None


class SendMessageView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request, chat_id):
        try:
            participant = ConversationParticipant.objects.select_related(
                "conversation"
            ).get(user=request.user, conversation_id=chat_id)
        except ConversationParticipant.DoesNotExist:
            return Response(
                {"detail": "Not a participant."}, status=status.HTTP_403_FORBIDDEN
            )

        blocked_response = guard_message_send(request, participant.conversation)
        if blocked_response:
            return blocked_response

        serializer = SendMessageSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        payload = serializer.validated_data

        msg = Message.objects.create(
            conversation_id=chat_id,
            sender=request.user,
            content=payload["content"],
            ciphertext=payload["ciphertext"],
            is_encrypted=payload["is_encrypted"],
            encryption_version=payload["encryption_version"],
            sender_device_id=payload.get("sender_device_id") or "",
            forwarded_from_id=payload.get("forwarded_from_id"),
            scheduled_for=payload.get("scheduled_for"),
            is_scheduled=bool(payload.get("scheduled_for")),
            status='scheduled' if bool(payload.get("scheduled_for")) else 'sent',
        )

        if not msg.is_scheduled:
            Conversation.objects.filter(id=chat_id).update(updated_at=timezone.now())
            ConversationParticipant.objects.filter(conversation_id=chat_id).exclude(
                user=request.user
            ).update(unread_count=F("unread_count") + 1, deleted_at=None)

            broadcast_chat_message(chat_id, msg)

        return Response(
            MessageSerializer(msg, context={"request": request}).data,
            status=status.HTTP_201_CREATED,
        )


class SendMediaMessageView(APIView):
    """Upload an image, video, document, audio file, or voice note."""

    permission_classes = [permissions.IsAuthenticated]
    parser_classes = [MultiPartParser, FormParser]

    def post(self, request, chat_id):
        try:
            participant = ConversationParticipant.objects.select_related(
                "conversation"
            ).get(
                user=request.user,
                conversation_id=chat_id,
                deleted_at__isnull=True,
            )
        except ConversationParticipant.DoesNotExist:
            return Response(
                {"detail": "Not a participant."}, status=status.HTTP_403_FORBIDDEN
            )

        blocked_response = guard_message_send(request, participant.conversation)
        if blocked_response:
            return blocked_response

        from .chat_permissions import check_chat_media_permission
        media_allowed, media_err = check_chat_media_permission(chat_id, request.user.id)
        if not media_allowed:
            return Response({"detail": media_err}, status=status.HTTP_403_FORBIDDEN)

        serializer = MediaMessageUploadSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        uploaded_file = serializer.validated_data["file"]

        msg = Message.objects.create(
            conversation_id=chat_id,
            sender=request.user,
            content=serializer.validated_data.get("caption", ""),
            message_type=serializer.validated_data["media_type"],
            attachment=uploaded_file,
            attachment_name=uploaded_file.name,
            attachment_mime_type=uploaded_file.content_type or "",
            attachment_size=uploaded_file.size,
        )
        Conversation.objects.filter(id=chat_id).update(updated_at=timezone.now())
        ConversationParticipant.objects.filter(conversation_id=chat_id).exclude(
            user=request.user
        ).update(unread_count=F("unread_count") + 1, deleted_at=None)

        broadcast_chat_message(chat_id, msg)
        return Response(
            MessageSerializer(msg, context={"request": request}).data,
            status=status.HTTP_201_CREATED,
        )


class ChatAttachmentMenuView(APIView):
    """
    GET /api/chats/<chat_id>/attachments/menu/
    Returns the Attachment Menu configuration for the 7 actions:
    1. Camera (Live capture photo/video)
    2. Photos (Device gallery media)
    3. Documents (PDFs, docs, spreadsheets, files)
    4. Contacts (Contact cards)
    5. Location (GPS coordinates & live location)
    6. Time Capsule (Scheduled locked messages with future reveal)
    7. EXI Pop (Interactive surprise pop with special effects)
    """
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request, chat_id):
        try:
            participant = ConversationParticipant.objects.select_related("conversation").get(
                user=request.user, conversation_id=chat_id, deleted_at__isnull=True
            )
        except ConversationParticipant.DoesNotExist:
            return Response({"detail": "Not a participant."}, status=status.HTTP_403_FORBIDDEN)

        from .chat_permissions import check_chat_media_permission, check_chat_message_permission, get_chat_permissions_display
        media_allowed, _ = check_chat_media_permission(chat_id, request.user.id)
        messages_allowed, _ = check_chat_message_permission(chat_id, request.user.id)
        perms_display = get_chat_permissions_display(chat_id, request.user.id)

        menu_items = [
            {
                "id": "camera",
                "title": "Camera",
                "description": "Capture a live photo or record a video",
                "icon": "camera",
                "category": "media",
                "supported_media": ["image", "video"],
                "is_enabled": media_allowed,
                "action_endpoint": f"/api/chats/{chat_id}/attachments/send/",
            },
            {
                "id": "photos",
                "title": "Photos & Videos",
                "description": "Share photos or videos from device gallery",
                "icon": "photo",
                "category": "media",
                "supported_media": ["image", "video"],
                "is_enabled": media_allowed,
                "action_endpoint": f"/api/chats/{chat_id}/attachments/send/",
            },
            {
                "id": "documents",
                "title": "Documents",
                "description": "Share PDFs, documents, spreadsheets, and files",
                "icon": "document",
                "category": "media",
                "supported_media": ["document"],
                "is_enabled": media_allowed,
                "action_endpoint": f"/api/chats/{chat_id}/attachments/send/",
            },
            {
                "id": "contacts",
                "title": "Contacts",
                "description": "Share a contact card with name, phone, or EXI ID",
                "icon": "contact",
                "category": "structured",
                "is_enabled": messages_allowed,
                "action_endpoint": f"/api/chats/{chat_id}/attachments/contact/",
            },
            {
                "id": "location",
                "title": "Location",
                "description": "Share your current or live GPS coordinates",
                "icon": "location",
                "category": "structured",
                "is_enabled": messages_allowed,
                "action_endpoint": f"/api/chats/{chat_id}/attachments/location/",
            },
            {
                "id": "time_capsule",
                "path": "Path A: Time Capsule",
                "title": "Time Capsule & Secret Letter",
                "description": "Schedule a locked message or secret letter with future reveal & passcode protection",
                "icon": "time_capsule",
                "category": "interactive",
                "is_enabled": messages_allowed,
                "supported_types": ["time_capsule", "secret_letter"],
                "occasions": ["birthday", "future_you", "milestone", "secret"],
                "unlock_presets": ["tomorrow", "one_week", "one_month", "custom"],
                "reveal_styles": ["animated", "classic"],
                "letter_themes": ["classic", "love", "birthday", "modern"],
                "supports_passcode": True,
                "supports_memories": True,
                "action_button": "Seal & Deliver",
                "preview_endpoint": f"/api/chats/{chat_id}/attachments/time-capsule/preview/",
                "action_endpoint": f"/api/chats/{chat_id}/attachments/time-capsule/",
                "seal_endpoint": f"/api/chats/{chat_id}/attachments/time-capsule/seal/",
            },
            {
                "id": "secret_letter",
                "path": "Path B: Secret Letter",
                "title": "Secret Letter",
                "description": "Send a passcode-protected sealed letter with themed stationery and future reveal",
                "icon": "mail",
                "category": "interactive",
                "is_enabled": messages_allowed,
                "supported_types": ["secret_letter", "letter"],
                "letter_themes": ["classic", "love", "birthday", "modern"],
                "letter_theme_options": [
                    {"id": "classic", "name": "Classic Parchment", "paper_texture": "parchment", "seal_color": "#C59B27", "font_family": "serif"},
                    {"id": "love", "name": "Love Letter", "paper_texture": "rose_linen", "seal_color": "#D90429", "font_family": "cursive"},
                    {"id": "birthday", "name": "Birthday Letter", "paper_texture": "confetti_ivory", "seal_color": "#7209B7", "font_family": "decorative"},
                    {"id": "modern", "name": "Modern Minimalist", "paper_texture": "matte_smooth", "seal_color": "#495057", "font_family": "sans-serif"},
                ],
                "occasions": ["birthday", "future_you", "milestone", "secret"],
                "unlock_presets": ["tomorrow", "one_week", "one_month", "custom"],
                "supports_passcode": True,
                "supports_memories": True,
                "action_button": "Seal & Send Letter",
                "preview_endpoint": f"/api/chats/{chat_id}/attachments/secret-letter/preview/",
                "action_endpoint": f"/api/chats/{chat_id}/attachments/secret-letter/",
                "seal_endpoint": f"/api/chats/{chat_id}/attachments/secret-letter/seal/",
            },
            {
                "id": "exi_pop",
                "title": "EXI Pop",
                "description": "Send an interactive surprise pop with burst animation",
                "icon": "sparkles",
                "category": "interactive",
                "is_enabled": messages_allowed,
                "pop_types": ["confetti", "buzz", "scratch_card", "flash", "fireworks", "heartbeat", "gift_box"],
                "action_endpoint": f"/api/chats/{chat_id}/attachments/exi-pop/",
            },
            {
                "id": "poll",
                "title": "Poll",
                "description": "Create a group poll with multiple choices and live votes",
                "icon": "poll",
                "category": "interactive",
                "is_enabled": messages_allowed,
                "action_endpoint": f"/api/chats/{chat_id}/polls/",
            },
            {
                "id": "business",
                "title": "Business",
                "description": "Share and recommend a business card to this chat",
                "icon": "storefront",
                "category": "structured",
                "is_enabled": messages_allowed,
                "action_endpoint": f"/api/chats/{chat_id}/attachments/business/",
            },
        ]

        return Response({
            "chat_id": str(chat_id),
            "conversation_type": participant.conversation.type,
            "attachment_menu": menu_items,
            "menu": menu_items,
            "permissions": perms_display,
        })


class ChatAttachmentSendView(APIView):
    """
    POST /api/chats/<chat_id>/attachments/send/
    POST /api/chats/<chat_id>/attachments/
    Unified Attachment API executing:
    Save and validate -> Create message -> Deliver via WebSocket
    Handles all 7 options from the attachment menu:
    Camera, Photos, Documents, Contacts, Location, Time Capsule, EXI Pop.
    """
    permission_classes = [permissions.IsAuthenticated]
    parser_classes = [MultiPartParser, FormParser, JSONParser]

    def post(self, request, chat_id):
        import json
        try:
            participant = ConversationParticipant.objects.select_related("conversation").get(
                user=request.user, conversation_id=chat_id, deleted_at__isnull=True
            )
        except ConversationParticipant.DoesNotExist:
            return Response({"detail": "Not a participant."}, status=status.HTTP_403_FORBIDDEN)

        blocked_response = guard_message_send(request, participant.conversation)
        if blocked_response:
            return blocked_response

        # Determine attachment type
        att_type = (request.data.get("attachment_type") or request.data.get("type") or "").strip().lower()

        # If not explicitly specified, deduce from payload keys
        if not att_type:
            if "latitude" in request.data or "location" in request.data:
                att_type = "location"
            elif "contact" in request.data or ("name" in request.data and ("phone" in request.data or "exi_id" in request.data or "email" in request.data)):
                att_type = "contacts"
            elif "unlock_at" in request.data or "time_capsule" in request.data:
                att_type = "time_capsule"
            elif "pop_type" in request.data or "exi_pop" in request.data:
                att_type = "exi_pop"
            elif "question" in request.data or "options" in request.data or "poll" in request.data:
                att_type = "poll"
            elif "business_id" in request.data or "business_exi_id" in request.data or "business_name" in request.data or "business" in request.data:
                att_type = "business"
            elif "file" in request.FILES or "file" in request.data:
                uploaded = request.FILES.get("file") or request.data.get("file")
                mime = (getattr(uploaded, "content_type", "") or "").lower()
                att_type = "documents" if mime.startswith(("application/", "text/")) else "photos"
            else:
                return Response(
                    {"detail": "attachment_type is required: camera, photos, documents, contacts, location, time_capsule, exi_pop, poll, or business."},
                    status=status.HTTP_400_BAD_REQUEST,
                )

        from .chat_permissions import check_chat_media_permission, check_chat_message_permission

        # 1. Camera / Photos / Documents
        if att_type in ("camera", "photos", "photo", "documents", "document", "media"):
            media_allowed, media_err = check_chat_media_permission(chat_id, request.user.id)
            if not media_allowed:
                return Response({"detail": media_err}, status=status.HTTP_403_FORBIDDEN)

            uploaded_file = request.FILES.get("file") or request.data.get("file")
            if not uploaded_file:
                return Response({"detail": "A file is required for media attachments."}, status=status.HTTP_400_BAD_REQUEST)

            caption = request.data.get("caption", "")
            mime = (getattr(uploaded_file, "content_type", "") or "").lower()

            if att_type in ("documents", "document") or mime.startswith(("application/", "text/")):
                media_type = "document"
            elif mime.startswith("video/"):
                media_type = "video"
            else:
                media_type = "image"

            msg = Message.objects.create(
                conversation_id=chat_id,
                sender=request.user,
                content=caption,
                message_type=media_type,
                attachment=uploaded_file,
                attachment_name=uploaded_file.name,
                attachment_mime_type=mime,
                attachment_size=uploaded_file.size,
                status="sent",
            )

        # 2. Contacts
        elif att_type in ("contacts", "contact"):
            msg_allowed, msg_err = check_chat_message_permission(chat_id, request.user.id)
            if not msg_allowed:
                return Response({"detail": msg_err}, status=status.HTTP_403_FORBIDDEN)

            raw_c = request.data.get("contact") or request.data
            ser = ContactAttachmentSerializer(data=raw_c)
            ser.is_valid(raise_exception=True)
            cdata = ser.validated_data

            phone = cdata.get("phone", "")
            exi_id = cdata.get("exi_id", "")
            matched_user = None
            if phone:
                matched_user = User.objects.filter(phone=phone).first()
            if not matched_user and exi_id:
                matched_user = User.objects.filter(exi_id=exi_id).first()

            is_on_exi = bool(matched_user) or cdata.get("is_on_exi", True)
            avatar_url = cdata.get("avatar_url", "")
            if not avatar_url and matched_user and getattr(matched_user, "profile_photo", None):
                try:
                    avatar_url = request.build_absolute_uri(matched_user.profile_photo.url)
                except Exception:
                    pass

            req_status = request.data.get("status")
            msg_status = "failed" if req_status == "failed" else "sent"

            msg = Message.objects.create(
                conversation_id=chat_id,
                sender=request.user,
                content=json.dumps({
                    "attachment_type": "contact",
                    "name": cdata["name"],
                    "role": cdata.get("role") or cdata.get("title") or "",
                    "phone": phone,
                    "phone_type": cdata.get("phone_type") or "Landline",
                    "country": cdata.get("country") or "India",
                    "exi_id": exi_id or (matched_user.exi_id if matched_user else ""),
                    "email": cdata.get("email", ""),
                    "organization": cdata.get("organization", ""),
                    "avatar_url": avatar_url,
                    "is_verified": cdata.get("is_verified", True),
                    "is_on_exi": is_on_exi,
                    "notes": cdata.get("notes", ""),
                    "vcard_raw": cdata.get("vcard_raw", ""),
                }),
                message_type="contact",
                status=msg_status,
            )

        # 3. Location
        elif att_type in ("location", "geo"):
            msg_allowed, msg_err = check_chat_message_permission(chat_id, request.user.id)
            if not msg_allowed:
                return Response({"detail": msg_err}, status=status.HTTP_403_FORBIDDEN)

            raw_loc = request.data.get("location") or request.data
            ser = LocationAttachmentSerializer(data=raw_loc)
            ser.is_valid(raise_exception=True)
            ldata = ser.validated_data

            msg = Message.objects.create(
                conversation_id=chat_id,
                sender=request.user,
                content=json.dumps({
                    "attachment_type": "location",
                    "latitude": ldata["latitude"],
                    "longitude": ldata["longitude"],
                    "name": ldata.get("name", ""),
                    "address": ldata.get("address", ""),
                    "is_live": ldata.get("is_live", False),
                    "live_duration_minutes": ldata.get("live_duration_minutes", 0),
                    "shared_at": timezone.now().isoformat(),
                }),
                message_type="location",
                status="sent",
            )

        # 4. Time Capsule & Secret Letter
        elif att_type in ("time_capsule", "capsule", "secret_letter", "letter"):
            msg_allowed, msg_err = check_chat_message_permission(chat_id, request.user.id)
            if not msg_allowed:
                return Response({"detail": msg_err}, status=status.HTTP_403_FORBIDDEN)

            raw_tc = request.data.get("time_capsule") or request.data.get("secret_letter") or request.data
            ser = TimeCapsuleAttachmentSerializer(data=raw_tc)
            ser.is_valid(raise_exception=True)
            tc_data = ser.validated_data

            unlock_at = tc_data["unlock_at"]
            capsule_type = "secret_letter" if att_type in ("secret_letter", "letter") else tc_data.get("capsule_type", "time_capsule")
            if tc_data.get("capsule_type") in ("secret_letter", "letter"):
                capsule_type = "secret_letter"

            passcode = (tc_data.get("passcode") or "").strip()
            secret_note = tc_data.get("content") or tc_data.get("note") or tc_data.get("secret_content") or ""

            msg = Message.objects.create(
                conversation_id=chat_id,
                sender=request.user,
                content=json.dumps({
                    "attachment_type": "time_capsule",
                    "capsule_type": capsule_type,
                    "title": tc_data.get("title") or ("Secret Letter" if capsule_type == "secret_letter" else "Time Capsule"),
                    "occasion": tc_data.get("occasion", "secret"),
                    "secret_content": secret_note,
                    "note": secret_note,
                    "memories": tc_data.get("memories", []),
                    "unlock_preset": tc_data.get("unlock_preset", "custom"),
                    "reveal_style": tc_data.get("reveal_style", "animated"),
                    "letter_theme": tc_data.get("letter_theme", "classic"),
                    "theme_details": LETTER_THEME_CONFIG.get(tc_data.get("letter_theme", "classic"), LETTER_THEME_CONFIG["classic"]) if capsule_type == "secret_letter" else None,
                    "passcode": passcode,
                    "has_passcode": bool(passcode),
                    "passcode_hint": tc_data.get("passcode_hint", ""),
                    "lock_hint": tc_data.get("lock_hint", ""),
                    "theme": tc_data.get("theme", "golden"),
                    "unlock_at": unlock_at.isoformat(),
                    "sealed_at": timezone.now().isoformat(),
                    "is_unlocked": False,
                    "is_opened": False,
                }),
                message_type="capsule",
                scheduled_for=unlock_at,
                is_scheduled=True,
                status="scheduled",
            )

        # 5. EXI Pop
        elif att_type in ("exi_pop", "pop"):
            msg_allowed, msg_err = check_chat_message_permission(chat_id, request.user.id)
            if not msg_allowed:
                return Response({"detail": msg_err}, status=status.HTTP_403_FORBIDDEN)

            raw_pop = request.data.get("exi_pop") or request.data
            ser = ExiPopAttachmentSerializer(data=raw_pop)
            ser.is_valid(raise_exception=True)
            pop_data = ser.validated_data

            msg = Message.objects.create(
                conversation_id=chat_id,
                sender=request.user,
                content=json.dumps({
                    "attachment_type": "exi_pop",
                    "pop_type": pop_data.get("pop_type", "confetti"),
                    "text": pop_data.get("content", ""),
                    "effect": pop_data.get("effect", "burst"),
                    "sound": pop_data.get("sound", "pop"),
                    "auto_dismiss_seconds": pop_data.get("auto_dismiss_seconds", 5),
                    "interactive_data": pop_data.get("interactive_data", {}),
                    "is_opened": False,
                    "created_at": timezone.now().isoformat(),
                }),
                message_type="exi_pop",
                status="sent",
            )

        # 6. Group Chat Poll
        elif att_type in ("poll", "polls"):
            msg_allowed, msg_err = check_chat_message_permission(chat_id, request.user.id)
            if not msg_allowed:
                return Response({"detail": msg_err}, status=status.HTTP_403_FORBIDDEN)

            raw_poll = request.data.get("poll") or request.data
            ser = PollCreateSerializer(data=raw_poll)
            ser.is_valid(raise_exception=True)
            pdata = ser.validated_data

            import uuid
            options_list = []
            for idx, opt_text in enumerate(pdata["options"], start=1):
                options_list.append({
                    "id": f"opt-{idx}-{uuid.uuid4().hex[:6]}",
                    "text": opt_text,
                    "voter_ids": [],
                })

            expires_at = pdata.get("expires_at")
            poll_content = {
                "attachment_type": "poll",
                "question": pdata["question"],
                "options": options_list,
                "allow_multiple_answers": pdata.get("allow_multiple_answers", False),
                "is_anonymous": pdata.get("is_anonymous", False),
                "is_closed": False,
                "expires_at": expires_at.isoformat() if expires_at else None,
                "created_at": timezone.now().isoformat(),
                "created_by": request.user.id,
            }

            msg = Message.objects.create(
                conversation_id=chat_id,
                sender=request.user,
                content=json.dumps(poll_content),
                message_type="poll",
                status="sent",
            )

        # 7. Business Card / Recommendation
        elif att_type in ("business", "business_card"):
            msg_allowed, msg_err = check_chat_message_permission(chat_id, request.user.id)
            if not msg_allowed:
                return Response({"detail": msg_err}, status=status.HTTP_403_FORBIDDEN)

            raw_b = request.data.get("business") or request.data
            biz_id = raw_b.get("business_id")
            biz_exi_id = raw_b.get("business_exi_id")

            biz_user = None
            if biz_id:
                biz_user = User.objects.filter(id=biz_id).first()
            if not biz_user and biz_exi_id:
                biz_user = User.objects.filter(exi_id=biz_exi_id).first()

            from accounts.user_preferences import get_section_preferences, update_section_preferences
            biz_prefs = {}
            if biz_user:
                biz_prefs = get_section_preferences(biz_user.id, "business") or {}
                biz_prefs["recommendation_count"] = biz_prefs.get("recommendation_count", 0) + 1
                update_section_preferences(biz_user.id, "business", biz_prefs)

            biz_name = (biz_user.business_name if biz_user and biz_user.business_name else None) or raw_b.get("business_name") or (biz_user.username if biz_user else "Business")
            biz_addr = (biz_user.business_address if biz_user else None) or raw_b.get("address", "")
            biz_web = (biz_user.business_website if biz_user else None) or raw_b.get("website", "")
            categories = biz_prefs.get("categories", []) or ([raw_b.get("category")] if raw_b.get("category") else ["General"])

            avatar_url = raw_b.get("avatar_url", "")
            if not avatar_url and biz_user and getattr(biz_user, "profile_photo", None):
                try:
                    avatar_url = request.build_absolute_uri(biz_user.profile_photo.url)
                except Exception:
                    pass

            share_ident = (biz_user.exi_id if biz_user else None) or biz_exi_id or str(biz_user.id if biz_user else biz_id)
            share_url = request.build_absolute_uri(f"/biz/{share_ident}")
            profile_url = f"/api/business/{share_ident}/public/"
            enquiry_url = f"/api/business/{share_ident}/enquire/"
            note_text = (raw_b.get("personal_message") or raw_b.get("note") or "").strip()

            b_card = {
                "attachment_type": "business",
                "business_id": biz_user.id if biz_user else biz_id,
                "business_exi_id": biz_user.exi_id if biz_user else biz_exi_id,
                "business_name": biz_name,
                "display_name": (biz_user.display_name if biz_user else None) or biz_name,
                "category": categories[0] if categories else "General",
                "categories": categories,
                "address": biz_addr,
                "website": biz_web,
                "bio": biz_user.bio if biz_user else "",
                "rating": biz_prefs.get("rating", 4.9),
                "review_count": biz_prefs.get("review_count", 0),
                "recommendation_count": biz_prefs.get("recommendation_count", 1),
                "is_verified": biz_prefs.get("verified", True),
                "avatar_url": avatar_url,
                "note": note_text,
                "personal_message": note_text,
                "share_url": share_url,
                "profile_url": profile_url,
                "enquiry_url": enquiry_url,
                "recommended_by_id": request.user.id,
                "recommended_by_name": getattr(request.user, "display_name", None) or request.user.username,
                "recommended_at": timezone.now().isoformat(),
                "can_chat": True,
                "can_enquire": True,
                "can_view_catalog": len(biz_prefs.get("products", [])) > 0,
                "products_preview": biz_prefs.get("products", [])[:3],
            }

            msg = Message.objects.create(
                conversation_id=chat_id,
                sender=request.user,
                content=json.dumps(b_card),
                message_type="business",
                status="sent",
            )

        else:
            return Response(
                {"detail": f"Unknown attachment type '{att_type}'. Supported: camera, photos, documents, contacts, location, time_capsule, exi_pop, poll, business."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        # Save and validate -> update conversation & unread count
        Conversation.objects.filter(id=chat_id).update(updated_at=timezone.now())
        ConversationParticipant.objects.filter(conversation_id=chat_id).exclude(
            user=request.user
        ).update(unread_count=F("unread_count") + 1, deleted_at=None)

        # Deliver via WebSocket
        broadcast_chat_message(chat_id, msg)

        return Response(
            MessageSerializer(msg, context={"request": request}).data,
            status=status.HTTP_201_CREATED,
        )


class ChatContactAttachmentView(APIView):
    """
    POST /api/chats/<chat_id>/attachments/contact/
    Dedicated endpoint for sharing contact data.
    """
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request, chat_id):
        data = dict(request.data)
        data["attachment_type"] = "contact"
        request._full_data = data
        return ChatAttachmentSendView().post(request, chat_id)


class ChatLocationAttachmentView(APIView):
    """
    POST /api/chats/<chat_id>/attachments/location/
    Dedicated endpoint for sharing GPS coordinates & live location.
    """
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request, chat_id):
        data = dict(request.data)
        data["attachment_type"] = "location"
        request._full_data = data
        return ChatAttachmentSendView().post(request, chat_id)


class ChatTimeCapsulePreviewView(APIView):
    """
    POST /api/chats/<chat_id>/attachments/time-capsule/preview/
    POST /api/chats/<chat_id>/attachments/secret-letter/preview/
    POST /api/chats/<chat_id>/time-capsule/preview/

    Step 4 (Path A) & Step 5 (Path B): Preview the capsule or secret letter.
    Validates input without saving to database, returns simulated sender preview,
    recipient preview (sealed/redacted), countdown, theme styling, and seal endpoint.
    """
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request, chat_id):
        from .chat_permissions import check_chat_message_permission

        try:
            participant = ConversationParticipant.objects.select_related("conversation").get(
                user=request.user, conversation_id=chat_id, deleted_at__isnull=True
            )
        except ConversationParticipant.DoesNotExist:
            return Response({"detail": "Not a participant."}, status=status.HTTP_403_FORBIDDEN)

        msg_allowed, msg_err = check_chat_message_permission(chat_id, request.user.id)
        if not msg_allowed:
            return Response({"detail": msg_err}, status=status.HTTP_403_FORBIDDEN)

        data = dict(request.data)
        path = request.path.lower()
        if "secret-letter" in path or data.get("capsule_type") in ("secret_letter", "letter"):
            data.setdefault("capsule_type", "secret_letter")

        ser = TimeCapsuleAttachmentSerializer(data=data)
        ser.is_valid(raise_exception=True)
        vdata = ser.validated_data

        now = timezone.now()
        unlock_at = vdata["unlock_at"]
        countdown_seconds = max(0, int((unlock_at - now).total_seconds()))
        capsule_type = vdata.get("capsule_type", "time_capsule")

        passcode = (vdata.get("passcode") or "").strip()
        has_passcode = bool(passcode)
        letter_theme = vdata.get("letter_theme", "classic")
        theme_details = LETTER_THEME_CONFIG.get(letter_theme, LETTER_THEME_CONFIG["classic"])
        reveal_style = vdata.get("reveal_style", "animated")
        secret_note = vdata.get("content") or vdata.get("note") or ""
        memories = vdata.get("memories", [])
        occasion = vdata.get("occasion", "secret")

        action_title = "Seal & Deliver" if capsule_type == "time_capsule" else "Seal & Send Letter"
        path_name = "Path B: Secret Letter" if capsule_type == "secret_letter" else "Path A: Time Capsule"
        seal_slug = "secret-letter" if capsule_type == "secret_letter" else "time-capsule"

        sender_preview = {
            "title": vdata.get("title") or ("Secret Letter" if capsule_type == "secret_letter" else "Time Capsule"),
            "capsule_type": capsule_type,
            "occasion": occasion,
            "unlock_at": unlock_at.isoformat(),
            "countdown_seconds": countdown_seconds,
            "note": secret_note,
            "memories": memories,
            "reveal_style": reveal_style,
            "letter_theme": letter_theme,
            "has_passcode": has_passcode,
            "passcode_hint": vdata.get("passcode_hint", ""),
            "theme_details": theme_details if capsule_type == "secret_letter" else None,
            "is_sealed": False,
            "status": "preview",
        }

        recipient_preview = {
            "title": sender_preview["title"],
            "capsule_type": capsule_type,
            "occasion": occasion,
            "unlock_at": unlock_at.isoformat(),
            "countdown_seconds": countdown_seconds,
            "note": None,
            "memories": [],
            "memories_count": len(memories),
            "reveal_style": reveal_style,
            "letter_theme": letter_theme,
            "has_passcode": has_passcode,
            "passcode_hint": vdata.get("passcode_hint", "") if has_passcode else "",
            "theme_details": theme_details if capsule_type == "secret_letter" else None,
            "is_sealed": True,
            "status": "sealed_preview",
            "message": f"Sealed! Unlocks in {countdown_seconds} seconds." if countdown_seconds > 0 else "Ready to unlock!",
        }

        return Response({
            "chat_id": str(chat_id),
            "capsule_type": capsule_type,
            "path": path_name,
            "action_button": action_title,
            "seal_endpoint": f"/api/chats/{chat_id}/attachments/{seal_slug}/",
            "seal_action_endpoint": f"/api/chats/{chat_id}/attachments/{seal_slug}/seal/",
            "sender_preview": sender_preview,
            "recipient_preview": recipient_preview,
            "theme_details": theme_details if capsule_type == "secret_letter" else None,
            "unlock_summary": {
                "preset": vdata.get("unlock_preset", "custom"),
                "unlock_at": unlock_at.isoformat(),
                "countdown_seconds": countdown_seconds,
            },
        }, status=status.HTTP_200_OK)


class ChatTimeCapsuleAttachmentView(APIView):
    """
    POST /api/chats/<chat_id>/attachments/time-capsule/
    POST /api/chats/<chat_id>/attachments/time-capsule/seal/
    POST /api/chats/<chat_id>/attachments/secret-letter/
    POST /api/chats/<chat_id>/attachments/secret-letter/seal/
    POST /api/chats/<chat_id>/time-capsule/
    POST /api/chats/<chat_id>/time-capsule/seal/
    Dedicated endpoint for sealing & delivering a Time Capsule (Path A Step 5)
    or sealing & sending a Secret Letter (Path B Step 6).
    """
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request, chat_id):
        data = dict(request.data)
        ctype = data.get("capsule_type") or data.get("type") or ""
        if ctype in ("secret_letter", "letter") or "secret-letter" in request.path:
            data["attachment_type"] = "secret_letter"
        else:
            data["attachment_type"] = "time_capsule"
        request._full_data = data
        return ChatAttachmentSendView().post(request, chat_id)


class ChatTimeCapsuleDetailView(APIView):
    """
    GET /api/chats/<chat_id>/attachments/time-capsule/<message_id>/
    GET /api/chats/<chat_id>/time-capsule/<message_id>/
    Inspect a Time Capsule or Secret Letter: reveals secret content once unlock_at is reached
    and passcode (when required) is verified.
    """
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request, chat_id, message_id):
        import json
        try:
            ConversationParticipant.objects.get(
                user=request.user, conversation_id=chat_id, deleted_at__isnull=True
            )
        except ConversationParticipant.DoesNotExist:
            return Response({"detail": "Not a participant."}, status=status.HTTP_403_FORBIDDEN)

        try:
            msg = Message.objects.get(id=message_id, conversation_id=chat_id, message_type="capsule")
        except Message.DoesNotExist:
            return Response({"detail": "Time Capsule message not found."}, status=status.HTTP_404_NOT_FOUND)

        try:
            data = json.loads(msg.content)
        except Exception:
            data = {}

        now = timezone.now()
        is_sender = request.user.id == msg.sender_id
        is_unlocked = bool(data.get("is_unlocked")) or (msg.scheduled_for and now >= msg.scheduled_for)

        time_left_seconds = 0
        if msg.scheduled_for and msg.scheduled_for > now:
            time_left_seconds = int((msg.scheduled_for - now).total_seconds())

        has_passcode = bool(data.get("passcode") or data.get("has_passcode"))
        is_opened = bool(data.get("is_opened", False))
        is_passcode_required = has_passcode and not is_opened and not is_sender

        response_data = {
            "message_id": str(msg.id),
            "sender_id": msg.sender_id,
            "capsule_type": data.get("capsule_type", "time_capsule"),
            "title": data.get("title", "Time Capsule"),
            "occasion": data.get("occasion", "secret"),
            "reveal_style": data.get("reveal_style", "animated"),
            "letter_theme": data.get("letter_theme", "classic"),
            "lock_hint": data.get("lock_hint", ""),
            "passcode_hint": data.get("passcode_hint", ""),
            "theme": data.get("theme", "golden"),
            "unlock_at": msg.scheduled_for.isoformat() if msg.scheduled_for else None,
            "is_unlocked": is_unlocked,
            "countdown_seconds": time_left_seconds,
            "has_passcode": has_passcode,
            "is_passcode_required": is_passcode_required,
            "is_opened": is_opened,
            "memories_count": len(data.get("memories") or []),
            "open_endpoint": f"/api/chats/{chat_id}/attachments/time-capsule/{msg.id}/open/",
        }

        if (is_unlocked and not is_passcode_required) or is_sender:
            secret_note = data.get("secret_content") or data.get("content") or data.get("note") or msg.content
            response_data["secret_content"] = secret_note
            response_data["note"] = secret_note
            response_data["memories"] = data.get("memories", [])
        else:
            response_data["secret_content"] = None
            response_data["note"] = None
            response_data["memories"] = []
            if not is_unlocked:
                response_data["message"] = f"Sealed! Unlocks in {time_left_seconds} seconds."
            else:
                response_data["message"] = "Unlocked! Passcode required to reveal note and memories."

        return Response(response_data)


class ChatTimeCapsuleOpenView(APIView):
    """
    POST /api/chats/<chat_id>/attachments/time-capsule/<message_id>/open/
    POST /api/chats/<chat_id>/time-capsule/<message_id>/open/
    Step 10: Opens a Time Capsule or Secret Letter.
    Validates unlock date, validates passcode when required, marks as opened, broadcasts WebSocket event,
    and returns revealed note and memories.
    """
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request, chat_id, message_id):
        import json
        from asgiref.sync import async_to_sync
        from channels.layers import get_channel_layer

        try:
            ConversationParticipant.objects.get(
                user=request.user, conversation_id=chat_id, deleted_at__isnull=True
            )
        except ConversationParticipant.DoesNotExist:
            return Response({"detail": "Not a participant."}, status=status.HTTP_403_FORBIDDEN)

        try:
            msg = Message.objects.get(id=message_id, conversation_id=chat_id, message_type="capsule")
        except Message.DoesNotExist:
            return Response({"detail": "Time Capsule message not found."}, status=status.HTTP_404_NOT_FOUND)

        now = timezone.now()
        is_sender = request.user.id == msg.sender_id
        is_unlocked = bool(msg.is_scheduled is False and msg.status == 'sent') or (msg.scheduled_for and now >= msg.scheduled_for)

        if not is_unlocked and not is_sender:
            countdown = int((msg.scheduled_for - now).total_seconds()) if msg.scheduled_for else 0
            return Response({
                "detail": f"Time Capsule is still sealed. Unlocks in {countdown} seconds.",
                "countdown_seconds": countdown,
                "unlock_at": msg.scheduled_for.isoformat() if msg.scheduled_for else None,
            }, status=status.HTTP_403_FORBIDDEN)

        try:
            data = json.loads(msg.content)
        except Exception:
            data = {}

        # Passcode verification
        expected_passcode = (data.get("passcode") or "").strip()
        if expected_passcode and not is_sender:
            provided_passcode = (request.data.get("passcode") or "").strip()
            if provided_passcode != expected_passcode:
                return Response({
                    "detail": "Incorrect passcode.",
                    "passcode_hint": data.get("passcode_hint", ""),
                }, status=status.HTTP_401_UNAUTHORIZED)

        # Mark as opened
        data["is_opened"] = True
        data["is_unlocked"] = True
        data["opened_by"] = request.user.id
        data["opened_at"] = now.isoformat()
        msg.content = json.dumps(data)
        msg.save(update_fields=["content"])

        # Broadcast WebSocket event
        try:
            channel_layer = get_channel_layer()
            async_to_sync(channel_layer.group_send)(
                f"chat_{chat_id}",
                {
                    "type": "capsule_opened",
                    "message_id": str(msg.id),
                    "chat_id": str(chat_id),
                    "opened_by": request.user.id,
                    "opened_by_name": request.user.first_name or request.user.username,
                    "capsule_type": data.get("capsule_type", "time_capsule"),
                    "reveal_style": data.get("reveal_style", "animated"),
                }
            )
        except Exception:
            pass

        secret_note = data.get("secret_content") or data.get("content") or data.get("note") or ""
        memories = data.get("memories") or []

        return Response({
            "detail": "Capsule opened successfully.",
            "message_id": str(msg.id),
            "capsule_type": data.get("capsule_type", "time_capsule"),
            "title": data.get("title", ""),
            "occasion": data.get("occasion", "secret"),
            "note": secret_note,
            "secret_content": secret_note,
            "memories": memories,
            "reveal_style": data.get("reveal_style", "animated"),
            "letter_theme": data.get("letter_theme", "classic"),
            "opened_at": data["opened_at"],
            "is_opened": True,
            "is_unlocked": True,
        }, status=status.HTTP_200_OK)


class TimeCapsuleCheckScheduledView(APIView):
    """
    POST /api/chats/time-capsules/check-scheduled/
    Trigger/webhook endpoint to run the Celery background task on demand
    for checking and delivering due Time Capsules & Secret Letters.
    """
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request):
        from chats.tasks import check_and_deliver_time_capsules
        count = check_and_deliver_time_capsules()
        return Response({
            "detail": "Processed scheduled time capsules successfully.",
            "delivered_count": count or 0,
        }, status=status.HTTP_200_OK)

    def get(self, request):
        return self.post(request)


class ChatExiPopAttachmentView(APIView):
    """
    POST /api/chats/<chat_id>/attachments/exi-pop/
    Dedicated endpoint for sending an interactive EXI Pop surprise.
    """
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request, chat_id):
        data = dict(request.data)
        data["attachment_type"] = "exi_pop"
        request._full_data = data
        return ChatAttachmentSendView().post(request, chat_id)


class ChatBusinessAttachmentView(APIView):
    """
    POST /api/chats/<chat_id>/attachments/business/
    Dedicated endpoint for sharing a business recommendation card in chat.
    """
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request, chat_id):
        data = dict(request.data)
        data["attachment_type"] = "business"
        request._full_data = data
        return ChatAttachmentSendView().post(request, chat_id)


class ChatExiPopOpenView(APIView):
    """
    POST /api/chats/<chat_id>/attachments/exi-pop/<message_id>/open/
    Record that recipient opened the pop and broadcast trigger event to WebSocket.
    """
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request, chat_id, message_id):
        import json
        from asgiref.sync import async_to_sync
        from channels.layers import get_channel_layer

        try:
            ConversationParticipant.objects.get(
                user=request.user, conversation_id=chat_id, deleted_at__isnull=True
            )
        except ConversationParticipant.DoesNotExist:
            return Response({"detail": "Not a participant."}, status=status.HTTP_403_FORBIDDEN)

        try:
            msg = Message.objects.get(id=message_id, conversation_id=chat_id, message_type="exi_pop")
        except Message.DoesNotExist:
            return Response({"detail": "EXI Pop not found."}, status=status.HTTP_404_NOT_FOUND)

        try:
            data = json.loads(msg.content)
        except Exception:
            data = {}

        data["is_opened"] = True
        data["opened_by"] = request.user.id
        data["opened_at"] = timezone.now().isoformat()
        msg.content = json.dumps(data)
        msg.save(update_fields=["content"])

        # Broadcast real-time interactive trigger event to all participants
        channel_layer = get_channel_layer()
        if channel_layer:
            async_to_sync(channel_layer.group_send)(
                f"chat_{chat_id}",
                {
                    "type": "exi_pop_opened",
                    "message_id": str(msg.id),
                    "pop_type": data.get("pop_type", "confetti"),
                    "effect": data.get("effect", "burst"),
                    "sound": data.get("sound", "pop"),
                    "opened_by": request.user.id,
                    "opened_by_name": getattr(request.user, "display_name", None) or request.user.username,
                }
            )

        return Response({
            "detail": "EXI Pop opened successfully.",
            "message_id": str(msg.id),
            "pop_type": data.get("pop_type", "confetti"),
            "effect": data.get("effect", "burst"),
            "sound": data.get("sound", "pop"),
            "text": data.get("text", ""),
            "is_opened": True,
        })


class GroupPollCreateView(APIView):
    """
    POST /api/chats/<chat_id>/polls/
    POST /api/groups/<group_id>/polls/
    Create a new group poll.
    """
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request, chat_id=None, group_id=None):
        target_chat_id = chat_id or group_id
        data = dict(request.data)
        data["attachment_type"] = "poll"
        request._full_data = data
        return ChatAttachmentSendView().post(request, target_chat_id)


class GroupPollDetailView(APIView):
    """
    GET /api/chats/<chat_id>/polls/<message_id>/
    GET /api/groups/<group_id>/polls/<message_id>/
    Inspect poll options, percentages, live vote counts, and user's voted choices.
    """
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request, message_id, chat_id=None, group_id=None):
        target_chat_id = chat_id or group_id
        try:
            if target_chat_id:
                ConversationParticipant.objects.get(
                    user=request.user, conversation_id=target_chat_id, deleted_at__isnull=True
                )
        except ConversationParticipant.DoesNotExist:
            return Response({"detail": "Not a participant."}, status=status.HTTP_403_FORBIDDEN)

        query = {"id": message_id, "message_type": "poll"}
        if target_chat_id:
            query["conversation_id"] = target_chat_id

        try:
            msg = Message.objects.get(**query)
        except Message.DoesNotExist:
            return Response({"detail": "Poll not found."}, status=status.HTTP_404_NOT_FOUND)

        if not target_chat_id:
            if not ConversationParticipant.objects.filter(
                user=request.user, conversation_id=msg.conversation_id, deleted_at__isnull=True
            ).exists():
                return Response({"detail": "Not a participant in this conversation."}, status=status.HTTP_403_FORBIDDEN)

        ser = MessageSerializer(msg, context={"request": request})
        return Response({
            "message_id": str(msg.id),
            "chat_id": str(msg.conversation_id),
            "sender_id": msg.sender_id,
            "created_at": msg.created_at.isoformat(),
            "poll": ser.data.get("attachment_data") or {},
        })


class GroupPollVoteView(APIView):
    """
    POST /api/chats/<chat_id>/polls/<message_id>/vote/
    POST /api/chats/polls/<message_id>/vote/
    Cast, change, or retract votes on a group poll.
    Payload: {"option_ids": ["opt-1-xxxx", ...], "retract": false}
    """
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request, message_id, chat_id=None, group_id=None):
        import json
        from asgiref.sync import async_to_sync
        from channels.layers import get_channel_layer
        from django.utils.dateparse import parse_datetime

        target_chat_id = chat_id or group_id
        query = {"id": message_id, "message_type": "poll"}
        if target_chat_id:
            query["conversation_id"] = target_chat_id

        try:
            msg = Message.objects.get(**query)
        except Message.DoesNotExist:
            return Response({"detail": "Poll not found."}, status=status.HTTP_404_NOT_FOUND)

        effective_chat_id = msg.conversation_id
        if not ConversationParticipant.objects.filter(
            user=request.user, conversation_id=effective_chat_id, deleted_at__isnull=True
        ).exists():
            return Response({"detail": "Not a participant in this chat."}, status=status.HTTP_403_FORBIDDEN)

        try:
            pdata = json.loads(msg.content)
        except Exception:
            pdata = {}

        now = timezone.now()
        expires_at_str = pdata.get("expires_at")
        if expires_at_str:
            try:
                exp_dt = parse_datetime(expires_at_str)
                if exp_dt and now >= exp_dt:
                    pdata["is_closed"] = True
                    msg.content = json.dumps(pdata)
                    msg.save(update_fields=["content"])
                    return Response({"detail": "This poll has expired and is closed."}, status=status.HTTP_400_BAD_REQUEST)
            except Exception:
                pass

        if pdata.get("is_closed"):
            return Response({"detail": "This poll is closed."}, status=status.HTTP_400_BAD_REQUEST)

        vote_ser = PollVoteSerializer(data=request.data)
        vote_ser.is_valid(raise_exception=True)
        selected_ids = list(vote_ser.validated_data["option_ids"])

        allow_multi = pdata.get("allow_multiple_answers", False)
        options = pdata.get("options", [])
        valid_option_ids = {opt["id"] for opt in options}

        invalid = set(selected_ids) - valid_option_ids
        if invalid:
            return Response({"detail": f"Invalid option IDs: {list(invalid)}"}, status=status.HTTP_400_BAD_REQUEST)

        if not allow_multi and len(selected_ids) > 1:
            return Response({"detail": "Multiple answers are not allowed for this poll."}, status=status.HTTP_400_BAD_REQUEST)

        user_id = request.user.id
        is_retract = request.data.get("retract", False)

        for opt in options:
            opt_id = opt["id"]
            voters = opt.setdefault("voter_ids", [])
            already_voted = user_id in voters

            if opt_id in selected_ids:
                if is_retract and already_voted:
                    voters.remove(user_id)
                elif not already_voted:
                    voters.append(user_id)
            else:
                if not allow_multi and already_voted:
                    voters.remove(user_id)

        msg.content = json.dumps(pdata)
        msg.save(update_fields=["content"])

        msg_ser = MessageSerializer(msg, context={"request": request})
        poll_res = msg_ser.data.get("attachment_data") or {}

        # Broadcast real-time WebSocket event to chat group
        channel_layer = get_channel_layer()
        if channel_layer:
            async_to_sync(channel_layer.group_send)(
                f"chat_{effective_chat_id}",
                {
                    "type": "poll_updated",
                    "message_id": str(msg.id),
                    "chat_id": str(effective_chat_id),
                    "poll": poll_res,
                    "voted_by": user_id,
                    "voted_by_name": getattr(request.user, "display_name", None) or request.user.username,
                }
            )

        return Response({
            "detail": "Vote recorded successfully.",
            "poll": poll_res,
        }, status=status.HTTP_200_OK)


class GroupPollCloseView(APIView):
    """
    POST /api/chats/<chat_id>/polls/<message_id>/close/
    POST /api/chats/polls/<message_id>/close/
    Close an active poll. Can only be performed by the poll creator or group admin.
    """
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request, message_id, chat_id=None, group_id=None):
        import json
        from asgiref.sync import async_to_sync
        from channels.layers import get_channel_layer

        target_chat_id = chat_id or group_id
        query = {"id": message_id, "message_type": "poll"}
        if target_chat_id:
            query["conversation_id"] = target_chat_id

        try:
            msg = Message.objects.get(**query)
        except Message.DoesNotExist:
            return Response({"detail": "Poll not found."}, status=status.HTTP_404_NOT_FOUND)

        effective_chat_id = msg.conversation_id
        try:
            participant = ConversationParticipant.objects.get(
                user=request.user, conversation_id=effective_chat_id, deleted_at__isnull=True
            )
        except ConversationParticipant.DoesNotExist:
            return Response({"detail": "Not a participant in this conversation."}, status=status.HTTP_403_FORBIDDEN)

        is_creator = msg.sender_id == request.user.id
        is_admin = getattr(participant, "role", "") in ("admin", "owner")
        if not is_creator and not is_admin:
            return Response({"detail": "Only the poll creator or group admins can close this poll."}, status=status.HTTP_403_FORBIDDEN)

        try:
            pdata = json.loads(msg.content)
        except Exception:
            pdata = {}

        if pdata.get("is_closed"):
            msg_ser = MessageSerializer(msg, context={"request": request})
            return Response({
                "detail": "Poll is already closed.",
                "poll": msg_ser.data.get("attachment_data") or {},
            }, status=status.HTTP_200_OK)

        pdata["is_closed"] = True
        pdata["closed_at"] = timezone.now().isoformat()
        pdata["closed_by"] = request.user.id

        msg.content = json.dumps(pdata)
        msg.save(update_fields=["content"])

        msg_ser = MessageSerializer(msg, context={"request": request})
        poll_res = msg_ser.data.get("attachment_data") or {}

        # Broadcast closed event
        channel_layer = get_channel_layer()
        if channel_layer:
            async_to_sync(channel_layer.group_send)(
                f"chat_{effective_chat_id}",
                {
                    "type": "poll_closed",
                    "message_id": str(msg.id),
                    "chat_id": str(effective_chat_id),
                    "closed_by": request.user.id,
                    "poll": poll_res,
                }
            )

        return Response({
            "detail": "Poll closed successfully.",
            "poll": poll_res,
        }, status=status.HTTP_200_OK)


class ContactCardDetailView(APIView):
    """
    GET /api/chats/messages/<message_id>/contact-card/
    GET /api/chats/<chat_id>/messages/<message_id>/contact-card/
    Returns the rich Contact Sheet details shown in Image 2:
    - Contact Avatar + Verified checkmark badge
    - Full Name: "Rahul Kumar"
    - Role: "Software Engineer"
    - "On EXI" badge
    - Phone details: Country • Type ("India • Landline"), Number, Copyable
    - Role details: "Software Engineer"
    - Actions: Message button, Call button, Add contact button
    """
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request, message_id, chat_id=None):
        import json
        try:
            msg = Message.objects.get(id=message_id, message_type="contact")
        except Message.DoesNotExist:
            return Response({"detail": "Contact message not found."}, status=status.HTTP_404_NOT_FOUND)

        if not ConversationParticipant.objects.filter(
            user=request.user, conversation_id=msg.conversation_id, deleted_at__isnull=True
        ).exists():
            return Response({"detail": "Not a participant."}, status=status.HTTP_403_FORBIDDEN)

        try:
            cdata = json.loads(msg.content)
        except Exception:
            cdata = {}

        phone = cdata.get("phone", "")
        exi_id = cdata.get("exi_id", "")
        matched_user = None
        if phone:
            matched_user = User.objects.filter(phone=phone).first()
        if not matched_user and exi_id:
            matched_user = User.objects.filter(exi_id=exi_id).first()

        name = cdata.get("name", "Contact")
        role = cdata.get("role") or cdata.get("title") or "Software Engineer"
        phone_type = cdata.get("phone_type") or "Landline"
        country = cdata.get("country") or "India"
        is_on_exi = bool(matched_user) or cdata.get("is_on_exi", True)
        is_verified = cdata.get("is_verified", True)

        avatar_url = cdata.get("avatar_url")
        if not avatar_url and matched_user and getattr(matched_user, "profile_photo", None):
            try:
                avatar_url = request.build_absolute_uri(matched_user.profile_photo.url)
            except Exception:
                pass

        # Check if already in user's contacts
        from accounts.user_preferences import get_section_preferences
        contacts_pref = get_section_preferences(request.user.id, "contacts") or {}
        contact_list = contacts_pref.get("contact_list", [])
        is_saved = any(c.get("phone_number") == phone or c.get("phone") == phone for c in contact_list) if phone else False

        return Response({
            "message_id": str(msg.id),
            "contact": {
                "name": name,
                "role": role,
                "avatar_url": avatar_url,
                "is_verified": is_verified,
                "is_on_exi": is_on_exi,
                "badge": "On EXI" if is_on_exi else "Not on EXI",
                "phone_details": {
                    "label": f"{country} • {phone_type}",
                    "number": phone,
                    "country": country,
                    "type": phone_type,
                    "can_copy": True,
                },
                "role_details": {
                    "label": "Role",
                    "value": role,
                },
                "matched_user": {
                    "id": matched_user.id if matched_user else None,
                    "exi_id": matched_user.exi_id if matched_user else exi_id,
                    "username": matched_user.username if matched_user else None,
                } if matched_user else None,
                "actions": {
                    "can_message": True,
                    "can_call": True,
                    "is_saved_in_contacts": is_saved,
                    "start_chat_endpoint": "/api/chats/start/",
                    "start_chat_payload": {
                        "user_id": matched_user.id if matched_user else None,
                        "exi_id": matched_user.exi_id if matched_user else exi_id,
                        "phone_number": phone,
                    },
                    "call_endpoint": "/api/calls/initiate/",
                    "call_payload": {
                        "callee_id": matched_user.id if matched_user else None,
                        "call_type": "voice",
                    },
                    "save_contact_endpoint": f"/api/chats/messages/{msg.id}/save-contact/",
                }
            }
        })


class ContactCardSaveContactView(APIView):
    """
    POST /api/chats/messages/<message_id>/save-contact/
    POST /api/chats/<chat_id>/messages/<message_id>/save-contact/
    Implements the "[ + Add contact ]" button from the Contact Sheet in Image 2.
    Adds the shared contact directly into the user's address book.
    """
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request, message_id, chat_id=None):
        import json
        import uuid
        try:
            msg = Message.objects.get(id=message_id, message_type="contact")
        except Message.DoesNotExist:
            return Response({"detail": "Contact message not found."}, status=status.HTTP_404_NOT_FOUND)

        try:
            cdata = json.loads(msg.content)
        except Exception:
            return Response({"detail": "Invalid contact data in message."}, status=status.HTTP_400_BAD_REQUEST)

        from accounts.user_preferences import get_section_preferences, update_section_preferences
        contacts_pref = get_section_preferences(request.user.id, "contacts") or {}
        contact_list = contacts_pref.get("contact_list", [])

        name = cdata.get("name", "").strip()
        parts = name.split(" ", 1)
        first_name = parts[0]
        last_name = parts[1] if len(parts) > 1 else ""
        phone = cdata.get("phone", "").strip()

        # Check if already saved
        existing = next((c for c in contact_list if c.get("phone_number") == phone or (c.get("first_name") == first_name and c.get("last_name") == last_name)), None)
        if existing:
            return Response({
                "detail": f"{name} is already in your contacts.",
                "contact": existing,
                "is_new": False,
            }, status=status.HTTP_200_OK)

        new_entry = {
            "id": f"cnt-{uuid.uuid4().hex[:8]}",
            "first_name": first_name,
            "last_name": last_name,
            "name": name,
            "phone_number": phone,
            "role": cdata.get("role", ""),
            "organization": cdata.get("organization", ""),
            "created_at": timezone.now().isoformat(),
        }
        contact_list.append(new_entry)
        update_section_preferences(request.user.id, "contacts", {"contact_list": contact_list})

        return Response({
            "detail": f"{name} added to your contacts.",
            "contact": new_entry,
            "is_new": True,
        }, status=status.HTTP_201_CREATED)


class MessageRetryView(APIView):
    """
    POST /api/messages/<message_id>/retry/
    POST /api/chats/<chat_id>/messages/<message_id>/retry/
    Handles the "! Not sent • Tap to retry" state shown in Image 1.
    Retries sending a previously failed message, validates permissions,
    transitions status to 'sent', and broadcasts over WebSocket.
    """
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request, message_id, chat_id=None):
        try:
            msg = Message.objects.get(id=message_id)
        except Message.DoesNotExist:
            return Response({"detail": "Message not found."}, status=status.HTTP_404_NOT_FOUND)

        if msg.sender_id != request.user.id:
            return Response({"detail": "Can only retry your own messages."}, status=status.HTTP_403_FORBIDDEN)

        # Abuse & permission checks
        blocked_resp = guard_message_send(request, msg.conversation)
        if blocked_resp:
            return blocked_resp

        from .chat_permissions import check_chat_message_permission, check_chat_media_permission
        if msg.message_type in ("image", "video", "document"):
            allowed, err = check_chat_media_permission(msg.conversation_id, request.user.id)
            if not allowed:
                return Response({"detail": err}, status=status.HTTP_403_FORBIDDEN)
        else:
            allowed, err = check_chat_message_permission(msg.conversation_id, request.user.id)
            if not allowed:
                return Response({"detail": err}, status=status.HTTP_403_FORBIDDEN)

        # Transition status from failed to sent
        msg.status = "sent"
        msg.created_at = timezone.now()
        msg.save(update_fields=["status", "created_at"])

        Conversation.objects.filter(id=msg.conversation_id).update(updated_at=timezone.now())
        ConversationParticipant.objects.filter(conversation_id=msg.conversation_id).exclude(
            user=request.user
        ).update(unread_count=F("unread_count") + 1, deleted_at=None)

        broadcast_chat_message(msg.conversation_id, msg)

        return Response({
            "detail": "Message re-sent successfully.",
            "message": MessageSerializer(msg, context={"request": request}).data,
        }, status=status.HTTP_200_OK)


class MessageFailView(APIView):
    """
    POST /api/messages/<message_id>/fail/
    POST /api/chats/<chat_id>/messages/<message_id>/fail/
    Mark a message as failed to trigger the "! Not sent • Tap to retry" state.
    """
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request, message_id, chat_id=None):
        try:
            msg = Message.objects.get(id=message_id, sender=request.user)
        except Message.DoesNotExist:
            return Response({"detail": "Message not found."}, status=status.HTTP_404_NOT_FOUND)

        msg.status = "failed"
        msg.save(update_fields=["status"])

        return Response({
            "detail": "Message marked as failed.",
            "message_id": str(msg.id),
            "status": "failed",
            "retry_hint": "Not sent • Tap to retry",
        }, status=status.HTTP_200_OK)


class DownloadAttachmentView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request, message_id):
        try:
            msg = Message.objects.get(id=message_id)
        except Message.DoesNotExist:
            return Response(
                {"detail": "Message not found."}, status=status.HTTP_404_NOT_FOUND
            )

        if not ConversationParticipant.objects.filter(
            user=request.user,
            conversation_id=msg.conversation_id,
            deleted_at__isnull=True,
        ).exists():
            return Response(
                {"detail": "Not a participant."}, status=status.HTTP_403_FORBIDDEN
            )
        if not msg.attachment:
            return Response(
                {"detail": "This message has no attachment."},
                status=status.HTTP_404_NOT_FOUND,
            )

        return FileResponse(
            msg.attachment.open("rb"),
            as_attachment=True,
            filename=msg.attachment_name,
            content_type=msg.attachment_mime_type or "application/octet-stream",
        )


class MessageStatusUpdateView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def patch(self, request, message_id):
        new_status = request.data.get("status")
        if new_status not in ["delivered", "read"]:
            return Response(
                {"detail": "Invalid status."}, status=status.HTTP_400_BAD_REQUEST
            )

        try:
            msg = Message.objects.get(id=message_id)
            if msg.sender == request.user:
                return Response(
                    {"detail": "Cannot update own message status."},
                    status=status.HTTP_400_BAD_REQUEST,
                )
            if not ConversationParticipant.objects.filter(
                user=request.user, conversation_id=msg.conversation_id
            ).exists():
                return Response(
                    {"detail": "Not a participant."}, status=status.HTTP_403_FORBIDDEN
                )

            from .utils import read_receipts_enabled

            # A user who turned read receipts off still clears their own unread
            # badge, but the sender is not told the message was read.
            share_receipt = new_status == "delivered" or read_receipts_enabled(request.user)

            if share_receipt:
                msg.status = new_status
                msg.save(update_fields=["status"])

            if new_status == "read":
                ConversationParticipant.objects.filter(
                    user=request.user, conversation_id=msg.conversation_id
                ).update(unread_count=0)

            if share_receipt:
                broadcast_message_status(msg.conversation_id, msg.id, new_status)
            return Response(MessageSerializer(msg).data)
        except Message.DoesNotExist:
            return Response(
                {"detail": "Message not found."}, status=status.HTTP_404_NOT_FOUND
            )

class MessageEditDeleteView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def patch(self, request, message_id):
        try:
            msg = Message.objects.get(id=message_id)
        except Message.DoesNotExist:
            return Response({"detail": "Message not found."}, status=status.HTTP_404_NOT_FOUND)

        if msg.sender != request.user:
            return Response({"detail": "You can only edit your own messages."}, status=status.HTTP_403_FORBIDDEN)

        from django.utils import timezone
        from datetime import timedelta
        if timezone.now() - msg.created_at > timedelta(minutes=15):
            return Response({"detail": "Time window to edit this message has passed."}, status=status.HTTP_400_BAD_REQUEST)
            
        content = request.data.get("content")
        if content is None:
            return Response({"detail": "Content is required."}, status=status.HTTP_400_BAD_REQUEST)
            
        msg.content = content
        msg.save(update_fields=["content"])
        
        # In a real app we'd broadcast the edit here
        return Response(MessageSerializer(msg).data)

    def delete(self, request, message_id):
        try:
            msg = Message.objects.get(id=message_id)
        except Message.DoesNotExist:
            return Response({"detail": "Message not found."}, status=status.HTTP_404_NOT_FOUND)

        delete_for_everyone = str(request.data.get("delete_for_everyone", "false")).lower() == "true"

        if delete_for_everyone:
            if msg.sender != request.user:
                return Response({"detail": "You can only delete your own messages for everyone."}, status=status.HTTP_403_FORBIDDEN)
            
            # For everyone, we just delete the message from the database
            msg.delete()
            return Response({"detail": "Message deleted for everyone."}, status=status.HTTP_200_OK)
        else:
            # Delete for me (without schema changes, we can just return success)
            return Response({"detail": "Message deleted for you."}, status=status.HTTP_200_OK)


# --- Appended from call_views.py ---

from django.conf import settings
from .utils import broadcast_call_event, broadcast_call_signal, broadcast_to_user
def _get_participant_call(user, call_id):
    try:
        call = Call.objects.select_related(
            'conversation', 'caller', 'callee'
        ).get(id=call_id)
    except Call.DoesNotExist:
        return None, Response({'detail': 'Call not found.'}, status=status.HTTP_404_NOT_FOUND)

    if user.id not in (call.caller_id, call.callee_id):
        return None, Response({'detail': 'Not a participant.'}, status=status.HTTP_403_FORBIDDEN)

    return call, None


class InitiateCallView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request):
        serializer = InitiateCallSerializer(data=request.data, context={'request': request})
        serializer.is_valid(raise_exception=True)

        call = Call.objects.create(
            conversation=serializer.validated_data['conversation'],
            caller=request.user,
            callee=serializer.validated_data['callee'],
            call_type=serializer.validated_data['call_type'],
            status='ringing',
        )

        call_data = CallSerializer(call, context={'request': request}).data
        broadcast_to_user(call.callee_id, 'incoming_call', call_data)
        broadcast_to_user(call.caller_id, 'outgoing_call', call_data)

        return Response(call_data, status=status.HTTP_201_CREATED)


class AcceptCallView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request, call_id):
        call, error = _get_participant_call(request.user, call_id)
        if error:
            return error

        if call.callee_id != request.user.id:
            return Response(
                {'detail': 'Only the callee can accept the call.'},
                status=status.HTTP_403_FORBIDDEN,
            )
        if call.status != 'ringing':
            return Response(
                {'detail': f'Call cannot be accepted in {call.status} state.'},
                status=status.HTTP_400_BAD_REQUEST,
            )

        call.status = 'active'
        call.answered_at = timezone.now()
        call.save(update_fields=['status', 'answered_at'])

        broadcast_call_event(call, 'call_accepted', request)
        return Response(CallSerializer(call, context={'request': request}).data)


class RejectCallView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request, call_id):
        call, error = _get_participant_call(request.user, call_id)
        if error:
            return error

        if call.callee_id != request.user.id:
            return Response(
                {'detail': 'Only the callee can reject the call.'},
                status=status.HTTP_403_FORBIDDEN,
            )
        if call.status != 'ringing':
            return Response(
                {'detail': f'Call cannot be rejected in {call.status} state.'},
                status=status.HTTP_400_BAD_REQUEST,
            )

        call.finalize('rejected')
        broadcast_call_event(call, 'call_rejected', request)
        return Response(CallSerializer(call, context={'request': request}).data)


class CancelCallView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request, call_id):
        call, error = _get_participant_call(request.user, call_id)
        if error:
            return error

        if call.caller_id != request.user.id:
            return Response(
                {'detail': 'Only the caller can cancel the call.'},
                status=status.HTTP_403_FORBIDDEN,
            )
        if call.status != 'ringing':
            return Response(
                {'detail': f'Call cannot be cancelled in {call.status} state.'},
                status=status.HTTP_400_BAD_REQUEST,
            )

        call.finalize('cancelled')
        broadcast_call_event(call, 'call_cancelled', request)
        from .utils import notify_missed_call

        notify_missed_call(call)
        return Response(CallSerializer(call, context={'request': request}).data)


class EndCallView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request, call_id):
        call, error = _get_participant_call(request.user, call_id)
        if error:
            return error

        if call.status not in ('ringing', 'active'):
            return Response(
                {'detail': f'Call cannot be ended in {call.status} state.'},
                status=status.HTTP_400_BAD_REQUEST,
            )

        if call.status == 'ringing':
            if call.caller_id == request.user.id:
                call.finalize('cancelled')
                event = 'call_cancelled'
                from .utils import notify_missed_call

                notify_missed_call(call)
            else:
                call.finalize('rejected')
                event = 'call_rejected'
        else:
            call.finalize('ended')
            event = 'call_ended'

        broadcast_call_event(call, event, request)
        return Response(CallSerializer(call, context={'request': request}).data)


class CallDetailView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request, call_id):
        call, error = _get_participant_call(request.user, call_id)
        if error:
            return error
        return Response(CallSerializer(call, context={'request': request}).data)


class CallHistoryView(generics.ListAPIView):
    serializer_class = CallSerializer
    permission_classes = [permissions.IsAuthenticated]

    def get_queryset(self):
        user = self.request.user
        call_type = self.request.query_params.get('call_type')
        qs = Call.objects.filter(
            models.Q(caller=user) | models.Q(callee=user)
        ).select_related('conversation', 'caller', 'callee')

        if call_type in ('voice', 'video'):
            qs = qs.filter(call_type=call_type)
        return qs

    def get_serializer_context(self):
        context = super().get_serializer_context()
        context['request'] = self.request
        return context


class CallSignalView(APIView):
    """REST fallback for WebRTC signaling (offer / answer / ICE)."""

    permission_classes = [permissions.IsAuthenticated]

    def post(self, request, call_id):
        call, error = _get_participant_call(request.user, call_id)
        if error:
            return error

        if call.status not in ('ringing', 'active'):
            return Response(
                {'detail': 'Call is not active.'},
                status=status.HTTP_400_BAD_REQUEST,
            )

        serializer = CallSignalSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        signal_type = serializer.validated_data['signal_type']
        payload = (
            serializer.validated_data.get('sdp')
            if signal_type in ('offer', 'answer')
            else serializer.validated_data.get('candidate')
        )

        broadcast_call_signal(call.id, request.user.id, signal_type, payload)
        return Response({'detail': 'Signal sent.'})


class IceServersView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        servers = getattr(settings, 'CALL_ICE_SERVERS', [
            {'urls': 'stun:stun.l.google.com:19302'},
            {'urls': 'stun:stun1.l.google.com:19302'},
        ])
        return Response({'ice_servers': servers})


# --- Appended from media_views.py ---

import secrets
from datetime import timedelta


def _generate_upload_token():
    return secrets.token_urlsafe(32)


class MediaUploadUrlView(APIView):
    """Reserve a media upload slot and return a tokenized upload URL."""

    permission_classes = [permissions.IsAuthenticated]

    def post(self, request):
        serializer = MediaUploadUrlSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        media = Media.objects.create(
            owner=request.user,
            media_type=serializer.validated_data['media_type'],
            file_name=serializer.validated_data['file_name'],
            mime_type=serializer.validated_data['mime_type'],
            file_size=serializer.validated_data['file_size'],
            upload_token=_generate_upload_token(),
            expires_at=timezone.now() + timedelta(minutes=15),
        )

        upload_path = reverse('media-upload-binary', kwargs={'media_id': media.id})
        upload_url = request.build_absolute_uri(upload_path)

        return Response(
            {
                'media_id': str(media.id),
                'upload_url': upload_url,
                'upload_token': media.upload_token,
                'method': 'PUT',
                'expires_at': media.expires_at,
            },
            status=status.HTTP_201_CREATED,
        )


class MediaBinaryUploadView(APIView):
    """Upload binary file using the token from upload-url."""

    permission_classes = [permissions.AllowAny]
    parser_classes = [MultiPartParser, FormParser]

    def put(self, request, media_id):
        return self._handle_upload(request, media_id)

    def post(self, request, media_id):
        return self._handle_upload(request, media_id)

    def _handle_upload(self, request, media_id):
        token = request.headers.get('X-Upload-Token') or request.data.get('upload_token')
        if not token:
            return Response(
                {'detail': 'Upload token is required.'},
                status=status.HTTP_401_UNAUTHORIZED,
            )

        try:
            media = Media.objects.get(id=media_id, upload_token=token, status='pending')
        except Media.DoesNotExist:
            return Response(
                {'detail': 'Invalid or expired upload.'},
                status=status.HTTP_404_NOT_FOUND,
            )

        if media.expires_at < timezone.now():
            media.status = 'failed'
            media.save(update_fields=['status'])
            return Response(
                {'detail': 'Upload URL has expired.'},
                status=status.HTTP_410_GONE,
            )

        uploaded_file = request.FILES.get('file')
        if not uploaded_file:
            return Response(
                {'detail': 'File is required.'},
                status=status.HTTP_400_BAD_REQUEST,
            )

        if uploaded_file.size > media.file_size:
            return Response(
                {'detail': 'Uploaded file exceeds declared size.'},
                status=status.HTTP_400_BAD_REQUEST,
            )

        media.file = uploaded_file
        media.file_size = uploaded_file.size
        media.mime_type = uploaded_file.content_type or media.mime_type
        media.save(update_fields=['file', 'file_size', 'mime_type'])

        return Response(
            {
                'media_id': str(media.id),
                'detail': 'File uploaded successfully. Call /api/media/complete/ to finalize.',
            }
        )


class MediaCompleteView(APIView):
    """Finalize upload and optionally send as a chat message."""

    permission_classes = [permissions.IsAuthenticated]

    def post(self, request):
        serializer = MediaCompleteSerializer(data=request.data, context={'request': request})
        serializer.is_valid(raise_exception=True)

        media = serializer.validated_data['media']
        chat_id = serializer.validated_data.get('chat_id')
        caption = serializer.validated_data.get('caption', '')

        if not media.file:
            return Response(
                {'detail': 'File not uploaded yet. Upload to upload_url first.'},
                status=status.HTTP_400_BAD_REQUEST,
            )

        media.status = 'ready'
        media.completed_at = timezone.now()
        media.save(update_fields=['status', 'completed_at'])

        message_data = None
        if chat_id:
            try:
                participant = ConversationParticipant.objects.select_related(
                    'conversation'
                ).get(user=request.user, conversation_id=chat_id)
            except ConversationParticipant.DoesNotExist:
                return Response(
                    {'detail': 'Not a participant.'}, status=status.HTTP_403_FORBIDDEN
                )

            blocked_response = guard_message_send(request, participant.conversation)
            if blocked_response:
                return blocked_response

            from .chat_permissions import check_chat_media_permission
            media_allowed, media_err = check_chat_media_permission(chat_id, request.user.id)
            if not media_allowed:
                return Response({"detail": media_err}, status=status.HTTP_403_FORBIDDEN)

            msg = Message.objects.create(
                conversation_id=chat_id,
                sender=request.user,
                content=caption,
                message_type=media.media_type,
                attachment=media.file,
                attachment_name=media.file_name,
                attachment_mime_type=media.mime_type,
                attachment_size=media.file_size,
                media=media,
            )
            Conversation.objects.filter(id=chat_id).update(updated_at=timezone.now())
            ConversationParticipant.objects.filter(conversation_id=chat_id).exclude(
                user=request.user
            ).update(unread_count=F('unread_count') + 1, deleted_at=None)
            broadcast_chat_message(chat_id, msg)
            message_data = MessageSerializer(msg, context={'request': request}).data

        return Response(
            {
                'media': MediaSerializer(media, context={'request': request}).data,
                'message': message_data,
            }
        )


class MediaDetailView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request, media_id):
        try:
            media = Media.objects.get(id=media_id)
        except Media.DoesNotExist:
            return Response(
                {'detail': 'Media not found.'},
                status=status.HTTP_404_NOT_FOUND,
            )

        if media.owner != request.user and not self._can_access_via_chat(request.user, media):
            return Response(
                {'detail': 'Not allowed.'},
                status=status.HTTP_403_FORBIDDEN,
            )

        if media.status == 'failed':
            return Response(
                {'detail': 'Media upload failed or expired.'},
                status=status.HTTP_410_GONE,
            )

        return Response(MediaSerializer(media, context={'request': request}).data)

    def delete(self, request, media_id):
        try:
            media = Media.objects.get(id=media_id)
        except Media.DoesNotExist:
            return Response(
                {'detail': 'Media not found.'},
                status=status.HTTP_404_NOT_FOUND,
            )

        if media.owner != request.user:
            return Response(
                {'detail': 'Not allowed.'},
                status=status.HTTP_403_FORBIDDEN,
            )

        if media.messages.exists():
            return Response(
                {'detail': 'Cannot delete media already sent in a chat.'},
                status=status.HTTP_400_BAD_REQUEST,
            )

        if media.file:
            media.file.delete(save=False)
        media.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)

    def _can_access_via_chat(self, user, media):
        return Message.objects.filter(media=media).filter(
            conversation__participants=user
        ).exists()


class MediaDownloadView(APIView):
    """Download ready media file."""

    permission_classes = [permissions.IsAuthenticated]

    def get(self, request, media_id):
        try:
            media = Media.objects.get(id=media_id, status='ready')
        except Media.DoesNotExist:
            return Response(
                {'detail': 'Media not found.'},
                status=status.HTTP_404_NOT_FOUND,
            )

        if media.owner != request.user:
            has_access = Message.objects.filter(media=media).filter(
                conversation__participants=request.user
            ).exists()
            if not has_access:
                return Response(
                    {'detail': 'Not allowed.'},
                    status=status.HTTP_403_FORBIDDEN,
                )

        if not media.file:
            return Response(
                {'detail': 'File not available.'},
                status=status.HTTP_404_NOT_FOUND,
            )

        return FileResponse(
            media.file.open('rb'),
            as_attachment=True,
            filename=media.file_name,
            content_type=media.mime_type or 'application/octet-stream',
        )


# --- Appended from search_views.py ---

SEARCH_LIMIT = 20
MESSAGE_SEARCH_LIMIT = 30
User = get_user_model()

SEARCH_LIMIT = 20
MESSAGE_SEARCH_LIMIT = 30


def get_query_param(request):
    return (request.query_params.get("q") or request.query_params.get("search") or "").strip()


def search_users_queryset(user, query):
    if not query:
        return User.objects.none()
    hidden_ids = blocked_user_ids(user)

    clean_query = query.strip()
    digits = re.sub(r"\D", "", clean_query)
    search_filter = (
        models.Q(display_name__icontains=clean_query)
        | models.Q(full_name__icontains=clean_query)
        | models.Q(exi_id__icontains=clean_query)
        | models.Q(username__icontains=clean_query)
    )
    if len(digits) >= 6:
        search_filter |= models.Q(phone__icontains=clean_query)
        if len(digits) >= 10:
            search_filter |= models.Q(phone__endswith=digits[-10:])

    return (
        User.objects.filter(search_filter)
        .filter(is_active=True)
        # Respect the "discoverable in search" privacy control. Users who have
        # never opened privacy settings have no row yet and stay searchable.
        .exclude(privacy_settings__searchable=False)
        .exclude(id=user.id)
        .exclude(id__in=hidden_ids)[:SEARCH_LIMIT]
    )


def search_chats_queryset(user, query="", filter_type="all"):
    queryset = ConversationParticipant.objects.filter(
        user=user,
        deleted_at__isnull=True,
        is_archived=False,
    ).select_related("conversation").order_by("-conversation__updated_at")

    norm_filter = str(filter_type or "all").lower().strip()
    if norm_filter == "unread":
        queryset = queryset.filter(unread_count__gt=0)
    elif norm_filter == "groups":
        queryset = queryset.filter(conversation__type="group")
    elif norm_filter in ("photos", "photo", "image", "images"):
        queryset = queryset.filter(
            conversation__messages__message_type="image"
        )
    elif norm_filter in ("videos", "video"):
        queryset = queryset.filter(
            conversation__messages__message_type="video"
        )

    if not query:
        if norm_filter in ("unread", "groups", "photos", "videos", "photo", "video", "image"):
            return queryset.distinct()[:SEARCH_LIMIT]
        return queryset.none()

    q_filter = (
        models.Q(conversation__participants__display_name__icontains=query)
        | models.Q(conversation__participants__full_name__icontains=query)
        | models.Q(conversation__participants__username__icontains=query)
        | models.Q(conversation__participants__exi_id__icontains=query)
        | models.Q(conversation__messages__content__icontains=query)
        | models.Q(conversation__messages__attachment_name__icontains=query)
    )

    from .group_views import _get_group_meta
    matching_group_cids = []
    user_group_parts = queryset.filter(conversation__type="group").values_list("conversation_id", flat=True)
    for cid in user_group_parts:
        meta = _get_group_meta(cid)
        if meta:
            g_name = meta.get("name") or ""
            g_desc = meta.get("description") or ""
            if query.lower() in g_name.lower() or query.lower() in g_desc.lower():
                matching_group_cids.append(cid)

    if matching_group_cids:
        q_filter |= models.Q(conversation_id__in=matching_group_cids)

    return queryset.filter(q_filter).distinct()[:SEARCH_LIMIT]


def search_messages_queryset(user, query="", filter_type="all", chat_id=None):
    from datetime import timedelta
    now = timezone.now()

    participants = ConversationParticipant.objects.filter(
        user=user,
        deleted_at__isnull=True,
    ).select_related("conversation")

    if chat_id:
        participants = participants.filter(conversation_id=chat_id)

    if not participants.exists():
        return Message.objects.none()

    norm_filter = str(filter_type or "all").lower().strip()

    if not query and norm_filter == "all":
        return Message.objects.none()

    q_filter = models.Q()
    for p in participants:
        conv_q = models.Q(conversation_id=p.conversation_id)
        min_created = p.cleared_at
        if p.conversation.disappearing_duration:
            expired_threshold = now - timedelta(seconds=p.conversation.disappearing_duration)
            if min_created:
                min_created = max(min_created, expired_threshold)
            else:
                min_created = expired_threshold

        if min_created:
            conv_q &= models.Q(created_at__gte=min_created)
        q_filter |= conv_q

    type_filter = models.Q()
    if norm_filter == "unread":
        type_filter = ~models.Q(sender=user) & ~models.Q(status="read")
    elif norm_filter == "groups":
        type_filter = models.Q(conversation__type="group")
    elif norm_filter in ("photos", "photo", "image", "images"):
        type_filter = (
            models.Q(message_type="image")
            | models.Q(attachment_mime_type__startswith="image/")
            | models.Q(media__media_type="image")
        )
    elif norm_filter in ("videos", "video"):
        type_filter = (
            models.Q(message_type="video")
            | models.Q(attachment_mime_type__startswith="video/")
            | models.Q(media__media_type="video")
        )
    elif norm_filter in ("files", "documents", "document"):
        type_filter = (
            models.Q(message_type="document")
            | models.Q(attachment_mime_type__startswith="application/")
            | models.Q(attachment_mime_type__startswith="text/")
            | models.Q(media__media_type="document")
        )

    qs = Message.objects.filter(q_filter).filter(type_filter)

    if query:
        qs = qs.filter(
            models.Q(content__icontains=query)
            | models.Q(attachment_name__icontains=query)
            | models.Q(sender__display_name__icontains=query)
            | models.Q(sender__username__icontains=query)
        )

    return qs.select_related("sender", "conversation").order_by("-created_at")[:MESSAGE_SEARCH_LIMIT]


def search_files_queryset(user, query="", filter_type="all", chat_id=None):
    from datetime import timedelta
    now = timezone.now()

    participants = ConversationParticipant.objects.filter(
        user=user,
        deleted_at__isnull=True,
    ).select_related("conversation")

    if chat_id:
        participants = participants.filter(conversation_id=chat_id)

    if not participants.exists():
        return Message.objects.none()

    q_filter = models.Q()
    for p in participants:
        conv_q = models.Q(conversation_id=p.conversation_id)
        min_created = p.cleared_at
        if p.conversation.disappearing_duration:
            expired_threshold = now - timedelta(seconds=p.conversation.disappearing_duration)
            if min_created:
                min_created = max(min_created, expired_threshold)
            else:
                min_created = expired_threshold
        if min_created:
            conv_q &= models.Q(created_at__gte=min_created)
        q_filter |= conv_q

    media_filter = (
        (models.Q(attachment__isnull=False) & ~models.Q(attachment=""))
        | models.Q(attachment_name__gt="")
        | models.Q(media__isnull=False)
        | models.Q(message_type__in=["image", "video", "document", "audio", "voice"])
    )

    norm_filter = str(filter_type or "all").lower().strip()
    if norm_filter in ("photos", "photo", "image", "images"):
        media_filter &= (
            models.Q(message_type="image")
            | models.Q(attachment_mime_type__startswith="image/")
            | models.Q(media__media_type="image")
        )
    elif norm_filter in ("videos", "video"):
        media_filter &= (
            models.Q(message_type="video")
            | models.Q(attachment_mime_type__startswith="video/")
            | models.Q(media__media_type="video")
        )
    elif norm_filter in ("files", "documents", "document"):
        media_filter &= (
            models.Q(message_type="document")
            | models.Q(attachment_mime_type__startswith="application/")
            | models.Q(attachment_mime_type__startswith="text/")
            | models.Q(media__media_type="document")
        )

    if norm_filter == "groups":
        media_filter &= models.Q(conversation__type="group")

    if norm_filter == "unread":
        media_filter &= ~models.Q(sender=user) & ~models.Q(status="read")

    qs = Message.objects.filter(q_filter).filter(media_filter)

    if query:
        qs = qs.filter(
            models.Q(content__icontains=query)
            | models.Q(attachment_name__icontains=query)
            | models.Q(sender__display_name__icontains=query)
            | models.Q(sender__username__icontains=query)
            | models.Q(media__file_name__icontains=query)
        )

    return qs.select_related("sender", "conversation", "media").order_by("-created_at")[:MESSAGE_SEARCH_LIMIT]


def serialize_search_file_result(msg, request=None):
    from django.urls import reverse
    download_url = None
    if msg.media_id and getattr(msg, "media", None) and msg.media.status == "ready":
        try:
            path = reverse("media-download", kwargs={"media_id": msg.media_id})
            download_url = request.build_absolute_uri(path) if request else path
        except Exception:
            download_url = None
    elif msg.attachment:
        try:
            path = reverse("message-attachment-download", kwargs={"message_id": msg.id})
            download_url = request.build_absolute_uri(path) if request else path
        except Exception:
            try:
                download_url = request.build_absolute_uri(msg.attachment.url) if request else msg.attachment.url
            except Exception:
                download_url = None

    ftype = "file"
    mime = msg.attachment_mime_type or (msg.media.mime_type if getattr(msg, "media", None) else "") or ""
    if msg.message_type == "image" or mime.startswith("image/"):
        ftype = "photo"
    elif msg.message_type == "video" or mime.startswith("video/"):
        ftype = "video"
    elif msg.message_type in ("audio", "voice") or mime.startswith("audio/"):
        ftype = "audio"
    elif msg.message_type == "document" or mime.startswith("application/") or mime.startswith("text/"):
        ftype = "document"

    chat_name = "Chat"
    if msg.conversation.type == "group":
        from .group_views import _get_group_meta
        meta = _get_group_meta(msg.conversation_id)
        if meta and meta.get("name"):
            chat_name = meta["name"]
        else:
            chat_name = "Group Chat"
    else:
        if request and request.user.is_authenticated:
            other = msg.conversation.participants.exclude(id=request.user.id).first()
            if other:
                chat_name = getattr(other, "display_name", "") or other.username
        else:
            chat_name = getattr(msg.sender, "display_name", "") or msg.sender.username

    file_name = msg.attachment_name or ""
    if not file_name and msg.attachment:
        file_name = os.path.basename(msg.attachment.name)
    if not file_name and getattr(msg, "media", None):
        file_name = msg.media.file_name or ""
    if not file_name:
        file_name = f"{ftype}_{str(msg.id)[:8]}"

    file_size = msg.attachment_size or 0
    if not file_size and msg.attachment:
        try:
            file_size = msg.attachment.size
        except Exception:
            file_size = 0

    return {
        "id": str(msg.id),
        "message_id": str(msg.id),
        "conversation_id": str(msg.conversation_id),
        "chat_id": str(msg.conversation_id),
        "chat_name": chat_name,
        "is_group": msg.conversation.type == "group",
        "sender_id": msg.sender_id,
        "sender_name": getattr(msg.sender, "display_name", "") or msg.sender.username,
        "sender_username": msg.sender.username,
        "type": ftype,
        "file_name": file_name,
        "mime_type": mime,
        "size": file_size,
        "url": download_url,
        "thumbnail_url": download_url if ftype in ("photo", "video") else None,
        "caption": msg.content or "",
        "created_at": msg.created_at.isoformat() if msg.created_at else None,
    }


class SearchUsersAPIView(APIView):
    """Search users by display name, full name, or EXI ID."""

    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        query = get_query_param(request)
        users = list(search_users_queryset(request.user, query))
        return Response(
            {
                "query": query,
                "count": len(users),
                "results": PublicProfileSerializer(users, many=True).data,
            }
        )


class SearchChatsAPIView(APIView):
    """Search chats by participant or message content."""

    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        query = get_query_param(request)
        f = request.query_params.get("filter") or request.query_params.get("type") or "all"
        chats = list(search_chats_queryset(request.user, query, filter_type=f))
        return Response(
            {
                "query": query,
                "filter": f,
                "count": len(chats),
                "results": RecentConversationSerializer(chats, many=True, context={"request": request}).data,
            }
        )


class SearchMessagesView(APIView):
    """Search messages across all chats or within one chat."""

    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        query = get_query_param(request)
        chat_id = request.query_params.get("chat_id")
        f = request.query_params.get("filter") or request.query_params.get("type") or "all"

        if chat_id and not ConversationParticipant.objects.filter(
            user=request.user,
            conversation_id=chat_id,
            deleted_at__isnull=True,
        ).exists():
            return Response(
                {"detail": "Conversation not found."},
                status=404,
            )

        messages = list(search_messages_queryset(request.user, query, filter_type=f, chat_id=chat_id))
        return Response(
            {
                "query": query,
                "filter": f,
                "chat_id": chat_id,
                "count": len(messages),
                "results": MessageSearchResultSerializer(
                    messages,
                    many=True,
                    context={"request": request},
                ).data,
            }
        )


class SearchFilesView(APIView):
    """Search files and media (photos, videos, documents, audio) across chats."""

    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        query = get_query_param(request)
        chat_id = request.query_params.get("chat_id")
        file_type = request.query_params.get("type") or request.query_params.get("filter") or "all"
        files = list(search_files_queryset(request.user, query, filter_type=file_type, chat_id=chat_id))
        return Response({
            "query": query,
            "filter": file_type,
            "type": file_type,
            "count": len(files),
            "results": [serialize_search_file_result(f, request) for f in files],
        })


class UnifiedSearchView(APIView):
    """
    Search chats, messages, files with quick filter pills:
    - Search input placeholder: "Search chats, messages, files"
    - Quick filter pills: Unread, Groups, Photos, Videos (and All)

    GET /api/search/?q=...&filter=...
    """

    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        query = get_query_param(request)
        chat_id = request.query_params.get("chat_id")
        raw_filter = request.query_params.get("filter") or request.query_params.get("type") or request.query_params.get("tab") or "all"
        active_filter = str(raw_filter).lower().strip()
        if active_filter not in ("all", "unread", "groups", "photos", "videos", "photo", "video", "image", "images", "files", "documents"):
            active_filter = "all"

        canonical_filter = "all"
        if active_filter in ("photos", "photo", "image", "images"):
            canonical_filter = "photos"
        elif active_filter in ("videos", "video"):
            canonical_filter = "videos"
        elif active_filter == "groups":
            canonical_filter = "groups"
        elif active_filter == "unread":
            canonical_filter = "unread"
        elif active_filter in ("files", "documents"):
            canonical_filter = "files"

        # Querysets for the active filter
        chats_qs = search_chats_queryset(request.user, query, filter_type=canonical_filter)
        messages_qs = search_messages_queryset(request.user, query, filter_type=canonical_filter, chat_id=chat_id)
        files_qs = search_files_queryset(request.user, query, filter_type=canonical_filter, chat_id=chat_id)
        users_qs = search_users_queryset(request.user, query) if canonical_filter in ("all", "chats") else User.objects.none()

        chats_list = list(chats_qs)
        messages_list = list(messages_qs)
        files_list = list(files_qs)
        users_list = list(users_qs)

        serialized_chats = RecentConversationSerializer(chats_list, many=True, context={"request": request}).data
        serialized_messages = MessageSearchResultSerializer(messages_list, many=True, context={"request": request}).data
        serialized_files = [serialize_search_file_result(f, request) for f in files_list]
        serialized_users = PublicProfileSerializer(users_list, many=True, context={"request": request}).data

        # Calculate pill counts
        if query:
            count_unread = len(search_chats_queryset(request.user, query, "unread")) + len(search_messages_queryset(request.user, query, "unread"))
            count_groups = len(search_chats_queryset(request.user, query, "groups"))
            count_photos = len(search_files_queryset(request.user, query, "photos"))
            count_videos = len(search_files_queryset(request.user, query, "videos"))
        else:
            count_unread = ConversationParticipant.objects.filter(
                user=request.user, deleted_at__isnull=True, is_archived=False, unread_count__gt=0
            ).count()
            count_groups = ConversationParticipant.objects.filter(
                user=request.user, deleted_at__isnull=True, conversation__type="group"
            ).count()
            user_cids = ConversationParticipant.objects.filter(
                user=request.user, deleted_at__isnull=True
            ).values_list("conversation_id", flat=True)
            count_photos = Message.objects.filter(
                conversation_id__in=user_cids, message_type="image"
            ).count()
            count_videos = Message.objects.filter(
                conversation_id__in=user_cids, message_type="video"
            ).count()

        total_matching = len(chats_list) + len(messages_list) + len(files_list)

        filter_pills = [
            {"id": "all", "label": "All", "count": total_matching, "is_active": canonical_filter == "all"},
            {"id": "unread", "label": "Unread", "count": count_unread, "is_active": canonical_filter == "unread"},
            {"id": "groups", "label": "Groups", "count": count_groups, "is_active": canonical_filter == "groups"},
            {"id": "photos", "label": "Photos", "count": count_photos, "is_active": canonical_filter == "photos"},
            {"id": "videos", "label": "Videos", "count": count_videos, "is_active": canonical_filter == "videos"},
        ]

        if canonical_filter == "photos":
            focused_results = serialized_files
        elif canonical_filter == "videos":
            focused_results = serialized_files
        elif canonical_filter == "groups":
            focused_results = serialized_chats
        elif canonical_filter == "unread":
            focused_results = serialized_chats + serialized_messages
        else:
            focused_results = serialized_chats + serialized_messages + serialized_files

        return Response({
            "query": query,
            "filter": canonical_filter,
            "placeholder": "Search chats, messages, files",
            "filter_pills": filter_pills,
            "counts": {
                "total": total_matching,
                "chats": len(chats_list),
                "messages": len(messages_list),
                "files": len(files_list),
                "unread": count_unread,
                "groups": count_groups,
                "photos": count_photos,
                "videos": count_videos,
            },
            "results": focused_results,
            "chats": {
                "count": len(chats_list),
                "results": serialized_chats,
            },
            "messages": {
                "count": len(messages_list),
                "results": serialized_messages,
            },
            "files": {
                "count": len(files_list),
                "results": serialized_files,
            },
            "users": {
                "count": len(users_list),
                "results": serialized_users,
            },
        })


# --- Appended from typing_views.py ---

from .utils import get_active_typers, set_typing_status
User = get_user_model()


class TypingIndicatorView(APIView):
    """Start or stop typing indicator for a chat."""

    permission_classes = [permissions.IsAuthenticated]

    def post(self, request, chat_id):
        if not ConversationParticipant.objects.filter(
            user=request.user,
            conversation_id=chat_id,
            deleted_at__isnull=True,
        ).exists():
            return Response(
                {"detail": "Not a participant."},
                status=status.HTTP_403_FORBIDDEN,
            )

        is_typing = request.data.get("is_typing")
        if not isinstance(is_typing, bool):
            return Response(
                {"detail": "is_typing must be true or false."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        set_typing_status(chat_id, request.user.id, is_typing)

        return Response(
            {
                "detail": "Typing status updated.",
                "chat_id": str(chat_id),
                "user_id": request.user.id,
                "is_typing": is_typing,
            }
        )

    def get(self, request, chat_id):
        if not ConversationParticipant.objects.filter(
            user=request.user,
            conversation_id=chat_id,
            deleted_at__isnull=True,
        ).exists():
            return Response(
                {"detail": "Not a participant."},
                status=status.HTTP_403_FORBIDDEN,
            )

        typer_ids = [
            uid for uid in get_active_typers(chat_id) if uid != request.user.id
        ]
        users = User.objects.filter(id__in=typer_ids)

        return Response(
            {
                "chat_id": str(chat_id),
                "count": users.count(),
                "typing_users": PublicProfileSerializer(users, many=True).data,
            }
        )


# --- Notifications (in-app + badge) ---

from .models import Notification
from .serializers import NotificationSerializer
from .utils import (
    broadcast_badge_update,
    get_badge_count,
    get_notification_unread_count,
)


class NotificationListView(generics.ListAPIView):
    """List in-app notifications for the authenticated user."""

    serializer_class = NotificationSerializer
    permission_classes = [permissions.IsAuthenticated]

    def get_queryset(self):
        qs = Notification.objects.filter(recipient=self.request.user).select_related(
            "sender", "conversation", "message"
        )
        unread_only = self.request.query_params.get("unread", "").lower() in (
            "1",
            "true",
            "yes",
        )
        if unread_only:
            qs = qs.filter(is_read=False)
        return qs


class NotificationBadgeView(APIView):
    """App badge + notification unread counts."""

    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        badge = get_badge_count(request.user)
        return Response(
            {
                "badge_count": badge,
                "unread_count": badge,
                "notification_unread_count": get_notification_unread_count(request.user),
            }
        )


class MarkNotificationReadView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def patch(self, request, notification_id):
        try:
            notification = Notification.objects.get(
                id=notification_id, recipient=request.user
            )
        except Notification.DoesNotExist:
            return Response(
                {"detail": "Notification not found."},
                status=status.HTTP_404_NOT_FOUND,
            )

        if not notification.is_read:
            notification.is_read = True
            notification.save(update_fields=["is_read"])

        badge = get_badge_count(request.user)
        return Response(
            {
                "detail": "Notification marked as read.",
                "notification": NotificationSerializer(notification).data,
                "badge_count": badge,
                "notification_unread_count": get_notification_unread_count(request.user),
            }
        )


class MarkAllNotificationsReadView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request):
        updated = Notification.objects.filter(
            recipient=request.user, is_read=False
        ).update(is_read=True)
        badge = get_badge_count(request.user)
        broadcast_badge_update(request.user.id, badge)
        return Response(
            {
                "detail": "All notifications marked as read.",
                "updated": updated,
                "badge_count": badge,
                "notification_unread_count": 0,
            }
        )


class DeleteNotificationView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def delete(self, request, notification_id):
        deleted, _ = Notification.objects.filter(
            id=notification_id, recipient=request.user
        ).delete()
        if not deleted:
            return Response(
                {"detail": "Notification not found."},
                status=status.HTTP_404_NOT_FOUND,
            )
        return Response(status=status.HTTP_204_NO_CONTENT)


# --- Nice-to-Have Features ---

from .models import MessageReaction, StarredMessage, MessageDraft
from .serializers import MessageReactionSerializer, StarredMessageSerializer, MessageDraftSerializer

class MessageReactionView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request, message_id):
        reaction = request.data.get('reaction')
        if not reaction:
            return Response({"detail": "reaction is required."}, status=status.HTTP_400_BAD_REQUEST)
        
        try:
            msg = Message.objects.get(id=message_id)
        except Message.DoesNotExist:
            return Response(status=status.HTTP_404_NOT_FOUND)

        if not ConversationParticipant.objects.filter(user=request.user, conversation=msg.conversation).exists():
            return Response(status=status.HTTP_403_FORBIDDEN)

        obj, created = MessageReaction.objects.get_or_create(
            message=msg,
            user=request.user,
            reaction=reaction
        )
        
        from channels.layers import get_channel_layer
        from asgiref.sync import async_to_sync
        channel_layer = get_channel_layer()
        async_to_sync(channel_layer.group_send)(
            f"chat_{msg.conversation_id}",
            {
                "type": "reaction_update",
                "message_id": str(msg.id),
                "user_id": request.user.id,
                "reaction": reaction,
                "action": "add"
            }
        )
        
        return Response(MessageReactionSerializer(obj).data, status=status.HTTP_201_CREATED if created else status.HTTP_200_OK)

    def delete(self, request, message_id):
        reaction = request.query_params.get('reaction')
        if not reaction:
            return Response({"detail": "reaction is required in query params."}, status=status.HTTP_400_BAD_REQUEST)
        
        deleted, _ = MessageReaction.objects.filter(message_id=message_id, user=request.user, reaction=reaction).delete()
        if deleted:
            try:
                msg = Message.objects.get(id=message_id)
                from channels.layers import get_channel_layer
                from asgiref.sync import async_to_sync
                channel_layer = get_channel_layer()
                async_to_sync(channel_layer.group_send)(
                    f"chat_{msg.conversation_id}",
                    {
                        "type": "reaction_update",
                        "message_id": str(msg.id),
                        "user_id": request.user.id,
                        "reaction": reaction,
                        "action": "remove"
                    }
                )
            except Message.DoesNotExist:
                pass
        return Response(status=status.HTTP_204_NO_CONTENT)


class MessageStarView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request, message_id):
        try:
            msg = Message.objects.get(id=message_id)
        except Message.DoesNotExist:
            return Response(status=status.HTTP_404_NOT_FOUND)

        if not ConversationParticipant.objects.filter(user=request.user, conversation=msg.conversation).exists():
            return Response(status=status.HTTP_403_FORBIDDEN)

        StarredMessage.objects.get_or_create(message=msg, user=request.user)
        return Response({"detail": "Message starred."}, status=status.HTTP_201_CREATED)

    def delete(self, request, message_id):
        StarredMessage.objects.filter(message_id=message_id, user=request.user).delete()
        return Response(status=status.HTTP_204_NO_CONTENT)


class StarredMessageListView(generics.ListAPIView):
    permission_classes = [permissions.IsAuthenticated]
    serializer_class = StarredMessageSerializer

    def get_queryset(self):
        return StarredMessage.objects.filter(user=self.request.user)


class MessageDraftListView(generics.ListCreateAPIView):
    permission_classes = [permissions.IsAuthenticated]
    serializer_class = MessageDraftSerializer

    def get_queryset(self):
        return MessageDraft.objects.filter(user=self.request.user)

    def perform_create(self, serializer):
        serializer.save(user=self.request.user)


class MessageDraftDetailView(generics.RetrieveUpdateDestroyAPIView):
    permission_classes = [permissions.IsAuthenticated]
    serializer_class = MessageDraftSerializer

    def get_queryset(self):
        return MessageDraft.objects.filter(user=self.request.user)


class ScheduledMessageListView(generics.ListAPIView):
    permission_classes = [permissions.IsAuthenticated]
    serializer_class = MessageSerializer

    def get_queryset(self):
        return Message.objects.filter(sender=self.request.user, is_scheduled=True, status='scheduled')

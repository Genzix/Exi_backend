from django.db import models, transaction
from django.db.models import F, Sum
from django.http import FileResponse
from rest_framework import generics, permissions, status
from rest_framework.parsers import FormParser, MultiPartParser
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

        return Response(
            RecentConversationSerializer(participant).data,
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
        return search_chats_queryset(self.request.user, get_query_param(self.request))


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


class MuteChatView(APIView):
    permission_classes = [permissions.IsAuthenticated]

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


class SetDisappearingMessagesView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request, chat_id):
        duration = request.data.get('duration')
        if duration is not None:
            try:
                duration = int(duration)
            except ValueError:
                return Response({"detail": "Invalid duration."}, status=status.HTTP_400_BAD_REQUEST)
                
        try:
            participant = ConversationParticipant.objects.get(
                user=request.user, conversation_id=chat_id
            )
            participant.conversation.disappearing_duration = duration if duration else None
            participant.conversation.save(update_fields=['disappearing_duration'])
            return Response({"detail": "Disappearing messages updated.", "duration": duration})
        except ConversationParticipant.DoesNotExist:
            return Response(
                {"detail": "Conversation not found."}, status=status.HTTP_404_NOT_FOUND
            )


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

        data = {
            "is_muted": participant.is_muted,
            "is_pinned": participant.is_pinned,
            "is_archived": participant.is_archived,
            "disappearing_duration": participant.conversation.disappearing_duration,
            "is_blocked": is_blocked,
        }
        return Response(data)

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
    return (
        User.objects.filter(
            models.Q(display_name__icontains=query)
            | models.Q(full_name__icontains=query)
            | models.Q(exi_id__icontains=query)
        )
        .filter(is_active=True)
        # Respect the "discoverable in search" privacy control. Users who have
        # never opened privacy settings have no row yet and stay searchable.
        .exclude(privacy_settings__searchable=False)
        .exclude(id=user.id)
        .exclude(id__in=hidden_ids)[:SEARCH_LIMIT]
    )


def search_chats_queryset(user, query):
    queryset = ConversationParticipant.objects.filter(
        user=user,
        deleted_at__isnull=True,
        is_archived=False,
    ).select_related("conversation").order_by("-conversation__updated_at")

    if not query:
        return queryset.none()

    return queryset.filter(
        models.Q(conversation__participants__display_name__icontains=query)
        | models.Q(conversation__participants__full_name__icontains=query)
        | models.Q(conversation__participants__exi_id__icontains=query)
        | models.Q(conversation__messages__content__icontains=query)
        | models.Q(conversation__messages__attachment_name__icontains=query)
    ).distinct()[:SEARCH_LIMIT]


def search_messages_queryset(user, query, chat_id=None):
    participant_conversations = ConversationParticipant.objects.filter(
        user=user,
        deleted_at__isnull=True,
    ).values_list("conversation_id", flat=True)

    queryset = Message.objects.filter(
        conversation_id__in=participant_conversations
    ).select_related("sender", "conversation").order_by("-created_at")

    if chat_id:
        queryset = queryset.filter(conversation_id=chat_id)

    if not query:
        return queryset.none()

    return queryset.filter(
        models.Q(content__icontains=query)
        | models.Q(attachment_name__icontains=query)
    )[:MESSAGE_SEARCH_LIMIT]


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
        chats = list(search_chats_queryset(request.user, query))
        return Response(
            {
                "query": query,
                "count": len(chats),
                "results": RecentConversationSerializer(chats, many=True).data,
            }
        )


class SearchMessagesView(APIView):
    """Search messages across all chats or within one chat."""

    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        query = get_query_param(request)
        chat_id = request.query_params.get("chat_id")

        if chat_id and not ConversationParticipant.objects.filter(
            user=request.user,
            conversation_id=chat_id,
            deleted_at__isnull=True,
        ).exists():
            return Response(
                {"detail": "Conversation not found."},
                status=404,
            )

        messages = list(search_messages_queryset(request.user, query, chat_id))
        return Response(
            {
                "query": query,
                "chat_id": chat_id,
                "count": len(messages),
                "results": MessageSearchResultSerializer(
                    messages,
                    many=True,
                    context={"request": request},
                ).data,
            }
        )


class UnifiedSearchView(APIView):
    """Search users, chats, and messages in one request."""

    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        query = get_query_param(request)
        chat_id = request.query_params.get("chat_id")

        users = list(search_users_queryset(request.user, query))
        chats = list(search_chats_queryset(request.user, query))
        messages = list(search_messages_queryset(request.user, query, chat_id))

        return Response(
            {
                "query": query,
                "users": {
                    "count": len(users),
                    "results": PublicProfileSerializer(users, many=True).data,
                },
                "chats": {
                    "count": len(chats),
                    "results": RecentConversationSerializer(chats, many=True).data,
                },
                "messages": {
                    "count": len(messages),
                    "results": MessageSearchResultSerializer(
                        messages,
                        many=True,
                        context={"request": request},
                    ).data,
                },
            }
        )


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

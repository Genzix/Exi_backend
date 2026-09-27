from datetime import timedelta
from django.utils import timezone
from rest_framework import permissions, status
from rest_framework.response import Response
from rest_framework.views import APIView

from .user_preferences import (
    clear_user_media,
    create_chat_backup,
    export_user_chat_history,
    get_chat_storage_breakdown,
    get_large_files,
    get_section_preferences,
    get_storage_usage,
    get_user_preferences,
    update_section_preferences,
    update_user_preferences,
)
from .utils import get_or_create_privacy_settings


# ==========================================
# 0. Unified Settings View
# ==========================================

class UnifiedSettingsView(APIView):
    """Get or update all user settings in one call (notifications, chats, appearance, storage, privacy)."""
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        user_id = request.user.id
        prefs = get_user_preferences(user_id)
        privacy = get_or_create_privacy_settings(request.user)

        from .serializers import PrivacySettingsSerializer

        return Response({
            "notifications": prefs.get("notifications", {}),
            "chats": prefs.get("chats", {}),
            "appearance": prefs.get("appearance", {}),
            "storage": prefs.get("storage", {}),
            "privacy": PrivacySettingsSerializer(privacy).data,
        })

    def patch(self, request):
        user_id = request.user.id
        data = request.data.copy() if hasattr(request.data, "copy") else dict(request.data)

        # Handle privacy if provided
        privacy_data = data.pop("privacy", None)
        if privacy_data:
            from .serializers import PrivacySettingsSerializer
            privacy = get_or_create_privacy_settings(request.user)
            p_serializer = PrivacySettingsSerializer(privacy, data=privacy_data, partial=True)
            p_serializer.is_valid(raise_exception=True)
            p_serializer.save()

        updated_prefs = update_user_preferences(user_id, data)
        privacy = get_or_create_privacy_settings(request.user)
        from .serializers import PrivacySettingsSerializer

        return Response({
            "notifications": updated_prefs.get("notifications", {}),
            "chats": updated_prefs.get("chats", {}),
            "appearance": updated_prefs.get("appearance", {}),
            "storage": updated_prefs.get("storage", {}),
            "privacy": PrivacySettingsSerializer(privacy).data,
            "detail": "Settings updated successfully."
        })

    put = patch


# ==========================================
# 1. Notifications Settings
# ==========================================

class NotificationSettingsView(APIView):
    """
    Manage Notification Settings:
    - Message Alerts, Group Alerts, Call Alerts
    - Sounds, Vibration, Preview, Mute All
    """
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        prefs = get_section_preferences(request.user.id, "notifications")
        return Response(prefs)

    def patch(self, request):
        data = request.data
        if not isinstance(data, dict):
            return Response({"detail": "Invalid data format."}, status=status.HTTP_400_BAD_REQUEST)

        # Validation for vibration options
        valid_vibrations = ["default", "short", "long", "off"]
        for vib_key in ["message_vibration", "group_vibration", "call_vibration"]:
            if vib_key in data and data[vib_key] not in valid_vibrations:
                return Response(
                    {"detail": f"{vib_key} must be one of: {', '.join(valid_vibrations)}."},
                    status=status.HTTP_400_BAD_REQUEST,
                )

        updated = update_section_preferences(request.user.id, "notifications", data)
        return Response({
            "detail": "Notification settings updated successfully.",
            "settings": updated
        })

    put = patch


class NotificationMuteView(APIView):
    """Mute or unmute all notifications with predefined duration or custom timestamp."""
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request):
        action = request.data.get("action", "mute")  # "mute" or "unmute"
        duration = request.data.get("duration")  # "8_hours", "1_week", "always", "off" or minutes (int)
        until = request.data.get("until")

        if action == "unmute" or duration == "off":
            updated = update_section_preferences(request.user.id, "notifications", {
                "mute_all": False,
                "mute_until": None,
            })
            return Response({
                "detail": "Notifications unmuted successfully.",
                "is_muted": False,
                "settings": updated,
            })

        mute_until_iso = None
        if until:
            mute_until_iso = str(until)
        elif duration == "8_hours":
            mute_until_iso = (timezone.now() + timedelta(hours=8)).isoformat()
        elif duration == "1_week":
            mute_until_iso = (timezone.now() + timedelta(days=7)).isoformat()
        elif duration == "always":
            mute_until_iso = None
        elif isinstance(duration, (int, float)):
            mute_until_iso = (timezone.now() + timedelta(minutes=int(duration))).isoformat()

        updated = update_section_preferences(request.user.id, "notifications", {
            "mute_all": True,
            "mute_until": mute_until_iso,
        })
        return Response({
            "detail": "Notifications muted successfully.",
            "is_muted": True,
            "mute_until": mute_until_iso,
            "settings": updated,
        })


# ==========================================
# 2. Chats Settings
# ==========================================

class ChatPreferencesView(APIView):
    """
    Manage Chat Settings:
    - Disappearing Messages default timer
    - Enter to Send
    - Keep Chats Archived
    - Auto Backup Frequency
    """
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        prefs = get_section_preferences(request.user.id, "chats")
        return Response(prefs)

    def patch(self, request):
        data = request.data
        if not isinstance(data, dict):
            return Response({"detail": "Invalid data format."}, status=status.HTTP_400_BAD_REQUEST)

        valid_frequencies = ["off", "daily", "weekly", "monthly"]
        if "auto_backup_frequency" in data and data["auto_backup_frequency"] not in valid_frequencies:
            return Response(
                {"detail": f"auto_backup_frequency must be one of: {', '.join(valid_frequencies)}."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        updated = update_section_preferences(request.user.id, "chats", data)
        return Response({
            "detail": "Chat settings updated successfully.",
            "settings": updated
        })

    put = patch


class ChatBackupView(APIView):
    """Get backup status or create a new manual chat backup."""
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        prefs = get_section_preferences(request.user.id, "chats")
        return Response({
            "last_backup_at": prefs.get("last_backup_at"),
            "last_backup_size": prefs.get("last_backup_size", 0),
            "last_backup_size_kb": round(prefs.get("last_backup_size", 0) / 1024, 2),
            "auto_backup_frequency": prefs.get("auto_backup_frequency", "off"),
            "backup_over_wifi_only": prefs.get("backup_over_wifi_only", True),
            "include_videos": prefs.get("include_videos", True),
        })

    def post(self, request):
        include_videos = request.data.get("include_videos", True)
        result = create_chat_backup(request.user, include_videos=bool(include_videos))
        return Response({
            "detail": "Chat backup created successfully.",
            **result["backup_info"],
            "backup_payload": result["backup_payload"],
        }, status=status.HTTP_201_CREATED)


class ChatRestoreView(APIView):
    """Restore chat messages and preferences from backup data."""
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request):
        backup_data = request.data.get("backup_payload") or request.data
        if not isinstance(backup_data, dict) or "conversations" not in backup_data:
            return Response(
                {"detail": "Invalid backup payload format. 'conversations' key required."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        # Restore settings if present
        if "settings" in backup_data and isinstance(backup_data["settings"], dict):
            update_user_preferences(request.user.id, backup_data["settings"])

        return Response({
            "detail": "Chat backup restored successfully.",
            "total_conversations_restored": len(backup_data.get("conversations", [])),
            "total_messages_restored": backup_data.get("total_messages", 0),
        })


class ChatHistoryExportView(APIView):
    """Export formatted chat history (all or specific chat)."""
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        chat_id = request.query_params.get("chat_id")
        export_data = export_user_chat_history(request.user, chat_id=chat_id)
        return Response(export_data)


class ChatHistoryClearAllView(APIView):
    """Clear message history across all user conversations."""
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request):
        from chats.models import ConversationParticipant
        count = ConversationParticipant.objects.filter(
            user=request.user,
            deleted_at__isnull=True
        ).update(cleared_at=timezone.now())

        return Response({
            "detail": "All chat histories cleared successfully.",
            "cleared_chats_count": count,
        })


class ChatHistoryDeleteAllView(APIView):
    """Delete all conversations for the user."""
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request):
        from chats.models import ConversationParticipant
        count = ConversationParticipant.objects.filter(
            user=request.user,
            deleted_at__isnull=True
        ).update(
            deleted_at=timezone.now(),
            is_pinned=False,
            is_archived=False,
        )

        return Response({
            "detail": "All chats deleted successfully.",
            "deleted_chats_count": count,
        })


class ChatArchiveAllView(APIView):
    """Archive all active chats for the user."""
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request):
        from chats.models import ConversationParticipant
        count = ConversationParticipant.objects.filter(
            user=request.user,
            deleted_at__isnull=True,
            is_archived=False,
        ).update(is_archived=True)

        return Response({
            "detail": "All active chats archived successfully.",
            "archived_count": count,
        })


class ChatUnarchiveAllView(APIView):
    """Unarchive all archived chats for the user."""
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request):
        from chats.models import ConversationParticipant
        count = ConversationParticipant.objects.filter(
            user=request.user,
            is_archived=True,
        ).update(is_archived=False)

        return Response({
            "detail": "All chats unarchived successfully.",
            "unarchived_count": count,
        })


# ==========================================
# 3. Appearance Settings
# ==========================================

class AppearanceSettingsView(APIView):
    """
    Manage Appearance Settings:
    - Light / Dark / System Theme Mode
    - Chat Theme & Accent Colors
    - Wallpaper Preset / Custom URL / Opacity
    - Font Size
    """
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        prefs = get_section_preferences(request.user.id, "appearance")
        return Response(prefs)

    def patch(self, request):
        data = request.data
        if not isinstance(data, dict):
            return Response({"detail": "Invalid data format."}, status=status.HTTP_400_BAD_REQUEST)

        valid_modes = ["light", "dark", "system"]
        theme_val = data.get("theme_mode") or data.get("theme")
        if theme_val and theme_val not in valid_modes:
            return Response(
                {"detail": f"theme_mode must be one of: {', '.join(valid_modes)}."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        valid_font_sizes = ["small", "medium", "large", "extra_large"]
        if "font_size" in data and data["font_size"] not in valid_font_sizes:
            return Response(
                {"detail": f"font_size must be one of: {', '.join(valid_font_sizes)}."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        valid_chat_themes = ["classic", "emerald", "ocean", "sunset", "midnight", "rose", "lavender", "custom"]
        if "chat_theme" in data and data["chat_theme"] not in valid_chat_themes:
            return Response(
                {"detail": f"chat_theme must be one of: {', '.join(valid_chat_themes)}."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        updated = update_section_preferences(request.user.id, "appearance", data)
        return Response({
            "detail": "Appearance settings updated successfully.",
            "settings": updated
        })

    put = patch


# ==========================================
# 4. Storage Settings & Management
# ==========================================

class StorageSettingsView(APIView):
    """
    Manage Auto-Download Preferences & Low Data Usage:
    - auto_download_mobile: [photo, audio, video, document]
    - auto_download_wifi: [photo, audio, video, document]
    - auto_download_roaming: [photo, audio, video, document]
    - low_data_usage_calls: bool
    """
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        prefs = get_section_preferences(request.user.id, "storage")
        usage_summary = get_storage_usage(request.user)
        return Response({
            "settings": prefs,
            "usage_summary": usage_summary,
        })

    def patch(self, request):
        data = request.data
        if not isinstance(data, dict):
            return Response({"detail": "Invalid data format."}, status=status.HTTP_400_BAD_REQUEST)

        updated = update_section_preferences(request.user.id, "storage", data)
        return Response({
            "detail": "Storage settings updated successfully.",
            "settings": updated
        })

    put = patch


class StorageUsageView(APIView):
    """Detailed breakdown of total storage consumed (media, attachments, breakdown by type)."""
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        usage = get_storage_usage(request.user)
        return Response(usage)


class StorageChatsView(APIView):
    """List chats ranked by storage usage with individual stats."""
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        chats_breakdown = get_chat_storage_breakdown(request.user)
        return Response({
            "total_chats": len(chats_breakdown),
            "chats": chats_breakdown,
        })


class StorageLargeFilesView(APIView):
    """List files exceeding a size threshold (default 5MB or ?min_size_mb=)."""
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        min_mb = request.query_params.get("min_size_mb", "5")
        try:
            min_mb_float = float(min_mb)
        except ValueError:
            min_mb_float = 5.0

        min_bytes = int(min_mb_float * 1024 * 1024)
        files = get_large_files(request.user, min_size_bytes=min_bytes)

        return Response({
            "threshold_mb": min_mb_float,
            "count": len(files),
            "files": files,
        })


class StorageLargeFilesDeleteView(APIView):
    """Delete selected large media or message attachments by IDs."""
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request):
        file_ids = request.data.get("file_ids") or []
        if not file_ids:
            return Response(
                {"detail": "file_ids list is required."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        from chats.models import Media, Message

        deleted_count = 0
        deleted_bytes = 0

        # Delete from Media
        medias = Media.objects.filter(id__in=file_ids, owner=request.user)
        for m in medias:
            deleted_bytes += m.file_size or 0
            if m.file:
                try:
                    m.file.delete(save=False)
                except Exception:
                    pass
            m.delete()
            deleted_count += 1

        # Clear from Message attachments
        messages = Message.objects.filter(id__in=file_ids, sender=request.user)
        for msg in messages:
            deleted_bytes += msg.attachment_size or 0
            if msg.attachment:
                try:
                    msg.attachment.delete(save=False)
                except Exception:
                    pass
                msg.attachment = None
                msg.attachment_size = 0
                msg.save(update_fields=["attachment", "attachment_size"])
                deleted_count += 1

        return Response({
            "detail": "Files deleted successfully.",
            "deleted_count": deleted_count,
            "deleted_bytes": deleted_bytes,
            "deleted_mb": round(deleted_bytes / (1024 * 1024), 2),
        })


class StorageClearMediaView(APIView):
    """Clear media for all chats or a specific chat, with filters for media types and starred messages."""
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request):
        chat_id = request.data.get("chat_id")
        media_types = request.data.get("media_types")
        keep_starred = request.data.get("keep_starred", True)
        older_than_days = request.data.get("older_than_days")

        result = clear_user_media(
            user=request.user,
            chat_id=chat_id,
            media_types=media_types,
            keep_starred=bool(keep_starred),
            older_than_days=older_than_days,
        )

        return Response({
            "detail": "Media cleared successfully.",
            **result,
        })

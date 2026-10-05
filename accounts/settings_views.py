from datetime import timedelta
from django.utils import timezone
from rest_framework import permissions, status
from rest_framework.response import Response
from rest_framework.views import APIView

from .user_preferences import (
    clear_user_media,
    create_chat_backup,
    delete_manage_storage_items,
    export_user_chat_history,
    format_file_size,
    get_chat_storage_breakdown,
    get_large_files,
    get_manage_storage_category,
    get_manage_storage_overview,
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

def to_bool(val, default=False):
    """Robustly parse boolean from json, form-data, or query parameters."""
    if val is None:
        return default
    if isinstance(val, bool):
        return val
    if isinstance(val, (int, float)):
        return bool(val)
    return str(val).lower().strip() in ("true", "1", "t", "yes")


class StorageSettingsView(APIView):
    """
    10.4 Auto-Download Preferences & Low Data Usage:
    Separate network conditions:
    - mobile_data: [photos, audio, videos, documents]
    - wifi: [photos, audio, videos, documents]
    - roaming: [photos, audio, videos, documents]
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
        if hasattr(request.data, "getlist"):
            data = {k: request.data.getlist(k) if len(request.data.getlist(k)) > 1 else request.data.get(k) for k in request.data.keys()}
        else:
            data = request.data.copy() if hasattr(request.data, "copy") else dict(request.data)
        if not isinstance(data, dict):
            return Response({"detail": "Invalid data format."}, status=status.HTTP_400_BAD_REQUEST)

        valid_categories = {"photos", "photo", "image", "videos", "video", "documents", "document", "doc", "audio", "voice"}

        # Validate network condition lists
        for net_key in ["mobile_data", "wifi", "roaming", "auto_download_mobile", "auto_download_wifi", "auto_download_roaming"]:
            if net_key in data:
                val = data[net_key]
                if isinstance(val, str):
                    val = [v.strip() for v in val.split(",") if v.strip()]
                    data[net_key] = val
                if not isinstance(val, list):
                    return Response(
                        {"detail": f"{net_key} must be a list of media types (photos, videos, documents, audio)."},
                        status=status.HTTP_400_BAD_REQUEST,
                    )
                for item in val:
                    if str(item).lower() not in valid_categories:
                        return Response(
                            {"detail": f"Invalid media type '{item}' in {net_key}. Allowed: photos, videos, documents, audio."},
                            status=status.HTTP_400_BAD_REQUEST,
                        )

        if "low_data_usage_calls" in data:
            data["low_data_usage_calls"] = to_bool(data["low_data_usage_calls"], False)

        updated = update_section_preferences(request.user.id, "storage", data)
        return Response({
            "detail": "Storage settings updated successfully.",
            "settings": updated
        })

    put = patch


class StorageUsageView(APIView):
    """
    10.1 Storage Usage:
    Returns categorized breakdown:
    Photos, Videos, Documents, Audio, Total.
    With raw bytes, item counts, and human-readable formatted sizes (e.g. 2.4 GB, 850 MB).
    """
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        usage = get_storage_usage(request.user)
        return Response(usage)


class StorageChatsView(APIView):
    """List chats ranked by storage usage with individual stats and formatted sizes."""
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        chats_breakdown = get_chat_storage_breakdown(request.user)
        return Response({
            "total_chats": len(chats_breakdown),
            "chats": chats_breakdown,
        })


class ManageStorageView(APIView):
    """
    10.2 Manage Storage Overview:
    Summarizes counts, total bytes, and formatted sizes for:
    - Large Files
    - Frequently Forwarded
    - Old Media
    - Unused Media
    """
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        days_threshold = request.query_params.get("days_threshold", "30")
        min_size_mb = request.query_params.get("min_size_mb", "5.0")
        try:
            days_threshold = int(days_threshold)
        except ValueError:
            days_threshold = 30
        try:
            min_size_mb = float(min_size_mb)
        except ValueError:
            min_size_mb = 5.0

        overview = get_manage_storage_overview(
            request.user,
            days_threshold=days_threshold,
            min_size_mb=min_size_mb
        )
        return Response(overview)


class ManageStorageCategoryView(APIView):
    """
    10.2 Manage Storage Category Listing:
    Lists items belonging to one of:
    - large_files
    - frequently_forwarded
    - old_media
    - unused_media
    Supports sorting: 'largest', 'newest', 'oldest'.
    Supports pagination: limit, offset.
    """
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        category = request.query_params.get("category", "large_files")
        sort = request.query_params.get("sort", "largest")
        limit = request.query_params.get("limit", "50")
        offset = request.query_params.get("offset", "0")
        days_threshold = request.query_params.get("days_threshold", "30")
        min_size_mb = request.query_params.get("min_size_mb", "5.0")

        try:
            limit = max(1, min(100, int(limit)))
            offset = max(0, int(offset))
            days_threshold = int(days_threshold)
            min_size_mb = float(min_size_mb)
        except ValueError:
            limit = 50
            offset = 0
            days_threshold = 30
            min_size_mb = 5.0

        data = get_manage_storage_category(
            user=request.user,
            category=category,
            sort=sort,
            limit=limit,
            offset=offset,
            days_threshold=days_threshold,
            min_size_mb=min_size_mb,
        )
        return Response(data)


class ManageStorageDeleteView(APIView):
    """
    10.2 Batch Delete Action:
    Allows:
    - Select Multiple: item_ids list
    - Select All: select_all=True with category
    CRITICAL MESSAGE RETENTION:
    Deleting downloaded media does NOT automatically delete the message
    unless delete_message=True is explicitly passed.
    """
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request):
        item_ids = request.data.get("item_ids") or []
        select_all = to_bool(request.data.get("select_all", False), False)
        category = request.data.get("category")
        delete_message = to_bool(request.data.get("delete_message", False), False)
        days_threshold = request.data.get("days_threshold", 30)
        min_size_mb = request.data.get("min_size_mb", 5.0)

        if not select_all and not item_ids:
            return Response(
                {"detail": "Either item_ids list or select_all=true with category is required."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        if select_all and not category:
            return Response(
                {"detail": "Category is required when select_all is true."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        result = delete_manage_storage_items(
            user=request.user,
            item_ids=item_ids,
            select_all=select_all,
            category=category,
            delete_message=delete_message,
            days_threshold=days_threshold,
            min_size_mb=min_size_mb,
        )

        return Response({
            "detail": "Selected items deleted successfully.",
            **result,
        })


class StorageLargeFilesView(APIView):
    """
    10.3 Large Files:
    List files exceeding size threshold (default 5MB or ?min_size_mb=).
    Supports sorting: 'largest', 'newest', 'oldest'.
    Supports media_type filter: 'photos', 'videos', 'documents', 'audio'.
    Supports pagination: page, page_size, limit, offset.
    """
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        min_mb = request.query_params.get("min_size_mb", "5.0")
        sort = request.query_params.get("sort", "largest")
        media_type = request.query_params.get("media_type")

        # Pagination parameters
        page = request.query_params.get("page")
        page_size = request.query_params.get("page_size") or request.query_params.get("limit", "50")

        try:
            min_mb_float = float(min_mb)
        except ValueError:
            min_mb_float = 5.0

        try:
            limit = max(1, min(100, int(page_size)))
        except ValueError:
            limit = 50

        if page:
            try:
                page_int = max(1, int(page))
                offset = (page_int - 1) * limit
            except ValueError:
                offset = 0
        else:
            try:
                offset = max(0, int(request.query_params.get("offset", "0")))
            except ValueError:
                offset = 0

        min_bytes = int(min_mb_float * 1024 * 1024)
        result = get_large_files(
            request.user,
            min_size_bytes=min_bytes,
            sort=sort,
            media_type=media_type,
            limit=limit,
            offset=offset,
        )

        return Response(result)


class StorageLargeFilesDeleteView(APIView):
    """
    10.3 Delete selected large media or message attachments.
    CRITICAL: delete_message=False preserves the message text and history.
    """
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request):
        file_ids = request.data.get("file_ids") or request.data.get("item_ids") or []
        delete_message = to_bool(request.data.get("delete_message", False), False)

        if not file_ids:
            return Response(
                {"detail": "file_ids list is required."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        result = delete_manage_storage_items(
            user=request.user,
            item_ids=file_ids,
            delete_message=delete_message,
        )

        return Response({
            "detail": "Files deleted successfully.",
            **result,
        })


class StorageClearMediaView(APIView):
    """
    10.5 Clear Media:
    Allows clearing:
    - clear_photos: bool
    - clear_videos: bool
    - clear_documents: bool
    - clear_audio: bool
    - media_types: list of categories
    CRITICAL MESSAGE RETENTION:
    Deleting downloaded media should NOT automatically delete the message
    unless the user explicitly chooses that (delete_message=True).
    """
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request):
        chat_id = request.data.get("chat_id")
        clear_photos = to_bool(request.data.get("clear_photos", False), False)
        clear_videos = to_bool(request.data.get("clear_videos", False), False)
        clear_documents = to_bool(request.data.get("clear_documents", False), False)
        clear_audio = to_bool(request.data.get("clear_audio", False), False)
        media_types = request.data.get("media_types")
        keep_starred = to_bool(request.data.get("keep_starred", True), True)
        older_than_days = request.data.get("older_than_days")
        delete_message = to_bool(request.data.get("delete_message", False), False)

        result = clear_user_media(
            user=request.user,
            chat_id=chat_id,
            clear_photos=clear_photos,
            clear_videos=clear_videos,
            clear_documents=clear_documents,
            clear_audio=clear_audio,
            media_types=media_types,
            keep_starred=keep_starred,
            older_than_days=older_than_days,
            delete_message=delete_message,
            dry_run=False,
        )

        return Response({
            "detail": "Media cleared successfully.",
            **result,
        })


class StorageClearMediaPreviewView(APIView):
    """
    10.5 Clear Media Preview:
    Simulates clearing media and returns estimated freed bytes, formatted sizes,
    and category breakdown without modifying any data.
    """
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request):
        chat_id = request.data.get("chat_id")
        clear_photos = to_bool(request.data.get("clear_photos", False), False)
        clear_videos = to_bool(request.data.get("clear_videos", False), False)
        clear_documents = to_bool(request.data.get("clear_documents", False), False)
        clear_audio = to_bool(request.data.get("clear_audio", False), False)
        media_types = request.data.get("media_types")
        keep_starred = to_bool(request.data.get("keep_starred", True), True)
        older_than_days = request.data.get("older_than_days")
        delete_message = to_bool(request.data.get("delete_message", False), False)

        result = clear_user_media(
            user=request.user,
            chat_id=chat_id,
            clear_photos=clear_photos,
            clear_videos=clear_videos,
            clear_documents=clear_documents,
            clear_audio=clear_audio,
            media_types=media_types,
            keep_starred=keep_starred,
            older_than_days=older_than_days,
            delete_message=delete_message,
            dry_run=True,
        )

        return Response({
            "detail": "Media clear preview calculated.",
            **result,
        })

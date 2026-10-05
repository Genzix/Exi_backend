import os
import json
import logging
from datetime import datetime, timedelta
from django.conf import settings
from django.core.cache import cache
from django.utils import timezone
from django.db.models import Sum, Q, Count

logger = logging.getLogger(__name__)

# Directory for persisting user preferences across server restarts
PREFS_DIR = os.path.join(settings.BASE_DIR, 'data', 'user_preferences')
os.makedirs(PREFS_DIR, exist_ok=True)

DEFAULT_PREFERENCES = {
    "notifications": {
        "message_alerts": True,
        "message_sound": "default",
        "message_vibration": "default",  # "default", "short", "long", "off"
        "group_alerts": True,
        "group_sound": "default",
        "group_vibration": "default",
        "mention_notifications": True,
        "reaction_notifications": True,
        "reply_notifications": True,
        "call_alerts": True,
        "missed_calls": True,
        "call_reminders": True,
        "call_ringtone": "default",
        "call_vibration": "default",
        "sounds": True,
        "vibration": True,
        "preview": True,
        "hide_content_on_lock_screen": False,
        "show_sender_name": True,
        "mute_all": False,
        "mute_until": None,  # ISO 8601 string or None
    },
    "chats": {
        "auto_backup_frequency": "off",  # "off", "daily", "weekly", "monthly"
        "backup_over_wifi_only": True,
        "include_videos": True,
        "last_backup_at": None,
        "last_backup_size": 0,
        "default_disappearing_timer": 0,  # 0 (off), 86400 (24h), 604800 (7d), 7776000 (90d)
        "apply_disappearing_to_new_chats": True,
        "enter_is_send": False,
        "enter_to_send": False,
        "keep_chats_archived": True,
        "show_link_previews": True,
        "message_reactions": True,
        "edit_messages": True,
        "forwarding_information": True,
    },
    "appearance": {
        "theme_mode": "system",  # "light", "dark", "system"
        "theme": "system",
        "chat_theme": "classic",  # "classic", "emerald", "ocean", "sunset", "midnight", "rose", "lavender", "custom"
        "accent_color": "#0084FF",
        "wallpaper": {
            "preset": "default",
            "url": "",
            "color": "#0F172A",
            "blur": 0,
            "opacity": 1.0,
        },
        "font_size": "medium",  # "small", "medium", "large", "extra_large"
    },
    "storage": {
        "mobile_data": ["photos", "audio"],
        "wifi": ["photos", "videos", "documents", "audio"],
        "roaming": [],
        "auto_download_mobile": ["photos", "audio"],
        "auto_download_wifi": ["photos", "videos", "documents", "audio"],
        "auto_download_roaming": [],
        "low_data_usage_calls": False,
        "save_photos_to_gallery": True,
        "save_videos_to_gallery": True,
        "exi_media_folder": True,
        "upload_quality": "standard", # "standard", "high"
        "download_quality": "standard", # "standard", "high"
        "use_wifi_for_large_files": True,
    },
    "privacy": {
        "last_seen": "everyone",         # "everyone", "contacts", "nobody"
        "online_status": "everyone",     # "everyone", "same_as_last_seen"
        "typing_indicator": "everyone",  # "everyone", "nobody"
        "profile_photo": "everyone",     # "everyone", "contacts", "nobody"
        "about": "everyone",             # "everyone", "contacts", "nobody"
        "status": "contacts",            # "contacts", "selected", "nobody"
        "status_selected_contacts": [],
        "restricted_users": [],          # List of user IDs that are restricted
        "location_sharing": "when_shared", # "never", "when_shared", "live"
        "add_to_groups": "everyone",       # "everyone", "contacts", "nobody"
        "group_invitations": "allow",      # "allow", "review", "block"
        "add_to_help_requests": "everyone",# "everyone", "contacts", "nobody"
        "see_exi_activity": "everyone",    # "everyone", "contacts", "nobody"
    },
    "calls": {
        "silence_unknown_callers": False,
        "show_caller_id": True,
        "call_waiting": True,
        "call_history_visibility": "show",  # "show", "hide"
        "unknown_caller_protection": False,
        "camera_preview": True,
        "background_effects": False,
        "automatically_switch_camera": False,
    },
    "exi_intelligence": {
        "exi_reminders": True,
        "exi_insights": True,
        "i_noticed": True,
        "relationship_reminders": True,
        "smart_suggestions": True,
        "smart_reminders": True,
        "important_dates": True,
        "conversation_insights": True,
        "suggested_actions": True,
        "context_memory": True,
    },
    "security": {
        "two_step_verification": False,
        "passcode_lock": False,
        "biometric_lock": False,
        "app_lock": False,
        "security_notifications": True,
        "encrypted_backup": False,
    },
    "chat_backup": {
        "automatic_backup": "off", # "daily", "weekly", "monthly", "off"
        "include_videos": False,
    },
    "safety": {
        "filter_unknown_messages": False,
        "automatically_move_suspicious_messages": True,
        "warn_about_suspicious_links": True,
        "helper_approval_required": True,
    },
    "exi_help": {
        "visibility_contacts": True,
        "visibility_nearby": True,
        "contact_me_about_request": "both", # "contacts", "nearby", "both"
        "allow_nearby_offers": True,
        "allow_contacts_offers": True,
    },
    "location": {
        "approximate_by_default": True,
        "exact_only_when_chosen": True,
    }
}


def _get_cache_key(user_id):
    return f"user_prefs:{user_id}"


def _get_file_path(user_id):
    return os.path.join(PREFS_DIR, f"{user_id}.json")


def get_user_preferences(user_id):
    """Retrieve full preferences for a user, using cache with disk fallback."""
    if not user_id:
        return json.loads(json.dumps(DEFAULT_PREFERENCES))

    cache_key = _get_cache_key(user_id)
    cached = cache.get(cache_key)
    if cached is not None and isinstance(cached, dict):
        return cached

    # Disk fallback
    file_path = _get_file_path(user_id)
    data = {}
    if os.path.exists(file_path):
        try:
            with open(file_path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception as e:
            logger.warning(f"Failed to read preferences file for user {user_id}: {e}")

    # Merge with defaults to guarantee all keys exist
    merged = json.loads(json.dumps(DEFAULT_PREFERENCES))
    for section, values in data.items():
        if section in merged and isinstance(values, dict):
            merged[section].update(values)
        else:
            merged[section] = values

    # Synchronize aliases
    if "enter_is_send" in merged["chats"] and "enter_to_send" not in data.get("chats", {}):
        merged["chats"]["enter_to_send"] = merged["chats"]["enter_is_send"]
    elif "enter_to_send" in merged["chats"]:
        merged["chats"]["enter_is_send"] = merged["chats"]["enter_to_send"]

    if "theme_mode" in merged["appearance"]:
        merged["appearance"]["theme"] = merged["appearance"]["theme_mode"]

    # Synchronize storage network aliases
    if "storage" in merged and isinstance(merged["storage"], dict):
        s_data = data.get("storage", {}) if isinstance(data.get("storage"), dict) else {}
        if "mobile_data" in s_data:
            merged["storage"]["auto_download_mobile"] = s_data["mobile_data"]
        elif "auto_download_mobile" in s_data:
            merged["storage"]["mobile_data"] = s_data["auto_download_mobile"]
        else:
            merged["storage"]["mobile_data"] = merged["storage"].get("mobile_data") or merged["storage"].get("auto_download_mobile", ["photos", "audio"])
            merged["storage"]["auto_download_mobile"] = merged["storage"]["mobile_data"]

        if "wifi" in s_data:
            merged["storage"]["auto_download_wifi"] = s_data["wifi"]
        elif "auto_download_wifi" in s_data:
            merged["storage"]["wifi"] = s_data["auto_download_wifi"]
        else:
            merged["storage"]["wifi"] = merged["storage"].get("wifi") or merged["storage"].get("auto_download_wifi", ["photos", "videos", "documents", "audio"])
            merged["storage"]["auto_download_wifi"] = merged["storage"]["wifi"]

        if "roaming" in s_data:
            merged["storage"]["auto_download_roaming"] = s_data["roaming"]
        elif "auto_download_roaming" in s_data:
            merged["storage"]["roaming"] = s_data["auto_download_roaming"]
        else:
            merged["storage"]["roaming"] = merged["storage"].get("roaming") or merged["storage"].get("auto_download_roaming", [])
            merged["storage"]["auto_download_roaming"] = merged["storage"]["roaming"]

    cache.set(cache_key, merged, timeout=86400 * 7)
    return merged


def get_section_preferences(user_id, section):
    """Get preferences for a specific section ('notifications', 'chats', 'appearance', 'storage')."""
    prefs = get_user_preferences(user_id)
    return prefs.get(section, DEFAULT_PREFERENCES.get(section, {}))


def update_user_preferences(user_id, updates):
    """Deep update user preferences and persist to cache and disk."""
    if not user_id or not isinstance(updates, dict):
        return get_user_preferences(user_id)

    current = get_user_preferences(user_id)

    for section, val in updates.items():
        if section in current and isinstance(current[section], dict) and isinstance(val, dict):
            current[section].update(val)
        else:
            current[section] = val

    # Synchronize aliases
    if "chats" in current:
        if "enter_to_send" in updates.get("chats", {}):
            current["chats"]["enter_is_send"] = current["chats"]["enter_to_send"]
        elif "enter_is_send" in updates.get("chats", {}):
            current["chats"]["enter_to_send"] = current["chats"]["enter_is_send"]

    if "appearance" in current:
        if "theme" in updates.get("appearance", {}):
            current["appearance"]["theme_mode"] = current["appearance"]["theme"]
        elif "theme_mode" in updates.get("appearance", {}):
            current["appearance"]["theme"] = current["appearance"]["theme_mode"]

    if "storage" in current and isinstance(current["storage"], dict):
        s_updates = updates.get("storage", {}) if isinstance(updates.get("storage"), dict) else {}
        if "mobile_data" in s_updates:
            current["storage"]["auto_download_mobile"] = s_updates["mobile_data"]
        elif "auto_download_mobile" in s_updates:
            current["storage"]["mobile_data"] = s_updates["auto_download_mobile"]

        if "wifi" in s_updates:
            current["storage"]["auto_download_wifi"] = s_updates["wifi"]
        elif "auto_download_wifi" in s_updates:
            current["storage"]["wifi"] = s_updates["auto_download_wifi"]

        if "roaming" in s_updates:
            current["storage"]["auto_download_roaming"] = s_updates["roaming"]
        elif "auto_download_roaming" in s_updates:
            current["storage"]["roaming"] = s_updates["auto_download_roaming"]

    # Save to cache
    cache_key = _get_cache_key(user_id)
    cache.set(cache_key, current, timeout=86400 * 7)

    # Persist to disk
    file_path = _get_file_path(user_id)
    try:
        with open(file_path, "w", encoding="utf-8") as f:
            json.dump(current, f, indent=2)
    except Exception as e:
        logger.error(f"Failed to persist preferences for user {user_id}: {e}")

    return current


def update_section_preferences(user_id, section, section_data):
    """Update a specific section's preferences."""
    if not isinstance(section_data, dict):
        return get_section_preferences(user_id, section)
    return update_user_preferences(user_id, {section: section_data}).get(section, {})


def is_notifications_muted(user_id):
    """Check if notifications are currently muted for the user."""
    prefs = get_section_preferences(user_id, "notifications")
    if not prefs.get("mute_all", False):
        return False

    mute_until = prefs.get("mute_until")
    if not mute_until:
        return True  # Muted indefinitely

    try:
        until_dt = datetime.fromisoformat(mute_until)
        if timezone.is_naive(until_dt):
            until_dt = timezone.make_aware(until_dt)
        if timezone.now() < until_dt:
            return True
        else:
            # Mute expired, update preference
            update_section_preferences(user_id, "notifications", {"mute_all": False, "mute_until": None})
            return False
    except Exception:
        return True


def should_show_preview(user_id):
    """Check whether to show message content preview in notifications."""
    prefs = get_section_preferences(user_id, "notifications")
    return bool(prefs.get("preview", True))


# ==========================================
# Storage Management Helpers (10.1 - 10.5)
# ==========================================

def format_file_size(size_in_bytes):
    """
    Format bytes into B, KB, MB, or GB (e.g. 2.4 GB, 4.8 GB, 850 MB, 320 MB, 85 MB, 42 MB, 35 MB).
    Matches user display requirements cleanly without trailing zeros.
    """
    if not size_in_bytes or size_in_bytes <= 0:
        return "0 B"
    size = float(size_in_bytes)
    if size < 1024:
        return f"{int(size)} B"
    elif size < 1024 * 1024:
        kb = round(size / 1024, 1)
        return f"{int(kb) if kb.is_integer() else kb} KB"
    elif size < 1024 * 1024 * 1024:
        mb = round(size / (1024 * 1024), 2)
        # Format cleanly: 850 MB, 320 MB, 85 MB, 42.5 MB
        if mb.is_integer():
            return f"{int(mb)} MB"
        # If one decimal is enough, keep it short
        return f"{round(mb, 1) if round(mb, 1) == mb else mb:g} MB"
    else:
        gb = round(size / (1024 * 1024 * 1024), 2)
        if gb.is_integer():
            return f"{int(gb)} GB"
        return f"{gb:g} GB"


def get_chat_title_for_user(conversation, user):
    """Generate display title for a conversation relative to a user."""
    if not conversation:
        return "Chat"
    if conversation.type == 'direct':
        other = conversation.participants.exclude(id=user.id).first()
        if other:
            return other.display_name or other.full_name or other.username or f"User {other.id}"
        return "Direct Chat"
    else:
        participants = conversation.participants.exclude(id=user.id)
        names = [p.display_name or p.full_name or p.username for p in participants[:3] if (p.display_name or p.full_name or p.username)]
        if names:
            return f"Group ({', '.join(names)})"
        return f"Group {str(conversation.id)[:8]}"


def get_storage_usage(user):
    """
    10.1 Storage Usage:
    Calculates total and categorized storage:
    Photos, Videos, Documents, Audio, Total.
    Aggregates across all active conversations (both sent and received media/attachments) and uploads.
    """
    from chats.models import Media, Message, ConversationParticipant

    user_conv_ids = list(
        ConversationParticipant.objects.filter(
            user=user, deleted_at__isnull=True
        ).values_list('conversation_id', flat=True)
    )

    conv_messages = Message.objects.filter(conversation_id__in=user_conv_ids)

    # 1. Photos (Image media + image message attachments)
    photos_media = Media.objects.filter(
        Q(messages__conversation_id__in=user_conv_ids) | Q(owner=user),
        media_type='image'
    ).distinct()
    photos_media_size = photos_media.aggregate(total=Sum('file_size'))['total'] or 0
    photos_att = conv_messages.filter(
        media__isnull=True,
        message_type='image',
        attachment__isnull=False
    ).exclude(attachment='')
    photos_att_size = photos_att.aggregate(total=Sum('attachment_size'))['total'] or 0
    photos_bytes = photos_media_size + photos_att_size
    photos_count = photos_media.count() + photos_att.count()

    # 2. Videos (Video media + video message attachments)
    videos_media = Media.objects.filter(
        Q(messages__conversation_id__in=user_conv_ids) | Q(owner=user),
        media_type='video'
    ).distinct()
    videos_media_size = videos_media.aggregate(total=Sum('file_size'))['total'] or 0
    videos_att = conv_messages.filter(
        media__isnull=True,
        message_type='video',
        attachment__isnull=False
    ).exclude(attachment='')
    videos_att_size = videos_att.aggregate(total=Sum('attachment_size'))['total'] or 0
    videos_bytes = videos_media_size + videos_att_size
    videos_count = videos_media.count() + videos_att.count()

    # 3. Documents (Document media + document message attachments)
    docs_media = Media.objects.filter(
        Q(messages__conversation_id__in=user_conv_ids) | Q(owner=user),
        media_type='document'
    ).distinct()
    docs_media_size = docs_media.aggregate(total=Sum('file_size'))['total'] or 0
    docs_att = conv_messages.filter(
        media__isnull=True,
        message_type='document',
        attachment__isnull=False
    ).exclude(attachment='')
    docs_att_size = docs_att.aggregate(total=Sum('attachment_size'))['total'] or 0
    docs_bytes = docs_media_size + docs_att_size
    docs_count = docs_media.count() + docs_att.count()

    # 4. Audio (Audio + Voice notes media and attachments)
    audio_media = Media.objects.filter(
        Q(messages__conversation_id__in=user_conv_ids) | Q(owner=user),
        media_type__in=['audio', 'voice']
    ).distinct()
    audio_media_size = audio_media.aggregate(total=Sum('file_size'))['total'] or 0
    audio_att = conv_messages.filter(
        media__isnull=True,
        message_type__in=['audio', 'voice'],
        attachment__isnull=False
    ).exclude(attachment='')
    audio_att_size = audio_att.aggregate(total=Sum('attachment_size'))['total'] or 0
    audio_bytes = audio_media_size + audio_att_size
    audio_count = audio_media.count() + audio_att.count()

    total_bytes = photos_bytes + videos_bytes + docs_bytes + audio_bytes
    total_count = photos_count + videos_count + docs_count + audio_count

    return {
        "storage_usage": {
            "photos": {
                "bytes": photos_bytes,
                "formatted": format_file_size(photos_bytes),
                "count": photos_count,
            },
            "videos": {
                "bytes": videos_bytes,
                "formatted": format_file_size(videos_bytes),
                "count": videos_count,
            },
            "documents": {
                "bytes": docs_bytes,
                "formatted": format_file_size(docs_bytes),
                "count": docs_count,
            },
            "audio": {
                "bytes": audio_bytes,
                "formatted": format_file_size(audio_bytes),
                "count": audio_count,
            },
            "total": {
                "bytes": total_bytes,
                "formatted": format_file_size(total_bytes),
                "count": total_count,
            },
        },
        "breakdown_percentages": {
            "photos": round((photos_bytes / total_bytes * 100), 2) if total_bytes else 0,
            "videos": round((videos_bytes / total_bytes * 100), 2) if total_bytes else 0,
            "documents": round((docs_bytes / total_bytes * 100), 2) if total_bytes else 0,
            "audio": round((audio_bytes / total_bytes * 100), 2) if total_bytes else 0,
        },
        # Backward compatibility aliases
        "total_bytes": total_bytes,
        "total_mb": round(total_bytes / (1024 * 1024), 2),
        "total_formatted": format_file_size(total_bytes),
        "counts": {
            "total_media_files": total_count,
            "active_chats": len(user_conv_ids),
            "total_messages": conv_messages.count(),
        },
    }


def get_chat_storage_breakdown(user):
    """Get storage usage grouped by conversation for the user."""
    from chats.models import ConversationParticipant, Message

    participants = ConversationParticipant.objects.filter(
        user=user,
        deleted_at__isnull=True
    ).select_related('conversation').prefetch_related('conversation__participants')

    results = []
    for p in participants:
        conv = p.conversation
        chat_title = get_chat_title_for_user(conv, user)
        other_user_id = None
        if conv.type == 'direct':
            other = conv.participants.exclude(id=user.id).first()
            if other:
                other_user_id = other.id

        messages = Message.objects.filter(conversation=conv)
        msg_count = messages.count()

        media_size = messages.filter(
            media__isnull=False
        ).aggregate(total=Sum('media__file_size'))['total'] or 0

        attachment_size = messages.filter(
            media__isnull=True,
            attachment__isnull=False
        ).exclude(attachment='').aggregate(total=Sum('attachment_size'))['total'] or 0

        chat_total_bytes = media_size + attachment_size

        results.append({
            "conversation_id": str(conv.id),
            "conversation_type": conv.type,
            "chat_title": chat_title,
            "other_user_id": other_user_id,
            "total_bytes": chat_total_bytes,
            "total_mb": round(chat_total_bytes / (1024 * 1024), 2),
            "formatted_size": format_file_size(chat_total_bytes),
            "messages_count": msg_count,
            "media_count": messages.filter(Q(media__isnull=False) | Q(attachment__isnull=False)).count(),
            "is_archived": p.is_archived,
            "is_pinned": p.is_pinned,
        })

    results.sort(key=lambda x: x["total_bytes"], reverse=True)
    return results


def get_manage_storage_overview(user, days_threshold=30, min_size_mb=5.0):
    """
    10.2 Manage Storage Overview:
    Summarizes counts, total bytes, and formatted sizes for:
    - Large Files
    - Frequently Forwarded
    - Old Media
    - Unused Media
    """
    from chats.models import Media, Message, ConversationParticipant

    user_conv_ids = list(
        ConversationParticipant.objects.filter(
            user=user, deleted_at__isnull=True
        ).values_list('conversation_id', flat=True)
    )

    min_size_bytes = int(min_size_mb * 1024 * 1024)
    old_cutoff = timezone.now() - timedelta(days=int(days_threshold))

    # 1. Large Files
    large_media = Media.objects.filter(
        Q(messages__conversation_id__in=user_conv_ids) | Q(owner=user),
        file_size__gte=min_size_bytes
    ).distinct()
    large_media_size = large_media.aggregate(total=Sum('file_size'))['total'] or 0
    large_att = Message.objects.filter(
        conversation_id__in=user_conv_ids,
        media__isnull=True,
        attachment__isnull=False,
        attachment_size__gte=min_size_bytes
    ).exclude(attachment='')
    large_att_size = large_att.aggregate(total=Sum('attachment_size'))['total'] or 0
    large_total_bytes = large_media_size + large_att_size
    large_count = large_media.count() + large_att.count()

    # 2. Frequently Forwarded
    ff_msgs = Message.objects.filter(
        conversation_id__in=user_conv_ids,
        message_type__in=['image', 'video', 'document', 'audio', 'voice']
    ).filter(
        Q(forwarded_from__isnull=False) | Q(forwarded_messages__isnull=False)
    ).distinct()
    ff_media_size = ff_msgs.filter(media__isnull=False).aggregate(total=Sum('media__file_size'))['total'] or 0
    ff_att_size = ff_msgs.filter(media__isnull=True, attachment__isnull=False).exclude(attachment='').aggregate(total=Sum('attachment_size'))['total'] or 0
    ff_total_bytes = ff_media_size + ff_att_size
    ff_count = ff_msgs.count()

    # 3. Old Media
    old_media = Media.objects.filter(
        Q(messages__conversation_id__in=user_conv_ids) | Q(owner=user),
        created_at__lt=old_cutoff
    ).distinct()
    old_media_size = old_media.aggregate(total=Sum('file_size'))['total'] or 0
    old_att = Message.objects.filter(
        conversation_id__in=user_conv_ids,
        media__isnull=True,
        attachment__isnull=False,
        created_at__lt=old_cutoff
    ).exclude(attachment='')
    old_att_size = old_att.aggregate(total=Sum('attachment_size'))['total'] or 0
    old_total_bytes = old_media_size + old_att_size
    old_count = old_media.count() + old_att.count()

    # 4. Unused Media
    unused_media = Media.objects.filter(
        owner=user,
        status='ready',
        messages__isnull=True
    ).distinct()
    unused_size = unused_media.aggregate(total=Sum('file_size'))['total'] or 0
    unused_count = unused_media.count()

    return {
        "categories": {
            "large_files": {
                "title": "Large Files",
                "count": large_count,
                "total_bytes": large_total_bytes,
                "formatted": format_file_size(large_total_bytes),
                "threshold_mb": float(min_size_mb),
            },
            "frequently_forwarded": {
                "title": "Frequently Forwarded",
                "count": ff_count,
                "total_bytes": ff_total_bytes,
                "formatted": format_file_size(ff_total_bytes),
            },
            "old_media": {
                "title": "Old Media",
                "count": old_count,
                "total_bytes": old_total_bytes,
                "formatted": format_file_size(old_total_bytes),
                "days_threshold": int(days_threshold),
            },
            "unused_media": {
                "title": "Unused Media",
                "count": unused_count,
                "total_bytes": unused_size,
                "formatted": format_file_size(unused_size),
            },
        }
    }


def get_manage_storage_category(user, category, sort='largest', limit=50, offset=0, days_threshold=30, min_size_mb=5.0):
    """
    10.2 Category Listing:
    Lists items belonging to:
    - 'large_files'
    - 'frequently_forwarded'
    - 'old_media'
    - 'unused_media'
    Supports sorting: 'largest', 'newest', 'oldest'.
    """
    from chats.models import Media, Message, ConversationParticipant

    user_convs = {
        p.conversation_id: p.conversation
        for p in ConversationParticipant.objects.filter(
            user=user, deleted_at__isnull=True
        ).select_related('conversation').prefetch_related('conversation__participants')
    }
    user_conv_ids = list(user_convs.keys())

    items = []
    min_size_bytes = int(min_size_mb * 1024 * 1024)
    old_cutoff = timezone.now() - timedelta(days=int(days_threshold))

    if category == 'large_files':
        medias = Media.objects.filter(
            Q(messages__conversation_id__in=user_conv_ids) | Q(owner=user),
            file_size__gte=min_size_bytes
        ).distinct()
        for m in medias:
            msg = m.messages.first()
            conv = user_convs.get(msg.conversation_id) if msg else None
            chat_title = get_chat_title_for_user(conv, user) if conv else "My Uploads"
            items.append({
                "id": str(m.id),
                "item_type": "media",
                "message_id": str(msg.id) if msg else None,
                "file_name": m.file_name,
                "file_size": m.file_size,
                "formatted_size": format_file_size(m.file_size),
                "media_type": m.media_type,
                "mime_type": m.mime_type,
                "file_url": m.file.url if m.file else None,
                "created_at": m.created_at.isoformat(),
                "conversation_id": str(msg.conversation_id) if msg else None,
                "chat_title": chat_title,
            })

        attachments = Message.objects.filter(
            conversation_id__in=user_conv_ids,
            media__isnull=True,
            attachment__isnull=False,
            attachment_size__gte=min_size_bytes
        ).exclude(attachment='')
        for a in attachments:
            conv = user_convs.get(a.conversation_id)
            chat_title = get_chat_title_for_user(conv, user) if conv else "Chat"
            items.append({
                "id": str(a.id),
                "item_type": "attachment",
                "message_id": str(a.id),
                "file_name": a.attachment_name or os.path.basename(a.attachment.name),
                "file_size": a.attachment_size or 0,
                "formatted_size": format_file_size(a.attachment_size or 0),
                "media_type": a.message_type,
                "mime_type": a.attachment_mime_type,
                "file_url": a.attachment.url if a.attachment else None,
                "created_at": a.created_at.isoformat(),
                "conversation_id": str(a.conversation_id),
                "chat_title": chat_title,
            })

    elif category == 'frequently_forwarded':
        ff_msgs = Message.objects.filter(
            conversation_id__in=user_conv_ids,
            message_type__in=['image', 'video', 'document', 'audio', 'voice']
        ).filter(
            Q(forwarded_from__isnull=False) | Q(forwarded_messages__isnull=False)
        ).select_related('media').distinct()
        for msg in ff_msgs:
            conv = user_convs.get(msg.conversation_id)
            chat_title = get_chat_title_for_user(conv, user) if conv else "Chat"
            if msg.media:
                items.append({
                    "id": str(msg.media.id),
                    "item_type": "media",
                    "message_id": str(msg.id),
                    "file_name": msg.media.file_name,
                    "file_size": msg.media.file_size,
                    "formatted_size": format_file_size(msg.media.file_size),
                    "media_type": msg.media.media_type,
                    "mime_type": msg.media.mime_type,
                    "file_url": msg.media.file.url if msg.media.file else None,
                    "created_at": msg.created_at.isoformat(),
                    "conversation_id": str(msg.conversation_id),
                    "chat_title": chat_title,
                })
            elif msg.attachment:
                items.append({
                    "id": str(msg.id),
                    "item_type": "attachment",
                    "message_id": str(msg.id),
                    "file_name": msg.attachment_name or os.path.basename(msg.attachment.name),
                    "file_size": msg.attachment_size or 0,
                    "formatted_size": format_file_size(msg.attachment_size or 0),
                    "media_type": msg.message_type,
                    "mime_type": msg.attachment_mime_type,
                    "file_url": msg.attachment.url if msg.attachment else None,
                    "created_at": msg.created_at.isoformat(),
                    "conversation_id": str(msg.conversation_id),
                    "chat_title": chat_title,
                })

    elif category == 'old_media':
        medias = Media.objects.filter(
            Q(messages__conversation_id__in=user_conv_ids) | Q(owner=user),
            created_at__lt=old_cutoff
        ).distinct()
        for m in medias:
            msg = m.messages.first()
            conv = user_convs.get(msg.conversation_id) if msg else None
            chat_title = get_chat_title_for_user(conv, user) if conv else "My Uploads"
            items.append({
                "id": str(m.id),
                "item_type": "media",
                "message_id": str(msg.id) if msg else None,
                "file_name": m.file_name,
                "file_size": m.file_size,
                "formatted_size": format_file_size(m.file_size),
                "media_type": m.media_type,
                "mime_type": m.mime_type,
                "file_url": m.file.url if m.file else None,
                "created_at": m.created_at.isoformat(),
                "conversation_id": str(msg.conversation_id) if msg else None,
                "chat_title": chat_title,
            })
        attachments = Message.objects.filter(
            conversation_id__in=user_conv_ids,
            media__isnull=True,
            attachment__isnull=False,
            created_at__lt=old_cutoff
        ).exclude(attachment='')
        for a in attachments:
            conv = user_convs.get(a.conversation_id)
            chat_title = get_chat_title_for_user(conv, user) if conv else "Chat"
            items.append({
                "id": str(a.id),
                "item_type": "attachment",
                "message_id": str(a.id),
                "file_name": a.attachment_name or os.path.basename(a.attachment.name),
                "file_size": a.attachment_size or 0,
                "formatted_size": format_file_size(a.attachment_size or 0),
                "media_type": a.message_type,
                "mime_type": a.attachment_mime_type,
                "file_url": a.attachment.url if a.attachment else None,
                "created_at": a.created_at.isoformat(),
                "conversation_id": str(a.conversation_id),
                "chat_title": chat_title,
            })

    elif category == 'unused_media':
        unused = Media.objects.filter(
            owner=user,
            status='ready',
            messages__isnull=True
        )
        for u in unused:
            items.append({
                "id": str(u.id),
                "item_type": "media",
                "message_id": None,
                "file_name": u.file_name,
                "file_size": u.file_size,
                "formatted_size": format_file_size(u.file_size),
                "media_type": u.media_type,
                "mime_type": u.mime_type,
                "file_url": u.file.url if u.file else None,
                "created_at": u.created_at.isoformat(),
                "conversation_id": None,
                "chat_title": "Unused Upload",
            })

    # Sort
    if sort == 'largest':
        items.sort(key=lambda x: x["file_size"], reverse=True)
    elif sort == 'newest':
        items.sort(key=lambda x: x["created_at"], reverse=True)
    elif sort == 'oldest':
        items.sort(key=lambda x: x["created_at"])
    else:
        items.sort(key=lambda x: x["file_size"], reverse=True)

    total_count = len(items)
    paginated_items = items[offset:offset + limit]

    return {
        "category": category,
        "total_count": total_count,
        "sort": sort,
        "items": paginated_items,
    }


def delete_manage_storage_items(user, item_ids=None, select_all=False, category=None, delete_message=False, days_threshold=30, min_size_mb=5.0):
    """
    10.2 Delete selected items or all items in a category.
    CRITICAL MESSAGE RETENTION RULE:
    If delete_message=False (default), only the downloaded/attached media file
    is deleted from disk/storage and unlinked. The Message row itself is preserved.
    If delete_message=True, the entire Message row is deleted.
    """
    from chats.models import Media, Message, ConversationParticipant

    if select_all and category:
        cat_result = get_manage_storage_category(
            user, category=category, sort='largest', limit=10000, offset=0,
            days_threshold=days_threshold, min_size_mb=min_size_mb
        )
        item_ids = [item["id"] for item in cat_result.get("items", [])]

    if not item_ids:
        return {
            "deleted_count": 0,
            "deleted_bytes": 0,
            "formatted_freed": "0 B",
            "messages_retained": 0,
            "messages_deleted": 0,
        }

    import uuid
    valid_uuids = []
    if isinstance(item_ids, (list, tuple, set)):
        for item in item_ids:
            try:
                valid_uuids.append(uuid.UUID(str(item).strip()))
            except (ValueError, AttributeError):
                pass
    elif isinstance(item_ids, str):
        for part in item_ids.strip("[]").replace('"', '').replace("'", '').split(","):
            try:
                valid_uuids.append(uuid.UUID(part.strip()))
            except (ValueError, AttributeError):
                pass

    if not valid_uuids:
        return {
            "deleted_count": 0,
            "deleted_bytes": 0,
            "formatted_freed": "0 B",
            "messages_retained": 0,
            "messages_deleted": 0,
        }

    user_conv_ids = list(
        ConversationParticipant.objects.filter(
            user=user, deleted_at__isnull=True
        ).values_list('conversation_id', flat=True)
    )

    deleted_count = 0
    deleted_bytes = 0
    messages_retained = 0
    messages_deleted = 0

    # Process Media items
    medias = Media.objects.filter(
        id__in=valid_uuids
    ).filter(
        Q(owner=user) | Q(messages__conversation_id__in=user_conv_ids)
    ).distinct()

    for m in medias:
        deleted_bytes += m.file_size or 0
        if m.file:
            try:
                m.file.delete(save=False)
            except Exception:
                pass

        linked_msgs = list(m.messages.all())
        if delete_message:
            for msg in linked_msgs:
                msg.delete()
                messages_deleted += 1
            m.delete()
        else:
            for msg in linked_msgs:
                msg.media = None
                msg.save(update_fields=['media'])
                messages_retained += 1
            m.delete()

        deleted_count += 1

    # Process Message attachments
    messages = Message.objects.filter(
        id__in=valid_uuids,
        conversation_id__in=user_conv_ids,
        attachment__isnull=False
    ).exclude(attachment='')

    for msg in messages:
        deleted_bytes += msg.attachment_size or 0
        if msg.attachment:
            try:
                msg.attachment.delete(save=False)
            except Exception:
                pass

        if delete_message:
            msg.delete()
            messages_deleted += 1
        else:
            msg.attachment = None
            msg.attachment_size = 0
            msg.attachment_name = ''
            msg.save(update_fields=['attachment', 'attachment_size', 'attachment_name'])
            messages_retained += 1

        deleted_count += 1

    return {
        "deleted_count": deleted_count,
        "deleted_bytes": deleted_bytes,
        "formatted_freed": format_file_size(deleted_bytes),
        "messages_retained": messages_retained,
        "messages_deleted": messages_deleted,
    }


def get_large_files(user, min_size_bytes=5 * 1024 * 1024, sort='largest', media_type=None, limit=50, offset=0):
    """
    10.3 Large Files:
    Finds files >= min_size_bytes.
    Supports sorting: 'largest', 'newest', 'oldest'.
    Supports media_type filter: 'photos', 'videos', 'documents', 'audio'.
    """
    from chats.models import Media, Message, ConversationParticipant

    user_convs = {
        p.conversation_id: p.conversation
        for p in ConversationParticipant.objects.filter(
            user=user, deleted_at__isnull=True
        ).select_related('conversation').prefetch_related('conversation__participants')
    }
    user_conv_ids = list(user_convs.keys())

    # Map filter
    media_filter = None
    if media_type:
        mt = media_type.lower().strip()
        if mt in ['photos', 'photo', 'image']:
            media_filter = ['image']
        elif mt in ['videos', 'video']:
            media_filter = ['video']
        elif mt in ['documents', 'document', 'doc']:
            media_filter = ['document']
        elif mt in ['audio', 'voice']:
            media_filter = ['audio', 'voice']

    items = []

    # Check Media
    medias_qs = Media.objects.filter(
        Q(owner=user) | Q(messages__conversation_id__in=user_conv_ids),
        file_size__gte=min_size_bytes
    )
    if media_filter:
        medias_qs = medias_qs.filter(media_type__in=media_filter)

    for m in medias_qs.distinct():
        msg = m.messages.first()
        conv = user_convs.get(msg.conversation_id) if msg else None
        chat_title = get_chat_title_for_user(conv, user) if conv else "My Uploads"
        items.append({
            "id": str(m.id),
            "item_type": "media",
            "message_id": str(msg.id) if msg else None,
            "file_name": m.file_name,
            "file_size": m.file_size,
            "formatted_size": format_file_size(m.file_size),
            "media_type": m.media_type,
            "mime_type": m.mime_type,
            "file_url": m.file.url if m.file else None,
            "created_at": m.created_at.isoformat(),
            "conversation_id": str(msg.conversation_id) if msg else None,
            "chat_title": chat_title,
        })

    # Check Message attachments
    attachments_qs = Message.objects.filter(
        conversation_id__in=user_conv_ids,
        media__isnull=True,
        attachment__isnull=False,
        attachment_size__gte=min_size_bytes
    ).exclude(attachment='')
    if media_filter:
        attachments_qs = attachments_qs.filter(message_type__in=media_filter)

    for a in attachments_qs:
        conv = user_convs.get(a.conversation_id)
        chat_title = get_chat_title_for_user(conv, user) if conv else "Chat"
        items.append({
            "id": str(a.id),
            "item_type": "attachment",
            "message_id": str(a.id),
            "file_name": a.attachment_name or os.path.basename(a.attachment.name),
            "file_size": a.attachment_size or 0,
            "formatted_size": format_file_size(a.attachment_size or 0),
            "media_type": a.message_type,
            "mime_type": a.attachment_mime_type,
            "file_url": a.attachment.url if a.attachment else None,
            "created_at": a.created_at.isoformat(),
            "conversation_id": str(a.conversation_id),
            "chat_title": chat_title,
        })

    # Sort
    if sort == 'largest':
        items.sort(key=lambda x: x["file_size"], reverse=True)
    elif sort == 'newest':
        items.sort(key=lambda x: x["created_at"], reverse=True)
    elif sort == 'oldest':
        items.sort(key=lambda x: x["created_at"])
    else:
        items.sort(key=lambda x: x["file_size"], reverse=True)

    total_count = len(items)
    paginated = items[offset:offset + limit]

    return {
        "count": total_count,
        "threshold_mb": round(min_size_bytes / (1024 * 1024), 2),
        "sort": sort,
        "files": paginated,
    }


def clear_user_media(
    user,
    chat_id=None,
    clear_photos=False,
    clear_videos=False,
    clear_documents=False,
    clear_audio=False,
    media_types=None,
    keep_starred=True,
    older_than_days=None,
    delete_message=False,
    dry_run=False,
):
    """
    10.5 Clear Media:
    Allows clearing:
    - Clear Photos
    - Clear Videos
    - Clear Documents
    - Clear Audio
    Or passing media_types: ['photos', 'videos', ...]
    CRITICAL MESSAGE RETENTION RULE:
    Deleting downloaded media should NOT automatically delete the message
    unless the user explicitly chooses that (delete_message=True).
    Supports dry_run=True for previewing before deleting.
    """
    from chats.models import Media, Message, StarredMessage, ConversationParticipant

    if chat_id:
        user_conv_ids = [chat_id]
    else:
        user_conv_ids = list(
            ConversationParticipant.objects.filter(
                user=user, deleted_at__isnull=True
            ).values_list('conversation_id', flat=True)
        )

    targeted_types = set()
    if clear_photos:
        targeted_types.add('image')
    if clear_videos:
        targeted_types.add('video')
    if clear_documents:
        targeted_types.add('document')
    if clear_audio:
        targeted_types.update(['audio', 'voice'])

    if media_types and isinstance(media_types, list):
        for mt in media_types:
            mt_clean = str(mt).lower().strip()
            if mt_clean in ['photos', 'photo', 'image']:
                targeted_types.add('image')
            elif mt_clean in ['videos', 'video']:
                targeted_types.add('video')
            elif mt_clean in ['documents', 'document']:
                targeted_types.add('document')
            elif mt_clean in ['audio', 'voice']:
                targeted_types.update(['audio', 'voice'])

    # If no specific filter was selected, default to clearing ALL media types
    if not targeted_types:
        targeted_types = {'image', 'video', 'document', 'audio', 'voice'}

    qs = Message.objects.filter(
        conversation_id__in=user_conv_ids,
        message_type__in=list(targeted_types)
    ).filter(
        Q(media__isnull=False) | Q(attachment__isnull=False)
    )

    starred_protected_count = 0
    if keep_starred:
        starred_ids = set(
            StarredMessage.objects.filter(user=user).values_list('message_id', flat=True)
        )
        starred_protected_count = qs.filter(id__in=starred_ids).count()
        qs = qs.exclude(id__in=starred_ids)

    if older_than_days:
        cutoff = timezone.now() - timedelta(days=int(older_than_days))
        qs = qs.filter(created_at__lt=cutoff)

    deleted_files_count = 0
    deleted_bytes = 0
    messages_retained = 0
    messages_deleted = 0

    category_breakdown = {
        "photos": {"count": 0, "bytes": 0},
        "videos": {"count": 0, "bytes": 0},
        "documents": {"count": 0, "bytes": 0},
        "audio": {"count": 0, "bytes": 0},
    }

    def _add_to_breakdown(m_type, size):
        nonlocal deleted_bytes, deleted_files_count
        deleted_bytes += size
        deleted_files_count += 1
        if m_type == 'image':
            category_breakdown['photos']['count'] += 1
            category_breakdown['photos']['bytes'] += size
        elif m_type == 'video':
            category_breakdown['videos']['count'] += 1
            category_breakdown['videos']['bytes'] += size
        elif m_type == 'document':
            category_breakdown['documents']['count'] += 1
            category_breakdown['documents']['bytes'] += size
        elif m_type in ['audio', 'voice']:
            category_breakdown['audio']['count'] += 1
            category_breakdown['audio']['bytes'] += size

    for msg in qs.select_related('media'):
        if msg.media:
            m_size = msg.media.file_size or 0
            _add_to_breakdown(msg.media.media_type, m_size)
            if not dry_run:
                if msg.media.file:
                    try:
                        msg.media.file.delete(save=False)
                    except Exception:
                        pass
                if delete_message:
                    msg.media.delete()
                    msg.delete()
                    messages_deleted += 1
                else:
                    msg.media.delete()
                    msg.media = None
                    msg.save(update_fields=['media'])
                    messages_retained += 1

        elif msg.attachment:
            att_size = msg.attachment_size or 0
            _add_to_breakdown(msg.message_type, att_size)
            if not dry_run:
                try:
                    msg.attachment.delete(save=False)
                except Exception:
                    pass
                if delete_message:
                    msg.delete()
                    messages_deleted += 1
                else:
                    msg.attachment = None
                    msg.attachment_size = 0
                    msg.attachment_name = ''
                    msg.save(update_fields=['attachment', 'attachment_size', 'attachment_name'])
                    messages_retained += 1

    formatted_breakdown = {
        cat: {
            "count": data["count"],
            "bytes": data["bytes"],
            "formatted": format_file_size(data["bytes"]),
        }
        for cat, data in category_breakdown.items()
    }

    result = {
        "deleted_files_count": deleted_files_count,
        "freed_bytes": deleted_bytes,
        "freed_formatted": format_file_size(deleted_bytes),
        "messages_retained": messages_retained if not dry_run else (deleted_files_count if not delete_message else 0),
        "messages_deleted": messages_deleted if not dry_run else (deleted_files_count if delete_message else 0),
        "starred_protected_count": starred_protected_count,
        "breakdown": formatted_breakdown,
        "dry_run": dry_run,
    }

    return result


# ==========================================
# Chat Backup & History Helpers
# ==========================================

def create_chat_backup(user, include_videos=True):
    """Create a structured backup dictionary of the user's chats, messages, and settings."""
    from chats.models import ConversationParticipant, Message

    participants = ConversationParticipant.objects.filter(
        user=user,
        deleted_at__isnull=True
    ).select_related('conversation')

    conversations_data = []
    total_messages_count = 0

    for p in participants:
        conv = p.conversation
        other_user = None
        if conv.type == 'direct':
            other = conv.participants.exclude(id=user.id).first()
            if other:
                other_user = {
                    "id": other.id,
                    "exi_id": other.exi_id,
                    "username": other.username,
                    "display_name": other.display_name,
                }

        messages = Message.objects.filter(conversation=conv).order_by('created_at')
        if not include_videos:
            messages = messages.exclude(message_type='video')

        msgs_list = []
        for m in messages:
            total_messages_count += 1
            msgs_list.append({
                "id": str(m.id),
                "sender_id": m.sender_id,
                "is_me": m.sender_id == user.id,
                "content": m.content,
                "message_type": m.message_type,
                "status": m.status,
                "created_at": m.created_at.isoformat(),
                "attachment_name": m.attachment_name,
                "attachment_size": m.attachment_size,
            })

        conversations_data.append({
            "conversation_id": str(conv.id),
            "type": conv.type,
            "other_participant": other_user,
            "is_archived": p.is_archived,
            "is_pinned": p.is_pinned,
            "is_muted": p.is_muted,
            "disappearing_duration": conv.disappearing_duration,
            "messages": msgs_list,
        })

    backup_payload = {
        "version": "1.0",
        "exported_at": timezone.now().isoformat(),
        "user": {
            "id": user.id,
            "exi_id": user.exi_id,
            "display_name": user.display_name,
            "email": user.email,
        },
        "settings": get_user_preferences(user.id),
        "conversations": conversations_data,
        "total_conversations": len(conversations_data),
        "total_messages": total_messages_count,
    }

    # Compute backup size in bytes
    backup_str = json.dumps(backup_payload)
    backup_size = len(backup_str.encode('utf-8'))

    # Update last backup info in preferences
    now_iso = timezone.now().isoformat()
    update_section_preferences(user.id, "chats", {
        "last_backup_at": now_iso,
        "last_backup_size": backup_size,
    })

    return {
        "backup_info": {
            "last_backup_at": now_iso,
            "last_backup_size": backup_size,
            "last_backup_size_kb": round(backup_size / 1024, 2),
            "total_conversations": len(conversations_data),
            "total_messages": total_messages_count,
        },
        "backup_payload": backup_payload,
    }


def export_user_chat_history(user, chat_id=None):
    """Export chat history in a format suitable for downloading or sharing."""
    from chats.models import ConversationParticipant, Message

    participants = ConversationParticipant.objects.filter(
        user=user,
        deleted_at__isnull=True
    ).select_related('conversation')

    if chat_id:
        participants = participants.filter(conversation_id=chat_id)

    exported_chats = []
    for p in participants:
        conv = p.conversation
        other_user = "Chat"
        if conv.type == 'direct':
            other = conv.participants.exclude(id=user.id).first()
            if other:
                other_user = other.display_name or other.full_name or other.username

        messages = Message.objects.filter(conversation=conv).order_by('created_at')
        msgs_list = []
        for m in messages:
            sender_name = "You" if m.sender_id == user.id else other_user
            msgs_list.append({
                "timestamp": m.created_at.strftime("%Y-%m-%d %H:%M:%S"),
                "sender": sender_name,
                "type": m.message_type,
                "content": m.content if m.content else f"[{m.message_type.upper()} ATTACHMENT]",
            })

        exported_chats.append({
            "conversation_id": str(conv.id),
            "chat_with": other_user,
            "type": conv.type,
            "message_count": len(msgs_list),
            "history": msgs_list,
        })

    return {
        "exported_at": timezone.now().isoformat(),
        "user": user.display_name or user.username,
        "chats": exported_chats,
    }

import os
import json
import hashlib
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
        "in_app_sound_effects": True,
        "group_notifications": True,
        "vibration": True,
        "preview": True,
        "hide_content_on_lock_screen": False,
        "show_sender_name": True,
        "mute_all": False,
        "mute_until": None,  # ISO 8601 string or None
    },
    "chats": {
        "theme": "system",  # "light", "dark", "system"
        "chat_wallpaper": {
            "preset": "default",
            "url": "",
            "color": "#0F172A",
            "blur": 0,
            "opacity": 1.0,
        },
        "font_size": "medium",  # "small", "medium", "large"
        "show_link_previews": True,
        "auto_download_media": "wifi_only",  # "wifi_only", "wifi_and_cellular", "never"
        "media_quality": "standard",  # "data_saver", "standard", "hd_quality"
        "read_receipts": True,
        "typing_indicator": True,
        "message_reactions": True,
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
        "cellular": ["photos", "audio"],
        "wifi": ["photos", "videos", "documents", "audio"],
        "roaming": [],
        "auto_download_mobile": ["photos", "audio"],
        "auto_download_cellular": ["photos", "audio"],
        "auto_download_wifi": ["photos", "videos", "documents", "audio"],
        "auto_download_roaming": [],
        "low_data_usage_calls": False,
        "save_photos_to_gallery": True,
        "save_videos_to_gallery": True,
        "exi_media_folder": True,
        "upload_quality": "standard",  # "standard", "high"
        "download_quality": "standard",  # "standard", "high"
        "use_wifi_for_large_files": True,
        "cache_last_cleared_at": None,
    },
    "privacy": {
        # Finding You
        "find_by_exi_id": "everyone",
        "find_by_id": "everyone",
        "find_by_phone": "everyone",
        "show_exi_id_on_profile": True,
        "message_requests": True,

        # Activity Privacy
        "last_seen": "everyone",
        "last_seen_except": [],
        "online_status": "everyone",
        "online_status_except": [],
        "typing_indicator": "everyone",
        "typing_indicator_except": [],
        "read_receipts": True,

        # Profile Privacy
        "notification_sound": "default",
        "notification_sound_audience": "everyone",
        "notification_sound_except": [],
        "notification_sound_tone": "default",
        "profile_photo": "everyone",
        "profile_photo_except": [],
        "about": "everyone",
        "about_except": [],
        "status": "contacts",
        "status_except": [],
        "status_selected_contacts": [],
        "groups": "everyone",
        "groups_except": [],
        "add_to_groups": "everyone",     # alias
        "restricted_users": [],          # List of user IDs that are restricted
        "location_sharing": "when_shared", # "never", "when_shared", "live"
        "group_invitations": "allow",      # "allow", "review", "block"
        "add_to_help_requests": "everyone",# "everyone", "contacts", "nobody"
        "see_exi_activity": "everyone",    # "everyone", "contacts", "nobody"
    },
    "calls": {
        # Call Settings (Who can call me)
        "who_can_call": "everyone",           # "everyone", "contacts", "nobody"
        "allow_calls_from": "everyone",       # alias for compatibility

        # Video Call Settings
        "video_quality": "auto",              # "auto", "1080p", "720p", "480p", "360p"
        "low_data_mode": False,               # True/False (low data mode during calls)
        "mirror_my_video": True,              # True/False (mirror front camera preview)

        # Call Management
        "call_waiting": True,                 # True/False (receive calls while in another call)
        "silence_unknown_callers": False,     # True/False (silence calls from non-contacts)

        # General Call & Video Preferences
        "show_caller_id": True,
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
        # App Security
        "app_lock": False,
        "app_lock_enabled": False,
        "app_lock_method": "pin",  # "pin", "biometrics", "both"
        "use_biometrics": False,
        "biometric_lock": False,
        "passcode_lock": False,
        "lock_after_seconds": 60,  # 0 (Immediately), 60 (1 min), 300 (5 min), 900 (15 min)
        "lock_on_background": True,

        # Two-Step Verification
        "two_step_verification": False,
        "two_step_enabled": False,
        "two_step_email": "",
        "two_step_last_changed_at": None,

        # Encryption
        "e2ee_enabled": True,

        # Security Alerts
        "show_security_notifications": True,
        "security_notifications": True,
        "new_login_alert": True,
        "security_code_change_alert": True,
        "password_change_alert": True,
        "suspicious_activity_alert": True,
        "two_step_change_alert": True,
        "alert_push": True,
        "alert_email": "",

        # Other Security Controls
        "encrypted_backup": False,
        "silence_unknown_callers": False,
        "block_unknown_callers": False,
        "unknown_caller_protection": False,
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
    },
    "group_settings": {
        "who_can_add_to_groups": "everyone",
        "who_can_add_me_to_groups": "everyone",
        "groups_except": [],
        "group_invitations": "allow",
        "allow_invite_links": True,
        "member_approval_by_default": False,
        "member_approval": False,
        "admin_controls_preset": "admins",
        "admin_permissions": {
            "edit_group_info": True,
            "send_messages": True,
            "add_other_members": True,
            "pin_messages": True,
        },
        "edit_group_info": True,
        "send_messages": True,
        "add_other_members": True,
        "pin_messages": True,
        "group_info_visibility": "everyone",
        "show_groups_on_profile": True,
    },
    "exi_card": {
        "theme": "classic",  # "classic", "dark", "neon", "obsidian", "emerald", "gold"
        "badge_title": "Verified Member",
        "show_phone": False,
        "show_email": False,
        "show_bio": True,
        "show_qr": True,
        "custom_tagline": "",
    },
    "health_card_settings": {
        "emergency_sharing_enabled": True,
        "share_blood_group": True,
        "share_allergies": True,
        "share_emergency_contacts": True,
        "share_medications": True,
        "share_token": "",
    },
    "vault_settings": {
        "vault_lock_enabled": False,
        "use_biometrics": False,
        "auto_lock_timeout": 300,
        "vault_pin_hash": "",
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
        "breakdown": {
            "photos": photos_bytes,
            "videos": videos_bytes,
            "documents": docs_bytes,
            "audio": audio_bytes,
        },
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
        "deleted_count": deleted_files_count,
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


# ===========================================================================
# CALL SETTINGS FLOW (Call Settings, Video Call Settings, Call Management)
# ===========================================================================

def _to_b(val, default=False):
    if val is None:
        return default
    if isinstance(val, dict):
        val = val.get("enabled", val.get("value"))
    if isinstance(val, bool):
        return val
    v = str(val).lower().strip()
    if v in ("true", "1", "yes", "on", "t"):
        return True
    if v in ("false", "0", "no", "off", "f"):
        return False
    return default


def normalize_video_quality(val, default="auto"):
    if not val:
        return default
    v = str(val).lower().strip().replace("-", "_").replace(" ", "_")
    if "auto" in v or "recommend" in v or "recommed" in v:
        return "auto"
    if "1080" in v or "full_hd" in v or v == "high":
        return "1080p"
    if "720" in v or "hd" in v or v == "medium":
        return "720p"
    if "480" in v or "data_saver" in v or "saver" in v or v in ("low", "360p"):
        return "480p"
    return default


VALID_CALL_VISIBILITY_OPTIONS = [
    {
        "value": "everyone",
        "label": "Everyone",
        "description": "Anyone on EXI can call you.",
    },
    {
        "value": "contacts",
        "label": "My Contacts",
        "description": "Only people in your contacts can call you.",
    },
    {
        "value": "nobody",
        "label": "Nobody",
        "description": "Nobody can call you on EXI.",
    },
]

VALID_VIDEO_QUALITY_OPTIONS = [
    {
        "value": "auto",
        "label": "Auto (Recommended)",
        "description": "Automatically adjusts video quality based on network conditions.",
    },
    {
        "value": "1080p",
        "label": "Full HD (1080p)",
        "description": "Crisp 1080p video stream for high-speed Wi-Fi or 5G connections.",
    },
    {
        "value": "720p",
        "label": "HD (720p)",
        "description": "Standard high-definition 720p video streaming.",
    },
    {
        "value": "480p",
        "label": "Data Saver (480p)",
        "description": "Low-resolution video optimized to save mobile data.",
    },
]


def get_call_settings_flow(user):
    """
    Returns complete Call Settings grouped into:
    1. Call Settings (who can call: Everyone / My Contacts / Nobody)
    2. Video Call Settings (Video Quality, Low Data Mode, Mirror My Video)
    3. Call Management (Call Waiting, Silence Unknown Callers)
    Plus legacy notification fields for backward compatibility.
    """
    calls_prefs = get_section_preferences(user.id, "calls") or {}
    storage_prefs = get_section_preferences(user.id, "storage") or {}
    notif_prefs = get_section_preferences(user.id, "notifications") or {}

    from accounts.utils import privacy_settings_or_default
    db_settings = privacy_settings_or_default(user)

    # 1. Call Settings (Who can call me)
    who_can_call = calls_prefs.get(
        "who_can_call",
        calls_prefs.get("allow_calls_from", db_settings.allow_calls_from or "everyone")
    )
    if who_can_call not in ("everyone", "contacts", "nobody"):
        who_can_call = "everyone"

    label_map = {
        "everyone": "Everyone",
        "contacts": "My Contacts",
        "nobody": "Nobody",
    }

    # 2. Video Call Settings
    video_quality = normalize_video_quality(calls_prefs.get("video_quality", "auto"))

    low_data_mode = _to_b(calls_prefs.get("low_data_mode", storage_prefs.get("low_data_usage_calls", False)))
    mirror_my_video = _to_b(calls_prefs.get("mirror_my_video", True), default=True)

    # 3. Call Management
    call_waiting = _to_b(calls_prefs.get("call_waiting", True), default=True)
    silence_unknown_callers = _to_b(calls_prefs.get("silence_unknown_callers", False), default=False)

    # Legacy notification fields
    call_alerts = bool(notif_prefs.get("call_alerts", True))
    call_ringtone = notif_prefs.get("call_ringtone", "default")
    call_vibration = notif_prefs.get("call_vibration", "default")
    incoming_call_notification = bool(notif_prefs.get("incoming_call_notification", True))
    missed_call_notification = bool(notif_prefs.get("missed_call_notification", True))

    return {
        "call_settings": {
            "title": "Call Settings",
            "who_can_call": {
                "value": who_can_call,
                "label": label_map.get(who_can_call, "Everyone"),
                "options": VALID_CALL_VISIBILITY_OPTIONS,
                "labels": label_map,
            },
        },
        "video_call_settings": {
            "title": "Video Call Settings",
            "video_quality": {
                "value": video_quality,
                "label": next((opt["label"] for opt in VALID_VIDEO_QUALITY_OPTIONS if opt["value"] == video_quality), "Auto (Recommended)"),
                "options": VALID_VIDEO_QUALITY_OPTIONS,
            },
            "low_data_mode": {
                "value": low_data_mode,
                "status": "on" if low_data_mode else "off",
                "options": ["on", "off"],
                "labels": {"on": "On", "off": "Off"},
                "description": "Use less data during voice and video calls.",
            },
            "mirror_my_video": {
                "value": mirror_my_video,
                "status": "on" if mirror_my_video else "off",
                "options": ["on", "off"],
                "labels": {"on": "On", "off": "Off"},
                "description": "Show yourself as your front camera sees you (mirrored).",
            },
        },
        "call_management": {
            "title": "Call Management",
            "call_waiting": {
                "value": call_waiting,
                "status": "on" if call_waiting else "off",
                "options": ["on", "off"],
                "labels": {"on": "On", "off": "Off"},
                "description": "Receive notification of incoming calls while on another call.",
            },
            "silence_unknown_callers": {
                "value": silence_unknown_callers,
                "status": "on" if silence_unknown_callers else "off",
                "options": ["on", "off"],
                "labels": {"on": "On", "off": "Off"},
                "description": "Calls from unknown numbers will be silenced. They will still be shown in the Calls tab.",
            },
        },
        # Flat fields for direct frontend binding and backward compatibility
        "who_can_call": who_can_call,
        "allow_calls_from": who_can_call,
        "video_quality": video_quality,
        "low_data_mode": low_data_mode,
        "mirror_my_video": mirror_my_video,
        "call_waiting": call_waiting,
        "silence_unknown_callers": silence_unknown_callers,
        "call_alerts": call_alerts,
        "call_ringtone": call_ringtone,
        "call_vibration": call_vibration,
        "incoming_call_notification": incoming_call_notification,
        "missed_call_notification": missed_call_notification,
        "low_data_usage_calls": low_data_mode,
    }


def _extract_scalar(val):
    if isinstance(val, (list, tuple)):
        return val[0] if val else None
    if isinstance(val, dict):
        return val.get("value")
    return val


def update_call_settings_flow(user, data):
    """
    Updates call settings from a partial payload, syncing with
    UserPrivacySettings, storage preferences, and security preferences.
    """
    calls_prefs = get_section_preferences(user.id, "calls") or {}
    storage_prefs = get_section_preferences(user.id, "storage") or {}
    security_prefs = get_section_preferences(user.id, "security") or {}
    notif_prefs = get_section_preferences(user.id, "notifications") or {}

    # Flatten if passed as querydict or nested
    if hasattr(data, "dict"):
        data = data.dict()

    # 1. Call Settings (who can call)
    who_raw = None
    if "who_can_call" in data:
        who_raw = data["who_can_call"]
    elif "allow_calls_from" in data:
        who_raw = data["allow_calls_from"]
    elif "call_settings" in data and isinstance(data["call_settings"], dict):
        who_raw = data["call_settings"].get("who_can_call")

    who_val = _extract_scalar(who_raw)
    if who_val is not None:
        who_str = str(who_val).lower().strip().replace(" ", "_")
        if who_str in ("my_contacts", "contacts"):
            who_val = "contacts"
        elif who_str in ("nobody", "none"):
            who_val = "nobody"
        elif who_str in ("everyone", "all", "anyone"):
            who_val = "everyone"
        else:
            who_val = who_str
        if who_val in ("everyone", "contacts", "nobody"):
            calls_prefs["who_can_call"] = who_val
            calls_prefs["allow_calls_from"] = who_val
            from accounts.utils import get_or_create_privacy_settings
            db_settings = get_or_create_privacy_settings(user)
            db_settings.allow_calls_from = who_val
            db_settings.save(update_fields=["allow_calls_from"])

    # 2. Video Call Settings
    video_data = data.get("video_call_settings") if isinstance(data.get("video_call_settings"), dict) else {}

    vq_raw = data.get("video_quality") if "video_quality" in data else video_data.get("video_quality")
    vq = _extract_scalar(vq_raw)
    if vq is not None:
        calls_prefs["video_quality"] = normalize_video_quality(vq)

    ldm_raw = None
    if "low_data_mode" in data:
        ldm_raw = data["low_data_mode"]
    elif "low_data_usage_calls" in data:
        ldm_raw = data["low_data_usage_calls"]
    elif "low_data_mode" in video_data:
        ldm_raw = video_data["low_data_mode"]

    ldm = _extract_scalar(ldm_raw)
    if ldm is not None:
        ldm_bool = _to_b(ldm)
        calls_prefs["low_data_mode"] = ldm_bool
        storage_prefs["low_data_usage_calls"] = ldm_bool

    mmv_raw = data.get("mirror_my_video") if "mirror_my_video" in data else video_data.get("mirror_my_video")
    if mmv_raw is None and "mirror_front_camera" in data:
        mmv_raw = data["mirror_front_camera"]

    mmv = _extract_scalar(mmv_raw)
    if mmv is not None:
        mmv_bool = _to_b(mmv)
        calls_prefs["mirror_my_video"] = mmv_bool

    # 3. Call Management
    mgmt_data = data.get("call_management") if isinstance(data.get("call_management"), dict) else {}

    cw_raw = data.get("call_waiting") if "call_waiting" in data else mgmt_data.get("call_waiting")
    cw = _extract_scalar(cw_raw)
    if cw is not None:
        cw_bool = _to_b(cw)
        calls_prefs["call_waiting"] = cw_bool

    suk_raw = data.get("silence_unknown_callers") if "silence_unknown_callers" in data else mgmt_data.get("silence_unknown_callers")
    suk = _extract_scalar(suk_raw)
    if suk is not None:
        suk_bool = _to_b(suk)
        calls_prefs["silence_unknown_callers"] = suk_bool
        security_prefs["silence_unknown_callers"] = suk_bool

    # Legacy notification fields
    notif_fields = ("call_alerts", "incoming_call_notification", "missed_call_notification")
    for field in notif_fields:
        if field in data:
            val = _extract_scalar(data[field])
            notif_prefs[field] = str(val).lower() in ("true", "1") if not isinstance(val, bool) else val

    if "call_ringtone" in data:
        notif_prefs["call_ringtone"] = str(_extract_scalar(data["call_ringtone"]))[:64]
    if "call_vibration" in data:
        val = str(_extract_scalar(data["call_vibration"])).lower()
        if val in ("default", "short", "long", "off"):
            notif_prefs["call_vibration"] = val

    # Persist updated sections
    update_section_preferences(user.id, "calls", calls_prefs)
    update_section_preferences(user.id, "storage", storage_prefs)
    update_section_preferences(user.id, "security", security_prefs)
    update_section_preferences(user.id, "notifications", notif_prefs)

    return get_call_settings_flow(user)


# ===========================================================================
# STORAGE & DATA FLOW (Cache & Temporary Data, Auto-download Wi-Fi & Cellular)
# ===========================================================================

TEMP_DIR = os.path.join(settings.BASE_DIR, 'data', 'temp')
os.makedirs(TEMP_DIR, exist_ok=True)

VALID_AUTO_DOWNLOAD_MEDIA = [
    {"key": "photos", "label": "Photos", "description": "Photos and stickers"},
    {"key": "audio", "label": "Audio", "description": "Voice messages and music"},
    {"key": "videos", "label": "Videos", "description": "Video clips and recordings"},
    {"key": "documents", "label": "Documents", "description": "PDFs, spreadsheets, and files"},
]


def get_cache_and_temp_usage(user):
    """
    Calculates estimated cache and temporary data size for the user.
    Aggregates files in data/temp/, media/cache/, and cache entries.
    """
    total_bytes = 0
    file_count = 0

    # Scan data/temp directory
    if os.path.exists(TEMP_DIR):
        try:
            for entry in os.scandir(TEMP_DIR):
                if entry.is_file():
                    total_bytes += entry.stat().st_size
                    file_count += 1
        except OSError:
            pass

    # Scan media/cache if present
    media_cache_dir = os.path.join(settings.MEDIA_ROOT, "cache")
    if os.path.exists(media_cache_dir):
        try:
            for entry in os.scandir(media_cache_dir):
                if entry.is_file():
                    total_bytes += entry.stat().st_size
                    file_count += 1
        except OSError:
            pass

    storage_prefs = get_section_preferences(user.id, "storage") or {}
    last_cleared = storage_prefs.get("cache_last_cleared_at")

    return {
        "cache_size_bytes": total_bytes,
        "cache_size_formatted": format_file_size(total_bytes),
        "temp_files_count": file_count,
        "last_cleared_at": last_cleared,
    }


def clear_cache_and_temp_data(user):
    """
    Clears cache and deletes temporary files for the user.
    """
    deleted_bytes = 0
    deleted_count = 0

    # 1. Clear data/temp files
    if os.path.exists(TEMP_DIR):
        try:
            for entry in os.scandir(TEMP_DIR):
                if entry.is_file():
                    sz = entry.stat().st_size
                    try:
                        os.remove(entry.path)
                        deleted_bytes += sz
                        deleted_count += 1
                    except OSError:
                        pass
        except OSError:
            pass

    # 2. Clear media/cache files
    media_cache_dir = os.path.join(settings.MEDIA_ROOT, "cache")
    if os.path.exists(media_cache_dir):
        try:
            for entry in os.scandir(media_cache_dir):
                if entry.is_file():
                    sz = entry.stat().st_size
                    try:
                        os.remove(entry.path)
                        deleted_bytes += sz
                        deleted_count += 1
                    except OSError:
                        pass
        except OSError:
            pass

    # 3. Clear Django user-specific cache keys
    cache.delete(f"user_prefs_{user.id}")
    cache.delete(f"storage_usage_{user.id}")

    # 4. Record timestamp in user preferences
    storage_prefs = get_section_preferences(user.id, "storage") or {}
    now_iso = timezone.now().isoformat()
    storage_prefs["cache_last_cleared_at"] = now_iso
    update_section_preferences(user.id, "storage", storage_prefs)

    return {
        "detail": "Cache and temporary data cleared successfully.",
        "freed_bytes": deleted_bytes,
        "freed_formatted": format_file_size(deleted_bytes),
        "files_cleared": deleted_count,
        "cleared_at": now_iso,
    }


def get_storage_data_flow(user):
    """
    Returns complete Storage & Data backend flow matching:
    - Clear Cache & Temporary Data
    - Auto-download Media on Wi-Fi
    - Auto-download on Cellular
    """
    storage_prefs = get_section_preferences(user.id, "storage") or {}
    cache_info = get_cache_and_temp_usage(user)

    # Wi-Fi auto download
    wifi_list = storage_prefs.get(
        "wifi",
        storage_prefs.get("auto_download_wifi", ["photos", "videos", "documents", "audio"])
    )
    if not isinstance(wifi_list, list):
        wifi_list = ["photos", "videos", "documents", "audio"]
    wifi_norm = [str(x).lower().strip() for x in wifi_list]

    wifi_options = []
    for item in VALID_AUTO_DOWNLOAD_MEDIA:
        k = item["key"]
        wifi_options.append({
            "key": k,
            "label": item["label"],
            "description": item["description"],
            "enabled": (k in wifi_norm or (k == "photos" and "photo" in wifi_norm)),
        })

    # Cellular (Mobile Data) auto download
    cellular_list = storage_prefs.get(
        "cellular",
        storage_prefs.get(
            "mobile_data",
            storage_prefs.get("auto_download_mobile", ["photos", "audio"])
        )
    )
    if not isinstance(cellular_list, list):
        cellular_list = ["photos", "audio"]
    cellular_norm = [str(x).lower().strip() for x in cellular_list]

    cellular_options = []
    for item in VALID_AUTO_DOWNLOAD_MEDIA:
        k = item["key"]
        cellular_options.append({
            "key": k,
            "label": item["label"],
            "description": item["description"],
            "enabled": (k in cellular_norm or (k == "photos" and "photo" in cellular_norm)),
        })

    return {
        "flow": {
            "title": "Storage & Data",
            "clear_cache_and_temp": {
                "title": "Clear Cache & Temporary Data",
                "cache_size_bytes": cache_info["cache_size_bytes"],
                "cache_size_formatted": cache_info["cache_size_formatted"],
                "temp_files_count": cache_info["temp_files_count"],
                "last_cleared_at": cache_info["last_cleared_at"],
                "can_clear": True,
                "description": "Frees up storage used by cached files, temporary media, and preview thumbnails without deleting your chat messages or saved media.",
            },
            "auto_download_wifi": {
                "title": "Auto-download Media on Wi-Fi",
                "selected": wifi_norm,
                "options": wifi_options,
                "photos": ("photos" in wifi_norm or "photo" in wifi_norm),
                "videos": ("videos" in wifi_norm or "video" in wifi_norm),
                "documents": ("documents" in wifi_norm or "document" in wifi_norm),
                "audio": ("audio" in wifi_norm or "voice" in wifi_norm),
                "description": "Select media types that will download automatically when connected to Wi-Fi.",
            },
            "auto_download_cellular": {
                "title": "Auto-download on Cellular",
                "selected": cellular_norm,
                "options": cellular_options,
                "photos": ("photos" in cellular_norm or "photo" in cellular_norm),
                "audio": ("audio" in cellular_norm or "voice" in cellular_norm),
                "videos": ("videos" in cellular_norm or "video" in cellular_norm),
                "documents": ("documents" in cellular_norm or "document" in cellular_norm),
                "description": "Select media types that will download automatically when using cellular/mobile data.",
            },
        },
        # Flat fields for direct binding
        "cache_size_bytes": cache_info["cache_size_bytes"],
        "cache_size_formatted": cache_info["cache_size_formatted"],
        "temp_files_count": cache_info["temp_files_count"],
        "auto_download_wifi": wifi_norm,
        "wifi": wifi_norm,
        "auto_download_cellular": cellular_norm,
        "cellular": cellular_norm,
        "mobile_data": cellular_norm,
        "low_data_usage_calls": storage_prefs.get("low_data_usage_calls", False),
    }


def update_storage_data_flow(user, data):
    """
    Updates Storage & Data settings (Wi-Fi, Cellular, Cache clearing).
    Supports list format, comma-separated string, or boolean dictionary toggles.
    """
    if hasattr(data, "dict"):
        data = data.dict()

    storage_prefs = get_section_preferences(user.id, "storage") or {}

    def _parse_media_list(raw_val, current_list):
        valid = {"photos", "audio", "videos", "documents"}
        alias_map = {"photo": "photos", "voice": "audio", "video": "videos", "doc": "documents", "document": "documents"}
        if isinstance(raw_val, str):
            raw_val = [x.strip() for x in raw_val.split(",") if x.strip()]
        if isinstance(raw_val, list):
            result = []
            for item in raw_val:
                norm = alias_map.get(str(item).lower().strip(), str(item).lower().strip())
                if norm in valid and norm not in result:
                    result.append(norm)
            return result
        if isinstance(raw_val, dict):
            # Boolean map e.g. {"photos": True, "videos": False}
            updated = list(current_list)
            for k, enabled in raw_val.items():
                norm = alias_map.get(str(k).lower().strip(), str(k).lower().strip())
                if norm in valid:
                    is_on = str(enabled).lower() in ("true", "1") if not isinstance(enabled, bool) else enabled
                    if is_on and norm not in updated:
                        updated.append(norm)
                    elif not is_on and norm in updated:
                        updated.remove(norm)
            return updated
        return current_list

    # 1. Wi-Fi Auto-download
    wifi_input = None
    if "auto_download_wifi" in data:
        wifi_input = data["auto_download_wifi"]
    elif "wifi" in data:
        wifi_input = data["wifi"]
    elif "flow" in data and isinstance(data["flow"], dict) and "auto_download_wifi" in data["flow"]:
        wifi_input = data["flow"]["auto_download_wifi"]

    if wifi_input is not None:
        current_wifi = storage_prefs.get("wifi", ["photos", "videos", "documents", "audio"])
        new_wifi = _parse_media_list(wifi_input, current_wifi)
        storage_prefs["wifi"] = new_wifi
        storage_prefs["auto_download_wifi"] = new_wifi

    # 2. Cellular Auto-download
    cellular_input = None
    if "auto_download_cellular" in data:
        cellular_input = data["auto_download_cellular"]
    elif "cellular" in data:
        cellular_input = data["cellular"]
    elif "mobile_data" in data:
        cellular_input = data["mobile_data"]
    elif "auto_download_mobile" in data:
        cellular_input = data["auto_download_mobile"]
    elif "flow" in data and isinstance(data["flow"], dict) and "auto_download_cellular" in data["flow"]:
        cellular_input = data["flow"]["auto_download_cellular"]

    if cellular_input is not None:
        current_cell = storage_prefs.get("cellular", storage_prefs.get("mobile_data", ["photos", "audio"]))
        new_cell = _parse_media_list(cellular_input, current_cell)
        storage_prefs["cellular"] = new_cell
        storage_prefs["mobile_data"] = new_cell
        storage_prefs["auto_download_mobile"] = new_cell
        storage_prefs["auto_download_cellular"] = new_cell

    # 3. Clear Cache if requested
    clear_cache_requested = False
    if "clear_cache" in data:
        clear_cache_requested = str(data["clear_cache"]).lower() in ("true", "1") if not isinstance(data["clear_cache"], bool) else data["clear_cache"]
    elif "clear_cache_and_temp" in data:
        clear_cache_requested = True

    cleared_info = None
    if clear_cache_requested:
        cleared_info = clear_cache_and_temp_data(user)

    # Persist
    update_section_preferences(user.id, "storage", storage_prefs)
    flow = get_storage_data_flow(user)
    if cleared_info:
        flow["cache_cleared"] = cleared_info
    return flow


# ===========================================================================
# SECURITY SETTINGS BACKEND FLOW
# 1. App Security (App Lock, Change Passcode, Use Biometrics)
# 2. Two-Step Verification (Enable Two-Step Verification)
# 3. Encryption (End-to-End Encryption)
# 4. Active Sessions (Manage Devices)
# 5. Security Alerts (Show Security Notifications)
# ===========================================================================

APP_LOCK_TIMEOUT_OPTIONS = [
    {"value": 0, "label": "Immediately"},
    {"value": 60, "label": "After 1 minute (Default)"},
    {"value": 300, "label": "After 5 minutes"},
    {"value": 900, "label": "After 15 minutes"},
    {"value": 3600, "label": "After 1 hour"},
]


def hash_passcode(pin, user_id):
    """Hash a 4-8 digit passcode with user salt."""
    return hashlib.sha256(f"exi-app-lock:{user_id}:{pin}".encode()).hexdigest()


def verify_passcode(pin, user_id):
    """Verify given passcode against stored hash."""
    prefs = get_section_preferences(user_id, "security") or {}
    stored = prefs.get("app_lock_pin_hash")
    if not stored:
        return False
    return hash_passcode(pin, user_id) == stored


def get_user_safety_number(user):
    """Generate deterministic 60-digit safety number (12 blocks of 5) for E2EE verification."""
    h = hashlib.sha256(f"exi-e2ee:{user.id}:{user.exi_id}:{getattr(user, 'date_joined', '')}".encode()).hexdigest()
    num_str = "".join(str(int(h[i:i+4], 16) % 100000).zfill(5) for i in range(0, 48, 4))
    blocks = [num_str[i:i+5] for i in range(0, 60, 5)]
    return " ".join(blocks)


def get_user_active_devices(user, current_device_id=None):
    """Retrieve active devices/sessions for user."""
    from accounts.models import UserDevice
    devices = UserDevice.objects.filter(user=user, is_active=True).order_by("-last_active")
    device_list = []
    for d in devices:
        is_current = bool(current_device_id and d.device_id == current_device_id)
        device_list.append({
            "device_id": d.device_id,
            "device_name": d.device_name or f"{d.get_platform_display()} Device",
            "platform": d.platform,
            "platform_display": d.get_platform_display(),
            "ip_address": d.ip_address or "Unknown",
            "last_active": d.last_active.isoformat() if d.last_active else None,
            "is_current_device": is_current,
        })

    # If no devices registered yet, provide default current session
    if not device_list:
        device_list.append({
            "device_id": "current-session",
            "device_name": "Current Device",
            "platform": "mobile",
            "platform_display": "Mobile",
            "ip_address": "127.0.0.1",
            "last_active": timezone.now().isoformat(),
            "is_current_device": True,
        })
    return device_list


def get_security_flow_data(user, request=None):
    """
    Returns complete Security Settings flow:
    1. App Security (App Lock, Change Passcode, Use Biometrics)
    2. Two-Step Verification (Enable Two-Step Verification)
    3. Encryption (End-to-End Encryption)
    4. Active Sessions (Manage Devices)
    5. Security Alerts (Show Security Notifications)
    """
    prefs = get_section_preferences(user.id, "security") or {}

    current_device_id = None
    if request:
        current_device_id = request.query_params.get("device_id") or request.headers.get("X-Device-Id")

    # 1. App Security
    app_lock_enabled = bool(prefs.get("app_lock_enabled", prefs.get("app_lock", False)))
    passcode_set = bool(prefs.get("app_lock_pin_hash"))
    use_biometrics = bool(prefs.get("use_biometrics", prefs.get("biometric_lock", False)))
    lock_after = prefs.get("lock_after_seconds", 60)
    lock_on_background = bool(prefs.get("lock_on_background", True))

    # 2. Two-Step Verification
    two_step_enabled = bool(prefs.get("two_step_enabled", prefs.get("two_step_verification", False)))
    two_step_pin_set = bool(prefs.get("two_step_pin_hash"))
    two_step_email = prefs.get("two_step_email", "")
    two_step_last_changed = prefs.get("two_step_last_changed_at")

    # 3. Encryption
    safety_number = get_user_safety_number(user)
    from accounts.models import DevicePreKey
    prekeys_count = DevicePreKey.objects.filter(device__user=user).count()

    # 4. Active Sessions
    active_devices = get_user_active_devices(user, current_device_id)

    # 5. Security Alerts
    show_security_notifs = bool(prefs.get("show_security_notifications", prefs.get("security_notifications", True)))
    new_login_alert = bool(prefs.get("new_login_alert", True))
    security_code_change_alert = bool(prefs.get("security_code_change_alert", True))

    return {
        "flow": {
            "title": "Security",
            "app_security": {
                "title": "App Security",
                "app_lock": {
                    "enabled": app_lock_enabled,
                    "description": "Require passcode or biometrics to unlock EXI.",
                    "lock_after_seconds": lock_after,
                    "lock_on_background": lock_on_background,
                    "timeout_options": APP_LOCK_TIMEOUT_OPTIONS,
                },
                "change_passcode": {
                    "passcode_set": passcode_set,
                    "description": "4 to 8 digit passcode to unlock EXI.",
                    "can_change": True,
                },
                "use_biometrics": {
                    "enabled": use_biometrics,
                    "description": "Unlock EXI with Face ID or Fingerprint.",
                },
            },
            "two_step_verification": {
                "title": "Two-Step Verification",
                "enabled": two_step_enabled,
                "pin_set": two_step_pin_set,
                "recovery_email": two_step_email,
                "last_changed_at": two_step_last_changed,
                "description": "For extra security, require a PIN when registering your phone number with EXI again.",
            },
            "encryption": {
                "title": "Encryption",
                "status": "enabled",
                "protocol": "Signal Protocol / Double Ratchet (Curve25519, AES-256-GCM, HMAC-SHA256)",
                "description": "Messages and calls are end-to-end encrypted. No one outside of this chat, not even EXI, can read or listen to them.",
                "safety_number": safety_number,
                "verified": True,
                "prekeys_available": prekeys_count,
            },
            "active_sessions": {
                "title": "Active Sessions",
                "subtitle": "Manage Devices",
                "device_count": len(active_devices),
                "devices": active_devices,
                "description": "Review and manage all devices where your EXI account is currently signed in.",
            },
            "security_alerts": {
                "title": "Security Alerts",
                "show_security_notifications": {
                    "enabled": show_security_notifs,
                    "description": "Get notified when a contact's security code changes, or when your account logs in on a new device.",
                },
                "new_login_alert": new_login_alert,
                "security_code_change_alert": security_code_change_alert,
            },
        },
        # Flat fields for direct binding
        "app_lock_enabled": app_lock_enabled,
        "app_lock": app_lock_enabled,
        "passcode_set": passcode_set,
        "pin_set": passcode_set,
        "use_biometrics": use_biometrics,
        "biometric_lock": use_biometrics,
        "two_step_enabled": two_step_enabled,
        "two_step_verification": two_step_enabled,
        "two_step_pin_set": two_step_pin_set,
        "e2ee_enabled": True,
        "show_security_notifications": show_security_notifs,
        "security_notifications": show_security_notifs,
        "new_login_alert": new_login_alert,
        "security_code_change_alert": security_code_change_alert,
        "active_devices_count": len(active_devices),
    }


def update_security_flow_data(user, data, request=None):
    """
    Updates security settings from a partial payload.
    Supports master flow, sub-sections, and individual toggle fields.
    """
    if hasattr(data, "dict"):
        data = data.dict()

    prefs = get_section_preferences(user.id, "security") or {}

    def _to_b(val):
        if isinstance(val, dict):
            val = val.get("enabled", val.get("value"))
        return str(val).lower() in ("true", "1") if not isinstance(val, bool) else val

    # 1. App Security
    app_data = data.get("app_security") if isinstance(data.get("app_security"), dict) else {}

    # App Lock
    app_lock_val = None
    if "app_lock" in data:
        app_lock_val = data["app_lock"]
    elif "app_lock_enabled" in data:
        app_lock_val = data["app_lock_enabled"]
    elif "app_lock" in app_data:
        app_lock_val = app_data["app_lock"]
    if app_lock_val is not None:
        b = _to_b(app_lock_val)
        prefs["app_lock_enabled"] = b
        prefs["app_lock"] = b

    # Timeout & Background
    if "lock_after_seconds" in data:
        try:
            prefs["lock_after_seconds"] = max(0, int(data["lock_after_seconds"]))
        except (ValueError, TypeError):
            pass
    if "lock_on_background" in data:
        prefs["lock_on_background"] = _to_b(data["lock_on_background"])

    # Passcode (set or change)
    passcode_val = data.get("passcode") or data.get("pin") or data.get("new_passcode") or data.get("new_pin")
    if passcode_val:
        p_str = str(passcode_val).strip()
        if p_str.isdigit() and (4 <= len(p_str) <= 8):
            current_p = data.get("current_passcode") or data.get("current_pin")
            existing_hash = prefs.get("app_lock_pin_hash")
            if existing_hash and current_p:
                if hash_passcode(str(current_p).strip(), user.id) != existing_hash:
                    raise ValueError("Incorrect current passcode.")
            prefs["app_lock_pin_hash"] = hash_passcode(p_str, user.id)
            prefs["app_lock_enabled"] = True
            prefs["app_lock"] = True
            prefs["passcode_last_changed_at"] = timezone.now().isoformat()

    # Use Biometrics
    bio_val = None
    if "use_biometrics" in data:
        bio_val = data["use_biometrics"]
    elif "biometric_lock" in data:
        bio_val = data["biometric_lock"]
    elif "use_biometrics" in app_data:
        bio_val = app_data["use_biometrics"]
    if bio_val is not None:
        b = _to_b(bio_val)
        prefs["use_biometrics"] = b
        prefs["biometric_lock"] = b
        if b:
            prefs["app_lock_method"] = "biometrics" if not prefs.get("app_lock_pin_hash") else "both"
        else:
            prefs["app_lock_method"] = "pin"

    # 2. Two-Step Verification
    two_step_data = data.get("two_step_verification") if isinstance(data.get("two_step_verification"), dict) else {}
    two_step_val = None
    if "two_step_enabled" in data:
        two_step_val = data["two_step_enabled"]
    elif "two_step_verification" in data:
        two_step_val = data["two_step_verification"]
    elif "enabled" in two_step_data:
        two_step_val = two_step_data["enabled"]
    if two_step_val is not None:
        b = _to_b(two_step_val)
        prefs["two_step_enabled"] = b
        prefs["two_step_verification"] = b

    two_step_pin = data.get("two_step_pin") or two_step_data.get("pin")
    if two_step_pin:
        tsp = str(two_step_pin).strip()
        if tsp.isdigit() and (4 <= len(tsp) <= 8):
            prefs["two_step_pin_hash"] = hashlib.sha256(f"2sv:{user.id}:{tsp}".encode()).hexdigest()
            prefs["two_step_enabled"] = True
            prefs["two_step_verification"] = True
            prefs["two_step_last_changed_at"] = timezone.now().isoformat()

    if "two_step_email" in data:
        prefs["two_step_email"] = str(data["two_step_email"]).strip()[:254]

    # 5. Security Alerts
    alerts_data = data.get("security_alerts") if isinstance(data.get("security_alerts"), dict) else {}
    sec_notif_val = None
    if "show_security_notifications" in data:
        sec_notif_val = data["show_security_notifications"]
    elif "security_notifications" in data:
        sec_notif_val = data["security_notifications"]
    elif "show_security_notifications" in alerts_data:
        sec_notif_val = alerts_data["show_security_notifications"]
    if sec_notif_val is not None:
        b = _to_b(sec_notif_val)
        prefs["show_security_notifications"] = b
        prefs["security_notifications"] = b

    if "new_login_alert" in data or "new_login_alert" in alerts_data:
        val = data.get("new_login_alert", alerts_data.get("new_login_alert"))
        prefs["new_login_alert"] = _to_b(val)

    if "security_code_change_alert" in data or "security_code_change_alert" in alerts_data:
        val = data.get("security_code_change_alert", alerts_data.get("security_code_change_alert"))
        prefs["security_code_change_alert"] = _to_b(val)

    # Persist
    update_section_preferences(user.id, "security", prefs)
    return get_security_flow_data(user, request)


# ==============================================================================
# GROUP SETTINGS FLOW & PRIVACY MANAGEMENT
# ==============================================================================

def _to_b_bool(val, default=False):
    if val is None:
        return default
    if isinstance(val, dict):
        val = val.get("enabled", val.get("value"))
    if isinstance(val, bool):
        return val
    v = str(val).lower().strip()
    if v in ("true", "1", "yes", "on", "t"):
        return True
    if v in ("false", "0", "no", "off", "f"):
        return False
    return default


INVITATIONS_DIR = os.path.join(settings.BASE_DIR, 'data', 'group_invitations')
os.makedirs(INVITATIONS_DIR, exist_ok=True)


def _get_invitations_cache_key(user_id):
    return f"group_invitations:{user_id}"


def _get_invitations_file(user_id):
    return os.path.join(INVITATIONS_DIR, f"{user_id}.json")


def get_user_group_invitations(user_id, status_filter="pending"):
    """
    Retrieve group invitations sent to a user.
    `status_filter`: "pending", "accepted", "declined", or None (for all).
    """
    if not user_id:
        return []

    cache_key = _get_invitations_cache_key(user_id)
    cached = cache.get(cache_key)
    if cached is not None and isinstance(cached, list):
        invs = cached
    else:
        fpath = _get_invitations_file(user_id)
        if os.path.exists(fpath):
            try:
                with open(fpath, "r", encoding="utf-8") as f:
                    invs = json.load(f)
                cache.set(cache_key, invs, timeout=3600)
            except Exception:
                invs = []
        else:
            invs = []

    if status_filter:
        return [inv for inv in invs if inv.get("status") == status_filter]
    return invs


def _save_user_invitations(user_id, invitations):
    """Save user group invitations to cache and disk."""
    cache_key = _get_invitations_cache_key(user_id)
    cache.set(cache_key, invitations, timeout=3600)
    try:
        fpath = _get_invitations_file(user_id)
        with open(fpath, "w", encoding="utf-8") as f:
            json.dump(invitations, f, indent=2)
    except Exception as e:
        logger.error(f"Failed to save group invitations for user {user_id}: {e}")


def create_group_invitation(group_id, inviter, target_user):
    """
    Creates and queues a group invitation for target_user.
    Avoids duplicate pending invitations for the same group.
    """
    import uuid
    from chats.group_views import _get_group_meta

    gid = str(group_id)
    meta = _get_group_meta(gid) or {}
    group_name = meta.get("name", "Group Chat")
    group_icon = meta.get("icon", "")
    invite_code = meta.get("invite_code", "")

    invitations = get_user_group_invitations(target_user.id, status_filter=None)
    for inv in invitations:
        if inv.get("group_id") == gid and inv.get("status") == "pending":
            return inv

    invitation = {
        "invitation_id": str(uuid.uuid4()),
        "group_id": gid,
        "group_name": group_name,
        "group_icon": group_icon,
        "inviter_id": inviter.id,
        "inviter_name": getattr(inviter, "display_name", None) or getattr(inviter, "full_name", None) or inviter.username,
        "invite_code": invite_code,
        "status": "pending",
        "created_at": timezone.now().isoformat(),
        "responded_at": None,
    }
    invitations.insert(0, invitation)
    _save_user_invitations(target_user.id, invitations)
    return invitation


def accept_group_invitation(user, invitation_id):
    """
    Accepts a pending group invitation and adds user to the group.
    Returns: (success: bool, detail: str, group_id: str or None)
    """
    from chats.models import Conversation, ConversationParticipant

    invitations = get_user_group_invitations(user.id, status_filter=None)
    target_inv = None
    for inv in invitations:
        if inv.get("invitation_id") == str(invitation_id):
            target_inv = inv
            break

    if not target_inv:
        return False, "Invitation not found.", None
    if target_inv.get("status") != "pending":
        return False, f"Invitation has already been {target_inv.get('status')}.", target_inv.get("group_id")

    group_id = target_inv.get("group_id")
    try:
        conv = Conversation.objects.get(id=group_id, type="group")
    except Conversation.DoesNotExist:
        target_inv["status"] = "expired"
        _save_user_invitations(user.id, invitations)
        return False, "Group no longer exists.", None

    p, created = ConversationParticipant.objects.get_or_create(
        conversation=conv,
        user=user,
        defaults={"deleted_at": None},
    )
    if not created and p.deleted_at is not None:
        p.deleted_at = None
        p.save(update_fields=["deleted_at"])

    target_inv["status"] = "accepted"
    target_inv["responded_at"] = timezone.now().isoformat()
    _save_user_invitations(user.id, invitations)
    return True, "Successfully joined group.", group_id


def decline_group_invitation(user, invitation_id):
    """
    Declines a pending group invitation.
    Returns: (success: bool, detail: str)
    """
    invitations = get_user_group_invitations(user.id, status_filter=None)
    target_inv = None
    for inv in invitations:
        if inv.get("invitation_id") == str(invitation_id):
            target_inv = inv
            break

    if not target_inv:
        return False, "Invitation not found."

    target_inv["status"] = "declined"
    target_inv["responded_at"] = timezone.now().isoformat()
    _save_user_invitations(user.id, invitations)
    return True, "Invitation declined."


def can_user_add_to_group(adder, target_user):
    """
    Checks if `adder` is allowed to add `target_user` directly to a group chat.
    Reads target_user's group settings / privacy preferences:
    - 'everyone': anyone can add
    - 'contacts': only contacts can add (via are_contacts)
    - 'nobody': nobody can add directly (must send invite)
    Returns: (is_allowed: bool, reason: str or None)
    """
    if adder.id == target_user.id:
        return True, None

    g_prefs = get_section_preferences(target_user.id, "group_settings") or {}
    p_prefs = get_section_preferences(target_user.id, "privacy") or {}
    who = g_prefs.get(
        "who_can_add_to_groups",
        g_prefs.get(
            "who_can_add_me_to_groups",
            p_prefs.get("groups", p_prefs.get("add_to_groups", "everyone"))
        )
    )
    who = str(who).lower().strip() if who else "everyone"

    if who in ("my_contacts_except", "contacts_except", "except"):
        who = "contacts_except"

    if who == "everyone":
        return True, None
    elif who == "nobody":
        return False, f"{target_user.username}'s privacy settings do not allow anyone to add them to groups directly."
    elif who in ("contacts_except", "my_contacts_except"):
        from accounts.utils import are_contacts
        if not are_contacts(target_user, adder):
            return False, f"{target_user.username}'s privacy settings only allow contacts to add them to groups."
        groups_except = g_prefs.get("groups_except", g_prefs.get("who_can_add_except", p_prefs.get("groups_except", [])))
        excluded_strs = {str(x).strip() for x in groups_except}
        if str(adder.id) in excluded_strs or (getattr(adder, 'phone', None) and str(adder.phone) in excluded_strs):
            return False, f"{target_user.username}'s privacy settings do not allow you to add them to groups."
        return True, None
    elif who == "contacts":
        from accounts.utils import are_contacts
        if are_contacts(target_user, adder):
            return True, None
        return False, f"{target_user.username}'s privacy settings only allow contacts to add them to groups."
    return True, None


def get_user_recent_groups(user, request=None, search=None, role=None, limit=20):
    """
    Retrieves recent group conversations the user is a member of,
    complete with role, member count, unread count, flags, and last message info.
    """
    from chats.models import ConversationParticipant, Message
    from chats.group_views import _get_group_meta

    participants = ConversationParticipant.objects.filter(
        user=user,
        deleted_at__isnull=True,
        conversation__type="group"
    ).select_related("conversation")

    groups_data = []
    for p in participants:
        conv = p.conversation
        meta = _get_group_meta(conv.id) or {}
        name = meta.get("name", f"Group {str(conv.id)[:6]}")
        desc = meta.get("description", "")
        icon = meta.get("icon", "")
        owner_id = meta.get("owner_id")
        admin_ids = meta.get("admin_ids") or []

        if user.id == owner_id:
            user_role = "owner"
        elif user.id in admin_ids:
            user_role = "admin"
        else:
            user_role = "member"

        if role and role.lower() != user_role:
            continue

        if search:
            s = search.lower()
            if s not in name.lower() and s not in desc.lower():
                continue

        member_count = ConversationParticipant.objects.filter(
            conversation=conv, deleted_at__isnull=True
        ).count()

        last_msg = Message.objects.filter(conversation=conv).order_by("-created_at").select_related("sender").first()
        last_msg_data = None
        sort_time = p.joined_at
        if last_msg:
            sort_time = last_msg.created_at
            sender_name = getattr(last_msg.sender, "display_name", None) or getattr(last_msg.sender, "full_name", None) or getattr(last_msg.sender, "username", "Unknown")
            last_msg_data = {
                "id": last_msg.id,
                "sender_id": last_msg.sender_id,
                "sender_name": sender_name,
                "content": last_msg.content[:80] if last_msg.content else "",
                "message_type": last_msg.message_type,
                "created_at": last_msg.created_at.isoformat(),
            }

        groups_data.append({
            "group_id": str(conv.id),
            "name": name,
            "description": desc,
            "icon": icon,
            "role": user_role,
            "member_count": member_count,
            "unread_count": p.unread_count,
            "is_pinned": p.is_pinned,
            "is_muted": p.is_muted,
            "is_archived": p.is_archived,
            "require_approval": meta.get("require_approval", False),
            "info_visibility": meta.get("info_visibility", "everyone"),
            "invite_enabled": meta.get("invite_enabled", True),
            "last_message": last_msg_data,
            "joined_at": p.joined_at.isoformat() if p.joined_at else None,
            "updated_at": conv.updated_at.isoformat() if conv.updated_at else None,
            "_sort_key": (1 if p.is_pinned else 0, sort_time.isoformat() if sort_time else ""),
        })

    groups_data.sort(key=lambda x: x["_sort_key"], reverse=True)
    for g in groups_data:
        g.pop("_sort_key", None)

    if limit and isinstance(limit, int) and limit > 0:
        groups_data = groups_data[:limit]

    return groups_data


VALID_GROUP_INVITATION_OPTIONS = [
    {
        "value": "require_approval",
        "label": "Require approval",
        "description": "Every invite waits for your yes",
        "alias": "review",
    },
    {
        "value": "direct_invitations_only",
        "label": "Direct invitations only",
        "description": "Only people who invite you by name",
        "alias": "block",
    },
    {
        "value": "open_invitation_links_allowed",
        "label": "Open invitation links allowed",
        "description": "Anyone with a link can add you",
        "alias": "allow",
    },
]

VALID_GROUP_INFO_VISIBILITY_OPTIONS = [
    {
        "value": "everyone",
        "label": "Everyone",
        "description": "Anyone on Exi",
    },
    {
        "value": "contacts",
        "label": "My Contacts",
        "description": "People in your address book",
    },
    {
        "value": "nobody",
        "label": "Nobody",
        "description": "Hidden from everyone",
    },
]

VALID_WHO_CAN_ADD_OPTIONS = [
    {
        "value": "everyone",
        "label": "Everyone",
        "description": "Anyone on Exi can add you directly to groups",
    },
    {
        "value": "contacts",
        "label": "My Contacts",
        "description": "People in your address book can add you directly to groups",
    },
    {
        "value": "nobody",
        "label": "Nobody",
        "description": "Nobody can add you directly; they must send an invitation",
    },
]


def get_group_settings_flow(user, request=None):
    """
    Returns complete Group Settings backend flow matching UI screens:
    1. Who can add me to groups: Everyone / My Contacts / Nobody
    2. Group Privacy:
       - Group Invitations (Require approval / Direct invitations only / Open invitation links allowed)
       - Member Approval (switch on/off: Admins must approve new members)
       - Admin Controls / Permissions (Edit Group Info, Send Messages, Add Other Members, Pin Messages)
       - Group Info Visibility (Everyone / My Contacts / Nobody)
       - Recent Groups
    """
    g_prefs = get_section_preferences(user.id, "group_settings") or {}
    p_prefs = get_section_preferences(user.id, "privacy") or {}

    who = g_prefs.get(
        "who_can_add_to_groups",
        g_prefs.get(
            "who_can_add_me_to_groups",
            p_prefs.get("groups", p_prefs.get("add_to_groups", "everyone"))
        )
    )
    who_str = str(who).lower().strip() if who else "everyone"
    if who_str in ("my_contacts_except", "contacts_except", "except"):
        who = "contacts_except"
    elif who_str in ("my_contacts", "contacts"):
        who = "contacts"
    elif who_str in ("nobody", "none"):
        who = "nobody"
    else:
        who = "everyone"

    groups_except = g_prefs.get("groups_except", g_prefs.get("who_can_add_except", p_prefs.get("groups_except", [])))

    invitations_mode = g_prefs.get(
        "group_invitations",
        p_prefs.get("group_invitations", "allow")
    )
    if invitations_mode not in ("allow", "review", "block", "require_approval", "direct_invitations_only", "open_invitation_links_allowed"):
        invitations_mode = "allow"

    allow_invite_links = _to_b_bool(g_prefs.get("allow_invite_links", True))
    member_approval_by_default = _to_b_bool(
        g_prefs.get("member_approval_by_default", g_prefs.get("member_approval", False)),
        default=False
    )
    admin_preset = g_prefs.get("admin_controls_preset", "admins")
    if admin_preset not in ("admins", "all_members"):
        admin_preset = "admins"

    info_vis = g_prefs.get("group_info_visibility", "everyone")
    if info_vis not in ("everyone", "contacts", "nobody"):
        info_vis = "everyone"

    show_profile = _to_b_bool(g_prefs.get("show_groups_on_profile", True))

    invitations = get_user_group_invitations(user.id, status_filter="pending")
    recent_groups = get_user_recent_groups(user, request=request, limit=5)

    who_labels = {
        "everyone": "Everyone",
        "contacts": "My Contacts",
        "contacts_except": "My contacts except",
        "nobody": "Nobody"
    }

    invitation_labels = {
        "allow": "Open invitation links allowed",
        "open_invitation_links_allowed": "Open invitation links allowed",
        "review": "Require approval",
        "require_approval": "Require approval",
        "block": "Direct invitations only",
        "direct_invitations_only": "Direct invitations only",
    }

    preset_labels = {
        "admins": "Admins Only",
        "all_members": "All Members"
    }

    info_vis_labels = {
        "everyone": "Everyone",
        "contacts": "My Contacts",
        "nobody": "Nobody"
    }

    # 4 Granular Admin Permissions from UI screenshot
    admin_perms = g_prefs.get("admin_permissions") or {}
    edit_group_info = _to_b_bool(admin_perms.get("edit_group_info", g_prefs.get("edit_group_info", True)), default=True)
    send_messages = _to_b_bool(admin_perms.get("send_messages", g_prefs.get("send_messages", True)), default=True)
    add_other_members = _to_b_bool(admin_perms.get("add_other_members", admin_perms.get("add_members", g_prefs.get("add_other_members", True))), default=True)
    pin_messages = _to_b_bool(admin_perms.get("pin_messages", g_prefs.get("pin_messages", True)), default=True)

    active_perms = [edit_group_info, send_messages, add_other_members, pin_messages]
    active_count = sum(1 for p in active_perms if p)
    status_label = "All permissions on" if active_count == 4 else f"{active_count} of 4 permissions on" if active_count > 0 else "All permissions off"

    admin_controls_data = {
        "title": "Admin Controls",
        "sheet_title": "Admin Permissions",
        "subtitle": status_label,
        "status_label": status_label,
        "description": "Choose the default permissions group administrators hold.",
        "permissions": {
            "edit_group_info": {
                "key": "edit_group_info",
                "label": "Edit Group Info",
                "description": "Change subject, icon, and description",
                "value": edit_group_info,
                "status": "on" if edit_group_info else "off",
                "options": ["on", "off"],
            },
            "send_messages": {
                "key": "send_messages",
                "label": "Send Messages",
                "description": "Allow posting into announcement-only groups",
                "value": send_messages,
                "status": "on" if send_messages else "off",
                "options": ["on", "off"],
            },
            "add_other_members": {
                "key": "add_other_members",
                "label": "Add Other Members",
                "description": "Invite and add new contacts",
                "value": add_other_members,
                "status": "on" if add_other_members else "off",
                "options": ["on", "off"],
            },
            "pin_messages": {
                "key": "pin_messages",
                "label": "Pin Messages",
                "description": "Highlight critical updates to the header",
                "value": pin_messages,
                "status": "on" if pin_messages else "off",
                "options": ["on", "off"],
            },
        },
        "permissions_list": [
            {
                "key": "edit_group_info",
                "label": "Edit Group Info",
                "description": "Change subject, icon, and description",
                "value": edit_group_info,
                "status": "on" if edit_group_info else "off",
            },
            {
                "key": "send_messages",
                "label": "Send Messages",
                "description": "Allow posting into announcement-only groups",
                "value": send_messages,
                "status": "on" if send_messages else "off",
            },
            {
                "key": "add_other_members",
                "label": "Add Other Members",
                "description": "Invite and add new contacts",
                "value": add_other_members,
                "status": "on" if add_other_members else "off",
            },
            {
                "key": "pin_messages",
                "label": "Pin Messages",
                "description": "Highlight critical updates to the header",
                "value": pin_messages,
                "status": "on" if pin_messages else "off",
            },
        ],
        "edit_group_info": edit_group_info,
        "send_messages": send_messages,
        "add_other_members": add_other_members,
        "add_members": add_other_members,
        "pin_messages": pin_messages,
        # Backward compatibility
        "default_preset": admin_preset,
        "options": ["admins", "all_members"],
        "labels": preset_labels,
        "selected_label": preset_labels.get(admin_preset, admin_preset.title()),
        "permissions_preview": {
            "edit_group_info": "admins" if admin_preset == "admins" else "everyone",
            "send_messages": "everyone",
            "send_media": "everyone",
            "add_members": "admins" if admin_preset == "admins" else "everyone",
            "pin_messages": "admins" if admin_preset == "admins" else "everyone",
            "remove_members": "admins",
            "approve_requests": "admins",
            "manage_admins": "admins",
        },
        "permissions_matrix": [
            {"permission": "Edit group information", "admin": "Yes", "member": "Based on permission" if admin_preset == "admins" else "Yes"},
            {"permission": "Add members", "admin": "Configurable", "member": "Configurable"},
            {"permission": "Remove members", "admin": "Yes", "member": "No"},
            {"permission": "Approve requests", "admin": "Yes", "member": "No"},
            {"permission": "Send messages", "admin": "Yes", "member": "Yes"},
            {"permission": "Manage admins", "admin": "Yes, subject to role hierarchy", "member": "No"},
        ],
    }

    selected_inv_label = invitation_labels.get(invitations_mode, "Require approval")

    return {
        "title": "Group Settings",
        "who_can_add_me_to_groups": {
            "title": "Who can add me to groups",
            "value": who,
            "options": ["everyone", "contacts", "contacts_except", "nobody"],
            "ui_options": ["everyone", "contacts", "nobody"],
            "options_detailed": VALID_WHO_CAN_ADD_OPTIONS,
            "labels": who_labels,
            "selected_label": who_labels.get(who, "Everyone"),
            "excluded_contacts": groups_except,
            "excluded_contacts_count": len(groups_except),
            "description": "Choose who can add you directly to group chats without sending an invitation."
        },
        "group_privacy": {
            "title": "Group Privacy",
            "group_invitations": {
                "title": "Group Invitations",
                "sheet_title": "Group Invitations",
                "subtitle": selected_inv_label,
                "selected_label": selected_inv_label,
                "value": invitations_mode,
                "allow_invite_links": allow_invite_links,
                "options": ["allow", "review", "block"],
                "ui_options": ["require_approval", "direct_invitations_only", "open_invitation_links_allowed"],
                "options_detailed": VALID_GROUP_INVITATION_OPTIONS,
                "labels": invitation_labels,
                "pending_count": len(invitations),
                "action_url": "/api/groups/invitations/",
                "description": "How people may invite you into a group."
            },
            "member_approval": {
                "title": "Member Approval",
                "subtitle": "Admins must approve new members",
                "description": "Admins must approve new members",
                "value": member_approval_by_default,
                "status": "on" if member_approval_by_default else "off",
                "require_approval_by_default": member_approval_by_default,
                "options": ["on", "off"],
                "labels": {"on": "On", "off": "Off"},
            },
            "admin_controls": admin_controls_data,
            "group_info_visibility": {
                "title": "Group Info Visibility",
                "sheet_title": "Group Info Visibility",
                "subtitle": info_vis_labels.get(info_vis, "My Contacts"),
                "description": "Who can see the groups you belong to.",
                "value": info_vis,
                "selected_label": info_vis_labels.get(info_vis, "My Contacts"),
                "options": ["everyone", "contacts", "nobody"],
                "options_detailed": VALID_GROUP_INFO_VISIBILITY_OPTIONS,
                "labels": info_vis_labels,
                "show_groups_on_profile": show_profile,
            },
            "recent_groups": {
                "title": "Recent Groups",
                "total_count": len(recent_groups),
                "groups": recent_groups,
                "action_url": "/api/groups/recent/",
                "description": "Your recently active group conversations and memberships."
            }
        },
        # Top-level helper fields for direct frontend binding
        "admin_permissions": admin_controls_data["permissions"],
        "edit_group_info": edit_group_info,
        "send_messages": send_messages,
        "add_other_members": add_other_members,
        "pin_messages": pin_messages,
    }


def update_group_settings_flow(user, data, request=None):
    """
    Updates group settings from a partial payload, syncing with
    user privacy preferences and returning the updated flow.
    """
    g_prefs = get_section_preferences(user.id, "group_settings") or {}
    p_prefs = get_section_preferences(user.id, "privacy") or {}

    if hasattr(data, "dict"):
        data = data.dict()

    # 1. Who can add me to groups
    who_raw = None
    exc_raw = None
    if "who_can_add_me_to_groups" in data:
        who_raw = data["who_can_add_me_to_groups"]
    elif "who_can_add_to_groups" in data:
        who_raw = data["who_can_add_to_groups"]
    elif "groups" in data:
        who_raw = data["groups"]
    elif "add_to_groups" in data:
        who_raw = data["add_to_groups"]

    if isinstance(who_raw, dict):
        exc_raw = who_raw.get("excluded_contacts") or who_raw.get("except")
        who_raw = who_raw.get("value")

    if exc_raw is None:
        exc_raw = data.get("groups_except") or data.get("who_can_add_except") or data.get("excluded_contacts")

    who_val = _extract_scalar(who_raw)
    if who_val is not None:
        who_str = str(who_val).lower().strip().replace(" ", "_").replace("-", "_")
        if who_str in ("my_contacts_except", "contacts_except", "except"):
            norm_who = "contacts_except"
        elif who_str in ("my_contacts", "contacts", "people_in_your_address_book"):
            norm_who = "contacts"
        elif who_str in ("nobody", "none", "hidden_from_everyone"):
            norm_who = "nobody"
        elif who_str in ("everyone", "anyone", "all", "anyone_on_exi"):
            norm_who = "everyone"
        else:
            norm_who = who_str

        if norm_who in ("everyone", "contacts", "contacts_except", "nobody"):
            g_prefs["who_can_add_to_groups"] = norm_who
            g_prefs["who_can_add_me_to_groups"] = norm_who
            p_prefs["groups"] = norm_who
            p_prefs["add_to_groups"] = norm_who

    if exc_raw is not None and isinstance(exc_raw, list):
        g_prefs["groups_except"] = exc_raw
        g_prefs["who_can_add_except"] = exc_raw
        p_prefs["groups_except"] = exc_raw
        p_prefs["groups_excluded_contacts"] = exc_raw

    # Group privacy sub-dict handling
    gp_data = data.get("group_privacy") if isinstance(data.get("group_privacy"), dict) else {}

    # 2. Group Invitations
    inv_raw = None
    if "group_invitations" in data:
        inv_raw = data["group_invitations"]
    elif "group_invitations" in gp_data:
        inv_raw = gp_data["group_invitations"]
    if isinstance(inv_raw, dict):
        if "value" in inv_raw:
            inv_raw = inv_raw["value"]
    inv_val = _extract_scalar(inv_raw)
    if inv_val is not None:
        inv_str = str(inv_val).lower().strip().replace(" ", "_").replace("-", "_")
        if inv_str in ("require_approval", "review", "approval", "every_invite_waits_for_your_yes"):
            norm_inv = "require_approval" if "require" in inv_str else inv_str
        elif inv_str in ("direct_invitations_only", "direct_only", "direct", "block", "only_people_who_invite_you_by_name"):
            norm_inv = "direct_invitations_only" if "direct" in inv_str else inv_str
        elif inv_str in ("open_invitation_links_allowed", "links_allowed", "links", "allow", "anyone_with_a_link_can_add_you"):
            norm_inv = "open_invitation_links_allowed" if "open" in inv_str else inv_str
        else:
            norm_inv = inv_str
        g_prefs["group_invitations"] = norm_inv
        p_prefs["group_invitations"] = norm_inv

    ail_raw = data.get("allow_invite_links", gp_data.get("allow_invite_links"))
    if ail_raw is not None:
        g_prefs["allow_invite_links"] = _to_b_bool(ail_raw)

    # 3. Member Approval
    ma_raw = None
    if "member_approval" in data:
        ma_raw = data["member_approval"]
    elif "member_approval_by_default" in data:
        ma_raw = data["member_approval_by_default"]
    elif "require_approval_by_default" in data:
        ma_raw = data["require_approval_by_default"]
    elif "member_approval" in gp_data:
        ma_raw = gp_data["member_approval"]
    if isinstance(ma_raw, dict):
        ma_raw = ma_raw.get("value", ma_raw.get("require_approval_by_default", ma_raw.get("status")))
    if ma_raw is not None:
        ma_bool = _to_b_bool(ma_raw)
        g_prefs["member_approval_by_default"] = ma_bool
        g_prefs["member_approval"] = ma_bool

    # 4. Admin Controls & Granular Admin Permissions
    ac_data = data.get("admin_controls") if isinstance(data.get("admin_controls"), dict) else {}
    ap_data = data.get("admin_permissions") if isinstance(data.get("admin_permissions"), dict) else {}
    gp_ac = gp_data.get("admin_controls") if isinstance(gp_data.get("admin_controls"), dict) else {}
    gp_ap = gp_data.get("admin_permissions") if isinstance(gp_data.get("admin_permissions"), dict) else {}

    admin_perms = g_prefs.get("admin_permissions") or {}
    for perm_key in ("edit_group_info", "send_messages", "add_other_members", "pin_messages"):
        val = None
        for source in (data, ac_data, ap_data, gp_data, gp_ac, gp_ap):
            if perm_key in source:
                val = source[perm_key]
                break
            if perm_key == "add_other_members" and "add_members" in source:
                val = source["add_members"]
                break
        if val is not None:
            if isinstance(val, dict):
                val = val.get("value", val.get("status"))
            b_val = _to_b_bool(val)
            admin_perms[perm_key] = b_val
            g_prefs[perm_key] = b_val
            if perm_key == "add_other_members":
                admin_perms["add_members"] = b_val
                g_prefs["add_members"] = b_val

    g_prefs["admin_permissions"] = admin_perms

    ac_raw = None
    if "admin_controls_preset" in data:
        ac_raw = data["admin_controls_preset"]
    elif "default_preset" in data:
        ac_raw = data["default_preset"]
    elif "default_preset" in ac_data:
        ac_raw = ac_data["default_preset"]
    elif "admin_controls_preset" in ac_data:
        ac_raw = ac_data["admin_controls_preset"]
    elif "default_preset" in gp_ac:
        ac_raw = gp_ac["default_preset"]

    ac_val = _extract_scalar(ac_raw)
    if ac_val is not None:
        ac_val = str(ac_val).lower().strip()
        if ac_val in ("admins", "all_members"):
            g_prefs["admin_controls_preset"] = ac_val

    # 5. Group Info Visibility
    giv_raw = None
    if "group_info_visibility" in data:
        giv_raw = data["group_info_visibility"]
    elif "group_info_visibility" in gp_data:
        giv_raw = gp_data["group_info_visibility"]
    if isinstance(giv_raw, dict):
        giv_raw = giv_raw.get("value")
    giv_val = _extract_scalar(giv_raw)
    if giv_val is not None:
        giv_str = str(giv_val).lower().strip().replace(" ", "_").replace("-", "_")
        if giv_str in ("my_contacts", "contacts", "people_in_your_address_book"):
            norm_giv = "contacts"
        elif giv_str in ("nobody", "none", "hidden_from_everyone"):
            norm_giv = "nobody"
        elif giv_str in ("everyone", "anyone", "all", "anyone_on_exi"):
            norm_giv = "everyone"
        else:
            norm_giv = giv_str
        if norm_giv in ("everyone", "contacts", "nobody"):
            g_prefs["group_info_visibility"] = norm_giv

    sgp_raw = data.get("show_groups_on_profile", gp_data.get("show_groups_on_profile"))
    if sgp_raw is not None:
        g_prefs["show_groups_on_profile"] = _to_b_bool(sgp_raw)

    update_section_preferences(user.id, "group_settings", g_prefs)
    update_section_preferences(user.id, "privacy", p_prefs)

    return get_group_settings_flow(user, request)


# ==============================================================================
# STATUS (STORIES) WORKFLOW MANAGEMENT
# ==============================================================================

STATUSES_DIR = os.path.join(settings.BASE_DIR, 'data', 'statuses')
os.makedirs(STATUSES_DIR, exist_ok=True)


def _status_file_path(user_id):
    return os.path.join(STATUSES_DIR, f"{user_id}.json")


def _status_cache_key(status_id):
    return f"status_item:{status_id}"


def _user_statuses_cache_key(user_id):
    return f"user_statuses:{user_id}"


def _filter_active_statuses(statuses):
    """Filters out statuses that have expired past 24 hours."""
    now = timezone.now()
    active = []
    for s in statuses:
        exp_str = s.get("expires_at")
        if not exp_str:
            continue
        try:
            exp_dt = datetime.fromisoformat(exp_str.replace("Z", "+00:00"))
            if timezone.is_naive(exp_dt):
                exp_dt = timezone.make_aware(exp_dt)
            if now < exp_dt:
                active.append(s)
        except Exception:
            continue
    return active


def get_user_statuses(user_id, active_only=True):
    """Retrieve all statuses posted by a user."""
    uid = str(user_id)
    cache_key = _user_statuses_cache_key(uid)
    cached = cache.get(cache_key)
    if cached is not None and isinstance(cached, list):
        statuses = cached
    else:
        fpath = _status_file_path(uid)
        if os.path.exists(fpath):
            try:
                with open(fpath, "r", encoding="utf-8") as f:
                    statuses = json.load(f)
            except Exception:
                statuses = []
        else:
            statuses = []

    if active_only:
        active = _filter_active_statuses(statuses)
        if len(active) != len(statuses):
            _save_user_statuses(user_id, active)
        return active
    return statuses


def _save_user_statuses(user_id, statuses):
    """Save statuses for a user to cache and disk."""
    uid = str(user_id)
    cache_key = _user_statuses_cache_key(uid)
    cache.set(cache_key, statuses, timeout=86400)
    for s in statuses:
        cache.set(_status_cache_key(s["id"]), (uid, s), timeout=86400)
    try:
        fpath = _status_file_path(uid)
        with open(fpath, "w", encoding="utf-8") as f:
            json.dump(statuses, f, indent=2)
    except Exception as e:
        logger.error(f"Failed to save statuses for user {uid}: {e}")


def get_status_by_id(status_id):
    """Retrieve a single status and its owner's user_id."""
    sid = str(status_id)
    cached = cache.get(_status_cache_key(sid))
    if cached is not None and isinstance(cached, tuple):
        uid, status_item = cached
        active = _filter_active_statuses([status_item])
        if active:
            return active[0], uid
        return None, None

    if os.path.exists(STATUSES_DIR):
        for fname in os.listdir(STATUSES_DIR):
            if fname.endswith(".json"):
                uid = fname[:-5]
                user_statuses = get_user_statuses(uid, active_only=True)
                for s in user_statuses:
                    if s.get("id") == sid:
                        cache.set(_status_cache_key(sid), (uid, s), timeout=86400)
                        return s, uid
    return None, None


def can_view_status(creator, viewer, status_data):
    """
    Evaluates whether `viewer` is allowed to view `status_data` created by `creator`.
    """
    if creator.id == viewer.id:
        return True

    from accounts.utils import is_user_blocked, are_contacts
    if is_user_blocked(creator, viewer) or is_user_blocked(viewer, creator):
        return False

    vis = status_data.get("visibility", "contacts")
    if vis == "everyone":
        return True
    elif vis == "contacts":
        return are_contacts(creator, viewer)
    elif vis == "contacts_except":
        if not are_contacts(creator, viewer):
            return False
        excluded = [int(x) for x in (status_data.get("excluded_contacts") or []) if str(x).isdigit()]
        return viewer.id not in excluded
    elif vis == "selected":
        selected = [int(x) for x in (status_data.get("selected_contacts") or []) if str(x).isdigit()]
        return viewer.id in selected
    elif vis == "nobody":
        return False
    return are_contacts(creator, viewer)


def create_status(user, status_type, caption="", text_content="", background_color="#1E293B", font_style="default", media_file=None, media_url="", media_type="", visibility=None, selected_contacts=None, excluded_contacts=None, request=None):
    """
    Create a new status for a user.
    Status types: 'photo', 'video', 'text'.
    Expires automatically after 24 hours.
    """
    import uuid
    from django.core.files.storage import default_storage

    stype = str(status_type or "").lower().strip()
    if stype in ("image", "photo"):
        stype = "photo"
    elif stype in ("video", "vid"):
        stype = "video"
    elif stype in ("text", "caption"):
        stype = "text"
    else:
        stype = "text" if (text_content and not media_file and not media_url) else "photo"

    final_media_url = media_url or ""
    final_media_type = media_type or ""
    final_media_size = 0

    if media_file:
        file_ext = os.path.splitext(media_file.name)[1]
        date_prefix = timezone.now().strftime("%Y/%m/%d")
        safe_name = f"{uuid.uuid4().hex[:16]}{file_ext}"
        rel_path = f"status_media/{date_prefix}/{safe_name}"
        saved_path = default_storage.save(rel_path, media_file)
        if request:
            final_media_url = request.build_absolute_uri(settings.MEDIA_URL + saved_path)
        else:
            final_media_url = f"{settings.MEDIA_URL}{saved_path}"
        final_media_type = getattr(media_file, "content_type", "") or ("image/jpeg" if stype == "photo" else "video/mp4")
        final_media_size = getattr(media_file, "size", 0)

    if not visibility:
        p_prefs = get_section_preferences(user.id, "privacy") or {}
        visibility = p_prefs.get("status", "contacts")
        if visibility == "selected" and not selected_contacts:
            selected_contacts = p_prefs.get("status_selected_contacts", [])

    vis = str(visibility).lower().strip() if visibility else "contacts"
    if vis not in ("everyone", "contacts", "contacts_except", "selected", "nobody"):
        vis = "contacts"

    now = timezone.now()
    expires_at = now + timedelta(hours=24)

    profile_photo_url = ""
    if getattr(user, "profile_photo", None):
        try:
            profile_photo_url = request.build_absolute_uri(user.profile_photo.url) if request else user.profile_photo.url
        except Exception:
            profile_photo_url = str(user.profile_photo)

    status_id = str(uuid.uuid4())
    status_entry = {
        "id": status_id,
        "user_id": user.id,
        "user": {
            "id": user.id,
            "username": user.username,
            "display_name": getattr(user, "display_name", None) or getattr(user, "full_name", None) or user.username,
            "profile_photo": profile_photo_url,
            "exi_id": getattr(user, "exi_id", ""),
        },
        "status_type": stype,
        "caption": str(caption or "").strip()[:500],
        "text_content": str(text_content or "").strip()[:1000],
        "background_color": str(background_color or "#1E293B").strip()[:30],
        "font_style": str(font_style or "default").strip()[:30],
        "media_url": final_media_url,
        "media_type": final_media_type,
        "media_size": final_media_size,
        "visibility": vis,
        "selected_contacts": [int(x) for x in (selected_contacts or []) if str(x).isdigit()],
        "excluded_contacts": [int(x) for x in (excluded_contacts or []) if str(x).isdigit()],
        "created_at": now.isoformat(),
        "expires_at": expires_at.isoformat(),
        "view_count": 0,
        "viewers": [],
    }

    user_statuses = get_user_statuses(user.id, active_only=True)
    user_statuses.append(status_entry)
    _save_user_statuses(user.id, user_statuses)

    return status_entry


def record_status_view(status_id, viewer, request=None):
    """
    Record that `viewer` viewed `status_id`.
    Returns: (success: bool, is_new_view: bool, status_data: dict, error: str or None)
    """
    status_data, owner_uid = get_status_by_id(status_id)
    if not status_data:
        return False, False, None, "Status not found or expired."

    try:
        from django.contrib.auth import get_user_model
        User = get_user_model()
        creator = User.objects.get(id=int(owner_uid), is_active=True)
    except Exception:
        return False, False, None, "Creator user not found."

    if not can_view_status(creator, viewer, status_data):
        return False, False, None, "You do not have permission to view this status."

    if viewer.id == creator.id:
        return True, False, status_data, None

    viewers = status_data.setdefault("viewers", [])
    already_viewed = any(v.get("user_id") == viewer.id for v in viewers)
    if already_viewed:
        return True, False, status_data, None

    profile_photo_url = ""
    if getattr(viewer, "profile_photo", None):
        try:
            profile_photo_url = request.build_absolute_uri(viewer.profile_photo.url) if request else viewer.profile_photo.url
        except Exception:
            profile_photo_url = str(viewer.profile_photo)

    viewers.append({
        "user_id": viewer.id,
        "username": viewer.username,
        "display_name": getattr(viewer, "display_name", None) or getattr(viewer, "full_name", None) or viewer.username,
        "profile_photo": profile_photo_url,
        "viewed_at": timezone.now().isoformat(),
    })
    status_data["view_count"] = len(viewers)

    all_statuses = get_user_statuses(owner_uid, active_only=False)
    for idx, s in enumerate(all_statuses):
        if s.get("id") == str(status_id):
            all_statuses[idx] = status_data
            break
    _save_user_statuses(owner_uid, all_statuses)

    return True, True, status_data, None


def get_status_viewers(status_id, requesting_user):
    """
    Returns list of viewers for a status (Creator only!).
    """
    status_data, owner_uid = get_status_by_id(status_id)
    if not status_data:
        return False, "Status not found or expired.", None

    if requesting_user.id != int(owner_uid):
        return False, "Only the status creator can view the viewer list.", None

    return True, None, {
        "status_id": status_data["id"],
        "view_count": len(status_data.get("viewers", [])),
        "viewers": status_data.get("viewers", []),
    }


def delete_status(status_id, requesting_user):
    """
    Deletes a status (Creator only).
    """
    status_data, owner_uid = get_status_by_id(status_id)
    if not status_data:
        return False, "Status not found or already expired."

    if requesting_user.id != int(owner_uid):
        return False, "Only the status creator can delete this status."

    all_statuses = get_user_statuses(owner_uid, active_only=False)
    remaining = [s for s in all_statuses if s.get("id") != str(status_id)]
    _save_user_statuses(owner_uid, remaining)
    cache.delete(_status_cache_key(str(status_id)))
    return True, "Status deleted successfully."


def get_status_feed(requesting_user, request=None):
    """
    Returns the complete status feed:
    - my_status: requesting user's active statuses (with viewers)
    - recent_updates: unviewed active statuses from contacts
    - viewed_updates: viewed active statuses from contacts
    - muted_updates: statuses from muted contacts
    """
    from django.contrib.auth import get_user_model
    User = get_user_model()

    my_active = get_user_statuses(requesting_user.id, active_only=True)
    my_status_data = None
    if my_active:
        profile_photo_url = ""
        if getattr(requesting_user, "profile_photo", None):
            try:
                profile_photo_url = request.build_absolute_uri(requesting_user.profile_photo.url) if request else requesting_user.profile_photo.url
            except Exception:
                profile_photo_url = str(requesting_user.profile_photo)

        my_status_data = {
            "user_id": requesting_user.id,
            "username": requesting_user.username,
            "display_name": getattr(requesting_user, "display_name", None) or getattr(requesting_user, "full_name", None) or requesting_user.username,
            "profile_photo": profile_photo_url,
            "status_count": len(my_active),
            "last_updated": my_active[-1].get("created_at"),
            "statuses": my_active,
        }

    p_prefs = get_section_preferences(requesting_user.id, "privacy") or {}
    muted_user_ids = set(p_prefs.get("muted_status_users", []))

    recent_updates = []
    viewed_updates = []
    muted_updates = []

    if os.path.exists(STATUSES_DIR):
        for fname in os.listdir(STATUSES_DIR):
            if not fname.endswith(".json"):
                continue
            uid_str = fname[:-5]
            try:
                uid = int(uid_str)
            except ValueError:
                continue

            if uid == requesting_user.id:
                continue

            try:
                creator = User.objects.get(id=uid, is_active=True)
            except User.DoesNotExist:
                continue

            user_statuses = get_user_statuses(uid, active_only=True)
            if not user_statuses:
                continue

            visible_statuses = []
            for s in user_statuses:
                if can_view_status(creator, requesting_user, s):
                    viewers = s.get("viewers", [])
                    has_viewed = any(v.get("user_id") == requesting_user.id for v in viewers)
                    s_copy = dict(s)
                    s_copy.pop("viewers", None)
                    s_copy["is_viewed"] = has_viewed
                    visible_statuses.append(s_copy)

            if not visible_statuses:
                continue

            all_viewed = all(s.get("is_viewed", False) for s in visible_statuses)
            last_updated = visible_statuses[-1].get("created_at")

            profile_photo_url = ""
            if getattr(creator, "profile_photo", None):
                try:
                    profile_photo_url = request.build_absolute_uri(creator.profile_photo.url) if request else creator.profile_photo.url
                except Exception:
                    profile_photo_url = str(creator.profile_photo)

            card = {
                "user_id": creator.id,
                "username": creator.username,
                "display_name": getattr(creator, "display_name", None) or getattr(creator, "full_name", None) or creator.username,
                "profile_photo": profile_photo_url,
                "status_count": len(visible_statuses),
                "all_viewed": all_viewed,
                "last_updated": last_updated,
                "statuses": visible_statuses,
            }

            if uid in muted_user_ids:
                muted_updates.append(card)
            elif all_viewed:
                viewed_updates.append(card)
            else:
                recent_updates.append(card)

    recent_updates.sort(key=lambda x: x.get("last_updated", ""), reverse=True)
    viewed_updates.sort(key=lambda x: x.get("last_updated", ""), reverse=True)
    muted_updates.sort(key=lambda x: x.get("last_updated", ""), reverse=True)

    return {
        "my_status": my_status_data,
        "recent_updates": recent_updates,
        "viewed_updates": viewed_updates,
        "muted_updates": muted_updates,
        "total_active_contacts": len(recent_updates) + len(viewed_updates),
    }


def mute_status_user(user, target_user_id):
    p_prefs = get_section_preferences(user.id, "privacy") or {}
    muted = set(p_prefs.get("muted_status_users", []))
    muted.add(int(target_user_id))
    p_prefs["muted_status_users"] = list(muted)
    update_section_preferences(user.id, "privacy", p_prefs)
    return True, f"Status updates from user {target_user_id} have been muted."


def unmute_status_user(user, target_user_id):
    p_prefs = get_section_preferences(user.id, "privacy") or {}
    muted = set(p_prefs.get("muted_status_users", []))
    muted.discard(int(target_user_id))
    p_prefs["muted_status_users"] = list(muted)
    update_section_preferences(user.id, "privacy", p_prefs)
    return True, f"Status updates from user {target_user_id} unmuted."


# ==============================================================================
# 1. PROFILE & DIGITAL EXI CARD MANAGEMENT
# ==============================================================================

VALID_CARD_THEMES = ["classic", "dark", "neon", "obsidian", "emerald", "gold"]


def get_user_exi_card(user, request=None, viewer=None):
    """
    Constructs the Digital EXI Card representation of the user:
    - Profile photo & Name
    - Unique EXI Username & Handle (@username.exi)
    - Digital EXI Card styling, security chip hash, card number, QR data
    - Privacy masking based on viewer relationship
    """
    card_prefs = get_section_preferences(user.id, "exi_card") or {}
    theme = card_prefs.get("theme", "classic")
    if theme not in VALID_CARD_THEMES:
        theme = "classic"

    badge_title = card_prefs.get("badge_title", "Verified Member")
    custom_tagline = card_prefs.get("custom_tagline", "")
    show_phone = _to_b(card_prefs.get("show_phone", False))
    show_email = _to_b(card_prefs.get("show_email", False))
    show_bio = _to_b(card_prefs.get("show_bio", True))
    show_qr = _to_b(card_prefs.get("show_qr", True))

    is_self = viewer is None or (viewer and viewer.id == user.id)

    profile_photo_url = ""
    if getattr(user, "profile_photo", None):
        try:
            profile_photo_url = request.build_absolute_uri(user.profile_photo.url) if request else user.profile_photo.url
        except Exception:
            profile_photo_url = str(user.profile_photo)

    # Generate standardized digital card number and chip signature
    exi_numeric = abs(hash(user.exi_id)) % 10000
    card_number = f"4532 •••• •••• {exi_numeric:04d}"
    security_chip = hashlib.sha256(f"exi_chip:{user.id}:{user.exi_id}".encode()).hexdigest()[:16].upper()

    # Formatted handle
    handle = f"@{user.username}.exi" if user.username else f"@{user.exi_id}"

    # Phone & email privacy handling
    phone_display = user.phone or ""
    if not is_self and not show_phone and phone_display:
        phone_display = phone_display[:3] + " ••• ••" + phone_display[-2:] if len(phone_display) > 6 else "••••••••"

    email_display = user.email or ""
    if not is_self and not show_email and email_display and "@" in email_display:
        parts = email_display.split("@")
        email_display = parts[0][:2] + "•••@" + parts[1]

    qr_data = f"exi://user/{user.exi_id}"
    share_url = request.build_absolute_uri(f"/api/profile/card/{user.exi_id}/") if request else f"/api/profile/card/{user.exi_id}/"

    member_since = user.date_joined.strftime("%B %Y") if getattr(user, "date_joined", None) else "2026"

    return {
        "exi_id": user.exi_id,
        "username": user.username,
        "handle": handle,
        "display_name": user.display_name or user.full_name or user.username,
        "full_name": user.full_name or user.display_name or user.username,
        "profile_photo": profile_photo_url,
        "bio": (user.bio or "") if (is_self or show_bio) else "",
        "status_message": user.status_message or "",
        "phone": phone_display if (is_self or show_phone) else None,
        "email": email_display if (is_self or show_email) else None,
        "digital_card": {
            "card_number": card_number,
            "security_chip": security_chip,
            "theme": theme,
            "badge_title": badge_title,
            "custom_tagline": custom_tagline,
            "member_since": member_since,
            "qr_code_data": qr_data if show_qr else None,
            "share_url": share_url,
            "supported_themes": VALID_CARD_THEMES,
        },
        "settings": {
            "theme": theme,
            "badge_title": badge_title,
            "custom_tagline": custom_tagline,
            "show_phone": show_phone,
            "show_email": show_email,
            "show_bio": show_bio,
            "show_qr": show_qr,
        }
    }


def update_user_exi_card(user, data):
    """Update digital card styling and visibility preferences."""
    card_prefs = get_section_preferences(user.id, "exi_card") or {}

    if "theme" in data:
        t = str(data["theme"]).lower().strip()
        if t in VALID_CARD_THEMES:
            card_prefs["theme"] = t
    if "badge_title" in data:
        card_prefs["badge_title"] = str(data["badge_title"]).strip()[:50]
    if "custom_tagline" in data:
        card_prefs["custom_tagline"] = str(data["custom_tagline"]).strip()[:100]
    if "show_phone" in data:
        card_prefs["show_phone"] = _to_b(data["show_phone"])
    if "show_email" in data:
        card_prefs["show_email"] = _to_b(data["show_email"])
    if "show_bio" in data:
        card_prefs["show_bio"] = _to_b(data["show_bio"])
    if "show_qr" in data:
        card_prefs["show_qr"] = _to_b(data["show_qr"])

    update_section_preferences(user.id, "exi_card", card_prefs)
    return get_user_exi_card(user)


# ==============================================================================
# 2. SHARE PROFILE HEALTH CARD & EMERGENCY INFORMATION
# ==============================================================================

HEALTH_CARDS_DIR = os.path.join(settings.BASE_DIR, 'data', 'health_cards')
os.makedirs(HEALTH_CARDS_DIR, exist_ok=True)

VALID_BLOOD_GROUPS = ["A+", "A-", "B+", "B-", "AB+", "AB-", "O+", "O-", "Unknown"]


def _health_card_file(user_id):
    return os.path.join(HEALTH_CARDS_DIR, f"{user_id}.json")


def _health_card_cache_key(user_id):
    return f"health_card:{user_id}"


def get_user_health_card(user, request=None, is_emergency_public=False):
    """
    Retrieve user's Health Identity Card & Emergency information.
    If `is_emergency_public` is True, returns only fields permitted by sharing settings.
    """
    uid = str(user.id)
    cache_key = _health_card_cache_key(uid)
    cached = cache.get(cache_key)
    if cached is not None and isinstance(cached, dict):
        card = cached
    else:
        fpath = _health_card_file(uid)
        if os.path.exists(fpath):
            try:
                with open(fpath, "r", encoding="utf-8") as f:
                    card = json.load(f)
            except Exception:
                card = {}
        else:
            card = {}

    # Ensure default structure
    if "share_token" not in card or not card["share_token"]:
        card["share_token"] = hashlib.md5(f"health:{user.id}:{user.exi_id}".encode()).hexdigest()[:12]

    # Map defaults
    personal = card.setdefault("personal_details", {})
    personal.setdefault("full_name", user.full_name or user.display_name or user.username)
    personal.setdefault("date_of_birth", "")
    personal.setdefault("age", None)
    personal.setdefault("gender", "")
    personal.setdefault("height_cm", None)
    personal.setdefault("weight_kg", None)
    personal.setdefault("organ_donor", False)
    personal.setdefault("primary_physician", {"name": "", "phone": "", "clinic": ""})
    personal.setdefault("insurance", {"provider": "", "policy_number": ""})

    card.setdefault("blood_group", "Unknown")
    card.setdefault("emergency_contacts", [])
    card.setdefault("allergies", [])
    card.setdefault("chronic_conditions", [])
    card.setdefault("medications", [])
    card.setdefault("medical_notes", "")
    card.setdefault("family_profiles", [])
    card.setdefault("emergency_sharing_enabled", True)

    sharing_settings = card.setdefault("sharing_settings", {
        "share_blood_group": True,
        "share_allergies": True,
        "share_emergency_contacts": True,
        "share_medications": True,
        "share_medical_notes": True,
    })

    share_token = card["share_token"]
    emergency_qr = f"exi://health/{share_token}"
    share_url = request.build_absolute_uri(f"/api/health/card/share/{share_token}/") if request else f"/api/health/card/share/{share_token}/"

    card["emergency_qr"] = emergency_qr
    card["share_url"] = share_url

    if is_emergency_public:
        if not card.get("emergency_sharing_enabled", True):
            return {"detail": "Emergency health sharing is disabled for this profile."}

        # Filter strictly for emergency responders
        return {
            "full_name": personal.get("full_name"),
            "blood_group": card.get("blood_group") if sharing_settings.get("share_blood_group", True) else None,
            "allergies": card.get("allergies") if sharing_settings.get("share_allergies", True) else [],
            "chronic_conditions": card.get("chronic_conditions") if sharing_settings.get("share_medical_notes", True) else [],
            "medications": card.get("medications") if sharing_settings.get("share_medications", True) else [],
            "medical_notes": card.get("medical_notes") if sharing_settings.get("share_medical_notes", True) else "",
            "organ_donor": personal.get("organ_donor", False),
            "emergency_contacts": card.get("emergency_contacts") if sharing_settings.get("share_emergency_contacts", True) else [],
            "is_emergency_card": True,
        }

    return card


def _save_user_health_card(user_id, card):
    uid = str(user_id)
    cache_key = _health_card_cache_key(uid)
    cache.set(cache_key, card, timeout=86400)
    try:
        fpath = _health_card_file(uid)
        with open(fpath, "w", encoding="utf-8") as f:
            json.dump(card, f, indent=2)
    except Exception as e:
        logger.error(f"Failed to save health card for user {uid}: {e}")


def update_user_health_card(user, data, request=None):
    card = get_user_health_card(user, request=request)

    if "blood_group" in data:
        bg = str(data["blood_group"]).strip()
        if bg in VALID_BLOOD_GROUPS:
            card["blood_group"] = bg

    if "personal_details" in data and isinstance(data["personal_details"], dict):
        p = card.setdefault("personal_details", {})
        for k, v in data["personal_details"].items():
            p[k] = v

    if "medical_notes" in data:
        card["medical_notes"] = str(data["medical_notes"]).strip()[:1000]

    if "allergies" in data and isinstance(data["allergies"], list):
        card["allergies"] = [str(a).strip() for a in data["allergies"] if str(a).strip()]

    if "chronic_conditions" in data and isinstance(data["chronic_conditions"], list):
        card["chronic_conditions"] = [str(c).strip() for c in data["chronic_conditions"] if str(c).strip()]

    if "medications" in data and isinstance(data["medications"], list):
        card["medications"] = data["medications"]

    if "emergency_contacts" in data and isinstance(data["emergency_contacts"], list):
        card["emergency_contacts"] = data["emergency_contacts"]

    if "emergency_sharing_enabled" in data:
        card["emergency_sharing_enabled"] = _to_b(data["emergency_sharing_enabled"])

    if "sharing_settings" in data and isinstance(data["sharing_settings"], dict):
        sh = card.setdefault("sharing_settings", {})
        for k, v in data["sharing_settings"].items():
            sh[k] = _to_b(v)

    _save_user_health_card(user.id, card)
    return get_user_health_card(user, request=request)


def add_emergency_contact(user, contact_data):
    card = get_user_health_card(user)
    contacts = card.setdefault("emergency_contacts", [])
    new_id = f"ec-{uuid.uuid4().hex[:8]}" if "uuid" in globals() else f"ec-{secrets.token_hex(4)}"

    contact = {
        "id": new_id,
        "name": str(contact_data.get("name") or "").strip()[:100],
        "relationship": str(contact_data.get("relationship") or "Emergency Contact").strip()[:50],
        "phone_number": str(contact_data.get("phone_number") or "").strip()[:30],
        "is_primary": _to_b(contact_data.get("is_primary", len(contacts) == 0)),
    }
    contacts.append(contact)
    _save_user_health_card(user.id, card)
    return contact


def delete_emergency_contact(user, contact_id):
    card = get_user_health_card(user)
    contacts = card.get("emergency_contacts", [])
    card["emergency_contacts"] = [c for c in contacts if c.get("id") != str(contact_id)]
    _save_user_health_card(user.id, card)
    return True


def get_family_profiles(user):
    card = get_user_health_card(user)
    return card.get("family_profiles", [])


def add_family_profile(user, data):
    import uuid
    card = get_user_health_card(user)
    families = card.setdefault("family_profiles", [])

    profile = {
        "id": f"fam-{uuid.uuid4().hex[:8]}",
        "relationship": str(data.get("relationship") or "dependent").strip()[:50],
        "full_name": str(data.get("full_name") or "").strip()[:100],
        "date_of_birth": str(data.get("date_of_birth") or "").strip()[:20],
        "blood_group": str(data.get("blood_group") or "Unknown").strip(),
        "allergies": data.get("allergies", []) if isinstance(data.get("allergies"), list) else [],
        "medical_notes": str(data.get("medical_notes") or "").strip()[:500],
        "emergency_contact": str(data.get("emergency_contact") or "").strip()[:50],
        "created_at": timezone.now().isoformat(),
    }
    families.append(profile)
    _save_user_health_card(user.id, card)
    return profile


def update_family_profile(user, family_id, data):
    card = get_user_health_card(user)
    families = card.get("family_profiles", [])
    target = None
    for f in families:
        if f.get("id") == str(family_id):
            target = f
            break
    if not target:
        return None

    for k in ("relationship", "full_name", "date_of_birth", "blood_group", "allergies", "medical_notes", "emergency_contact"):
        if k in data:
            target[k] = data[k]
    target["updated_at"] = timezone.now().isoformat()
    _save_user_health_card(user.id, card)
    return target


def delete_family_profile(user, family_id):
    card = get_user_health_card(user)
    families = card.get("family_profiles", [])
    card["family_profiles"] = [f for f in families if f.get("id") != str(family_id)]
    _save_user_health_card(user.id, card)
    return True


def get_health_card_by_share_token(token, request=None):
    """Locate user by health share token and return public emergency card."""
    if not token or not os.path.exists(HEALTH_CARDS_DIR):
        return None

    from django.contrib.auth import get_user_model
    User = get_user_model()

    for fname in os.listdir(HEALTH_CARDS_DIR):
        if not fname.endswith(".json"):
            continue
        uid = fname[:-5]
        try:
            fpath = os.path.join(HEALTH_CARDS_DIR, fname)
            with open(fpath, "r", encoding="utf-8") as f:
                card_data = json.load(f)
            if card_data.get("share_token") == token:
                user = User.objects.get(id=int(uid), is_active=True)
                return get_user_health_card(user, request=request, is_emergency_public=True)
        except Exception:
            continue
    return None


# ==============================================================================
# 3. MY VAULT - SECURE PERSONAL DOCUMENT STORAGE & ACCESS HISTORY
# ==============================================================================

VAULT_DIR = os.path.join(settings.BASE_DIR, 'data', 'vault')
VAULT_HISTORY_DIR = os.path.join(settings.BASE_DIR, 'data', 'vault_history')
os.makedirs(VAULT_DIR, exist_ok=True)
os.makedirs(VAULT_HISTORY_DIR, exist_ok=True)

VAULT_CATEGORIES = [
    {"id": "identity", "name": "Identity", "description": "Passport, National ID, Driver's License, Voter ID", "icon": "id-card"},
    {"id": "medical", "name": "Medical", "description": "Prescriptions, Lab Reports, Vaccine Records, Insurance", "icon": "heart-pulse"},
    {"id": "financial", "name": "Financial", "description": "Tax Returns, Bank Statements, Invoices, Payslips", "icon": "receipt"},
    {"id": "education", "name": "Education", "description": "Certificates, Diplomas, Transcripts, Degrees", "icon": "graduation-cap"},
    {"id": "property", "name": "Property & Legal", "description": "Contracts, Rental Agreements, Vehicle Registration", "icon": "shield-check"},
    {"id": "personal", "name": "Personal", "description": "Personal Notes, Letters, Miscellaneous Records", "icon": "folder"},
]

VAULT_CAT_MAP = {c["id"]: c["name"] for c in VAULT_CATEGORIES}


def _vault_file(user_id):
    return os.path.join(VAULT_DIR, f"{user_id}.json")


def _vault_cache_key(user_id):
    return f"vault:{user_id}"


def _vault_history_file(user_id):
    return os.path.join(VAULT_HISTORY_DIR, f"{user_id}.json")


def _vault_history_cache_key(user_id):
    return f"vault_history:{user_id}"


def log_vault_access(user, action, doc_id=None, doc_title=None, request=None):
    """
    Appends an audit log entry to the user's Vault Access History.
    """
    import uuid
    uid = str(user.id)
    cache_key = _vault_history_cache_key(uid)
    fpath = _vault_history_file(uid)

    history = cache.get(cache_key)
    if history is None:
        if os.path.exists(fpath):
            try:
                with open(fpath, "r", encoding="utf-8") as f:
                    history = json.load(f)
            except Exception:
                history = []
        else:
            history = []

    ip_address = request.META.get("REMOTE_ADDR", "") if request else ""
    user_agent = request.META.get("HTTP_USER_AGENT", "") if request else ""
    device_name = "Mobile Device" if ("Mobile" in user_agent or "Android" in user_agent or "iPhone" in user_agent) else "Web Browser"

    entry = {
        "id": f"vh-{uuid.uuid4().hex[:8]}",
        "action": action,
        "document_id": str(doc_id) if doc_id else None,
        "document_title": str(doc_title) if doc_title else None,
        "timestamp": timezone.now().isoformat(),
        "ip_address": ip_address,
        "device_name": device_name,
    }
    history.insert(0, entry)
    history = history[:200]  # retain 200 most recent logs

    cache.set(cache_key, history, timeout=86400)
    try:
        with open(fpath, "w", encoding="utf-8") as f:
            json.dump(history, f, indent=2)
    except Exception as e:
        logger.error(f"Failed to log vault access: {e}")
    return entry


def get_vault_history(user, limit=50):
    uid = str(user.id)
    cache_key = _vault_history_cache_key(uid)
    cached = cache.get(cache_key)
    if cached is not None and isinstance(cached, list):
        return cached[:limit]

    fpath = _vault_history_file(uid)
    if os.path.exists(fpath):
        try:
            with open(fpath, "r", encoding="utf-8") as f:
                history = json.load(f)
            cache.set(cache_key, history, timeout=86400)
            return history[:limit]
        except Exception:
            return []
    return []


def get_user_vault_documents(user_id):
    uid = str(user_id)
    cache_key = _vault_cache_key(uid)
    cached = cache.get(cache_key)
    if cached is not None and isinstance(cached, list):
        return cached

    fpath = _vault_file(uid)
    if os.path.exists(fpath):
        try:
            with open(fpath, "r", encoding="utf-8") as f:
                docs = json.load(f)
            cache.set(cache_key, docs, timeout=86400)
            return docs
        except Exception:
            return []
    return []


def _save_user_vault_documents(user_id, docs):
    uid = str(user_id)
    cache_key = _vault_cache_key(uid)
    cache.set(cache_key, docs, timeout=86400)
    try:
        fpath = _vault_file(uid)
        with open(fpath, "w", encoding="utf-8") as f:
            json.dump(docs, f, indent=2)
    except Exception as e:
        logger.error(f"Failed to save vault documents for {uid}: {e}")


def get_user_vault_overview(user, request=None, category=None, search=None, file_type=None):
    """
    Returns full Vault Overview:
    - Vault Lock settings (PIN, Biometrics)
    - Categories & counts
    - Filtered documents list
    - Storage used
    - Recent access history
    """
    vault_settings = get_section_preferences(user.id, "vault_settings") or {}
    sec_prefs = get_section_preferences(user.id, "security") or {}

    lock_enabled = _to_b(vault_settings.get("vault_lock_enabled", False))
    use_biometrics = _to_b(vault_settings.get("use_biometrics", sec_prefs.get("use_biometrics", False)))

    docs = get_user_vault_documents(user.id)

    total_size = sum(d.get("file_size", 0) for d in docs)

    # Category counts
    cat_counts = {c["id"]: 0 for c in VAULT_CATEGORIES}
    for d in docs:
        c_id = d.get("category_id", "personal")
        if c_id in cat_counts:
            cat_counts[c_id] += 1
        else:
            cat_counts["personal"] += 1

    categories_payload = []
    for c in VAULT_CATEGORIES:
        categories_payload.append({
            "id": c["id"],
            "name": c["name"],
            "description": c["description"],
            "icon": c["icon"],
            "document_count": cat_counts.get(c["id"], 0),
        })

    # Filter documents
    filtered_docs = docs
    if category:
        cat_lower = category.lower().strip()
        filtered_docs = [d for d in filtered_docs if d.get("category_id") == cat_lower or d.get("category", "").lower() == cat_lower]

    if file_type:
        ft_lower = file_type.lower().strip()
        filtered_docs = [d for d in filtered_docs if d.get("file_type") == ft_lower]

    if search:
        s_lower = search.lower().strip()
        filtered_docs = [
            d for d in filtered_docs
            if s_lower in d.get("title", "").lower() or s_lower in d.get("description", "").lower() or any(s_lower in t.lower() for t in d.get("tags", []))
        ]

    history = get_vault_history(user, limit=5)

    return {
        "vault_lock": {
            "vault_lock_enabled": lock_enabled,
            "use_biometrics": use_biometrics,
            "has_pin": bool(vault_settings.get("vault_pin_hash") or sec_prefs.get("app_lock_pin_hash")),
            "auto_lock_timeout": vault_settings.get("auto_lock_timeout", 300),
        },
        "stats": {
            "total_documents": len(docs),
            "total_storage_bytes": total_size,
            "total_storage_mb": round(total_size / (1024 * 1024), 2),
        },
        "categories": categories_payload,
        "documents": filtered_docs,
        "recent_history": history,
    }


def add_vault_document(user, title, category, file=None, file_url="", description="", tags=None, request=None):
    """
    Upload and register a document in My Vault.
    Supports Images (JPG, PNG, WEBP) and PDFs.
    """
    import uuid
    from django.core.files.storage import default_storage

    cat_id = str(category or "personal").lower().strip()
    if cat_id not in VAULT_CAT_MAP:
        cat_id = "personal"
    cat_name = VAULT_CAT_MAP.get(cat_id, "Personal")

    final_file_url = file_url or ""
    file_name = ""
    file_size = 0
    mime_type = ""
    file_type = "document"

    if file:
        file_name = file.name
        file_size = file.size
        mime_type = getattr(file, "content_type", "") or "application/octet-stream"

        ext = os.path.splitext(file.name)[1].lower()
        if ext in (".jpg", ".jpeg", ".png", ".webp", ".heic"):
            file_type = "image"
        elif ext == ".pdf":
            file_type = "pdf"
        else:
            file_type = "document"

        date_prefix = timezone.now().strftime("%Y/%m/%d")
        safe_name = f"{uuid.uuid4().hex[:16]}_{file.name.replace(' ', '_')}"
        rel_path = f"vault_documents/{user.id}/{date_prefix}/{safe_name}"
        saved_path = default_storage.save(rel_path, file)

        if request:
            final_file_url = request.build_absolute_uri(settings.MEDIA_URL + saved_path)
        else:
            final_file_url = f"{settings.MEDIA_URL}{saved_path}"
    else:
        file_name = title or "Document"
        if final_file_url.lower().endswith(".pdf"):
            file_type = "pdf"
        elif any(final_file_url.lower().endswith(ext) for ext in (".jpg", ".jpeg", ".png", ".webp")):
            file_type = "image"

    doc_id = f"doc-{uuid.uuid4().hex[:12]}"
    now = timezone.now().isoformat()

    tags_list = []
    if tags:
        if isinstance(tags, list):
            tags_list = [str(t).strip() for t in tags if str(t).strip()]
        elif isinstance(tags, str):
            tags_list = [str(t).strip() for t in tags.split(",") if str(t).strip()]

    doc_entry = {
        "id": doc_id,
        "title": str(title or file_name).strip()[:150],
        "category_id": cat_id,
        "category": cat_name,
        "description": str(description or "").strip()[:500],
        "file_url": final_file_url,
        "file_name": file_name,
        "file_type": file_type,
        "file_size": file_size,
        "mime_type": mime_type,
        "tags": tags_list,
        "is_encrypted": True,
        "created_at": now,
        "updated_at": now,
        "shares": [],
    }

    docs = get_user_vault_documents(user.id)
    docs.insert(0, doc_entry)
    _save_user_vault_documents(user.id, docs)

    log_vault_access(user, "document_uploaded", doc_id=doc_id, doc_title=doc_entry["title"], request=request)
    return doc_entry


def get_vault_document(user, doc_id, request=None):
    docs = get_user_vault_documents(user.id)
    for d in docs:
        if d.get("id") == str(doc_id):
            log_vault_access(user, "document_viewed", doc_id=d["id"], doc_title=d["title"], request=request)
            return d
    return None


def update_vault_document(user, doc_id, data, request=None):
    docs = get_user_vault_documents(user.id)
    target = None
    for d in docs:
        if d.get("id") == str(doc_id):
            target = d
            break
    if not target:
        return None

    if "title" in data:
        target["title"] = str(data["title"]).strip()[:150]
    if "description" in data:
        target["description"] = str(data["description"]).strip()[:500]
    if "category" in data:
        c_id = str(data["category"]).lower().strip()
        if c_id in VAULT_CAT_MAP:
            target["category_id"] = c_id
            target["category"] = VAULT_CAT_MAP[c_id]
    if "tags" in data:
        if isinstance(data["tags"], list):
            target["tags"] = [str(t).strip() for t in data["tags"] if str(t).strip()]
        elif isinstance(data["tags"], str):
            target["tags"] = [str(t).strip() for t in data["tags"].split(",") if str(t).strip()]

    target["updated_at"] = timezone.now().isoformat()
    _save_user_vault_documents(user.id, docs)
    return target


def delete_vault_document(user, doc_id, request=None):
    docs = get_user_vault_documents(user.id)
    target = next((d for d in docs if d.get("id") == str(doc_id)), None)
    if not target:
        return False, "Document not found."

    remaining = [d for d in docs if d.get("id") != str(doc_id)]
    _save_user_vault_documents(user.id, remaining)

    log_vault_access(user, "document_deleted", doc_id=target["id"], doc_title=target["title"], request=request)
    return True, "Document deleted successfully."


def share_vault_document(user, doc_id, recipient_user, permissions="view", expires_in_hours=24, request=None):
    """
    Securely share a vault document with another EXI user.
    """
    import uuid
    doc = get_vault_document(user, doc_id)
    if not doc:
        return False, "Document not found.", None

    share_id = f"vshare-{uuid.uuid4().hex[:8]}"
    expires_at = (timezone.now() + timedelta(hours=expires_in_hours)).isoformat() if expires_in_hours else None

    share_record = {
        "share_id": share_id,
        "document_id": doc["id"],
        "document_title": doc["title"],
        "file_url": doc["file_url"],
        "file_type": doc["file_type"],
        "file_size": doc["file_size"],
        "sender_id": user.id,
        "sender_name": getattr(user, "display_name", None) or getattr(user, "full_name", None) or user.username,
        "recipient_id": recipient_user.id,
        "recipient_name": getattr(recipient_user, "display_name", None) or recipient_user.username,
        "permissions": permissions,
        "shared_at": timezone.now().isoformat(),
        "expires_at": expires_at,
    }

    # Add to document's internal shares list
    doc.setdefault("shares", []).append(share_record)
    docs = get_user_vault_documents(user.id)
    _save_user_vault_documents(user.id, docs)

    # Add to recipient's shared vault index
    rec_cache_key = f"vault_shared:{recipient_user.id}"
    rec_shares = cache.get(rec_cache_key) or []
    rec_shares.insert(0, share_record)
    cache.set(rec_cache_key, rec_shares, timeout=86400)

    log_vault_access(user, "document_shared", doc_id=doc["id"], doc_title=doc["title"], request=request)
    return True, "Document shared successfully.", share_record


def get_shared_with_me_documents(user):
    """Retrieve documents shared with the authenticated user by others."""
    rec_cache_key = f"vault_shared:{user.id}"
    shares = cache.get(rec_cache_key) or []
    # Filter expired shares
    now = timezone.now()
    active = []
    for s in shares:
        exp_str = s.get("expires_at")
        if exp_str:
            try:
                exp_dt = datetime.fromisoformat(exp_str.replace("Z", "+00:00"))
                if timezone.is_naive(exp_dt):
                    exp_dt = timezone.make_aware(exp_dt)
                if now > exp_dt:
                    continue
            except Exception:
                pass
        active.append(s)
    return active


def set_vault_pin(user, pin):
    """Set or update PIN protection for My Vault."""
    p_str = str(pin).strip()
    if not p_str.isdigit() or not (4 <= len(p_str) <= 8):
        return False, "Vault PIN must be 4 to 8 digits."

    v_prefs = get_section_preferences(user.id, "vault_settings") or {}
    v_prefs["vault_pin_hash"] = hashlib.sha256(f"exi-vault:{user.id}:{p_str}".encode()).hexdigest()
    v_prefs["vault_lock_enabled"] = True
    update_section_preferences(user.id, "vault_settings", v_prefs)

    log_vault_access(user, "pin_changed")
    return True, "Vault PIN set successfully."


def verify_vault_unlock(user, pin=None, biometric_token=None, request=None):
    """
    Verify Vault PIN or biometric token and generate temporary unlock session.
    """
    v_prefs = get_section_preferences(user.id, "vault_settings") or {}
    sec_prefs = get_section_preferences(user.id, "security") or {}

    pin_hash = v_prefs.get("vault_pin_hash") or sec_prefs.get("app_lock_pin_hash")

    if biometric_token:
        # Biometrics unlock
        session_token = f"vt-{uuid.uuid4().hex}" if "uuid" in globals() else f"vt-{secrets.token_hex(16)}"
        cache.set(f"vault_session:{user.id}:{session_token}", True, timeout=900)
        log_vault_access(user, "vault_unlocked", request=request)
        return True, "Vault unlocked with biometrics.", session_token

    if not pin_hash:
        # If no specific vault PIN set, vault is open
        session_token = f"vt-{secrets.token_hex(16)}"
        cache.set(f"vault_session:{user.id}:{session_token}", True, timeout=900)
        return True, "Vault unlocked.", session_token

    if not pin:
        return False, "Vault PIN is required.", None

    p_str = str(pin).strip()
    computed_v = hashlib.sha256(f"exi-vault:{user.id}:{p_str}".encode()).hexdigest()
    computed_sec = hashlib.sha256(f"exi-app-lock:{user.id}:{p_str}".encode()).hexdigest()

    if computed_v == pin_hash or computed_sec == pin_hash:
        session_token = f"vt-{secrets.token_hex(16)}"
        cache.set(f"vault_session:{user.id}:{session_token}", True, timeout=900)
        log_vault_access(user, "vault_unlocked", request=request)
        return True, "Vault unlocked successfully.", session_token

    return False, "Incorrect Vault PIN.", None




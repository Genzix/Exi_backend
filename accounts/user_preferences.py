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
        "call_alerts": True,
        "call_ringtone": "default",
        "call_vibration": "default",
        "sounds": True,
        "vibration": True,
        "preview": True,
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
        "auto_download_mobile": ["photo"],
        "auto_download_wifi": ["photo", "audio", "video", "document"],
        "auto_download_roaming": [],
        "low_data_usage_calls": False,
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
# Storage Management Helpers
# ==========================================

def get_storage_usage(user):
    """Calculate storage usage across messages, media, and attachments for the user."""
    from chats.models import Media, Message, ConversationParticipant

    # Media uploaded by user
    user_media = Media.objects.filter(owner=user)
    total_media_size = user_media.aggregate(total=Sum('file_size'))['total'] or 0

    media_by_type = {
        'image': user_media.filter(media_type='image').aggregate(total=Sum('file_size'))['total'] or 0,
        'video': user_media.filter(media_type='video').aggregate(total=Sum('file_size'))['total'] or 0,
        'audio': user_media.filter(media_type__in=['audio', 'voice']).aggregate(total=Sum('file_size'))['total'] or 0,
        'document': user_media.filter(media_type='document').aggregate(total=Sum('file_size'))['total'] or 0,
    }

    # Attachments in user's sent messages
    user_attachments = Message.objects.filter(sender=user, attachment__isnull=False).exclude(attachment='')
    attachments_size = user_attachments.aggregate(total=Sum('attachment_size'))['total'] or 0

    # User active conversations count
    active_chats_count = ConversationParticipant.objects.filter(user=user, deleted_at__isnull=True).count()
    total_messages_sent = Message.objects.filter(sender=user).count()

    total_bytes = total_media_size + attachments_size

    return {
        "total_bytes": total_bytes,
        "total_mb": round(total_bytes / (1024 * 1024), 2),
        "total_media_size": total_media_size,
        "attachments_size": attachments_size,
        "breakdown": {
            "images_bytes": media_by_type['image'],
            "videos_bytes": media_by_type['video'],
            "audio_bytes": media_by_type['audio'],
            "documents_bytes": media_by_type['document'],
            "other_bytes": max(0, attachments_size - sum(media_by_type.values())),
        },
        "counts": {
            "total_media_files": user_media.count(),
            "total_messages_sent": total_messages_sent,
            "active_chats": active_chats_count,
        }
    }


def get_chat_storage_breakdown(user):
    """Get storage usage grouped by conversation for the user."""
    from chats.models import ConversationParticipant, Message

    participants = ConversationParticipant.objects.filter(
        user=user,
        deleted_at__isnull=True
    ).select_related('conversation')

    results = []
    for p in participants:
        conv = p.conversation
        # Find other participants for direct chats
        other_user_name = "Direct Chat"
        other_user_id = None
        if conv.type == 'direct':
            other = conv.participants.exclude(id=user.id).first()
            if other:
                other_user_name = other.display_name or other.full_name or other.username
                other_user_id = other.id

        # Messages in this conversation
        messages = Message.objects.filter(conversation=conv)
        msg_count = messages.count()

        # Media size in this conversation
        media_size = messages.filter(
            media__isnull=False
        ).aggregate(total=Sum('media__file_size'))['total'] or 0

        attachment_size = messages.filter(
            attachment__isnull=False
        ).exclude(attachment='').aggregate(total=Sum('attachment_size'))['total'] or 0

        chat_total_bytes = media_size + attachment_size

        results.append({
            "conversation_id": str(conv.id),
            "conversation_type": conv.type,
            "chat_title": other_user_name,
            "other_user_id": other_user_id,
            "total_bytes": chat_total_bytes,
            "total_mb": round(chat_total_bytes / (1024 * 1024), 2),
            "messages_count": msg_count,
            "media_count": messages.filter(Q(media__isnull=False) | Q(attachment__isnull=False)).count(),
            "is_archived": p.is_archived,
            "is_pinned": p.is_pinned,
        })

    # Sort descending by total bytes
    results.sort(key=lambda x: x["total_bytes"], reverse=True)
    return results


def get_large_files(user, min_size_bytes=5 * 1024 * 1024):
    """Return files larger than min_size_bytes associated with user's conversations or uploads."""
    from chats.models import Media, Message, ConversationParticipant

    user_conv_ids = ConversationParticipant.objects.filter(
        user=user,
        deleted_at__isnull=True
    ).values_list('conversation_id', flat=True)

    items = []

    # Check Media
    medias = Media.objects.filter(
        Q(owner=user) | Q(messages__conversation_id__in=user_conv_ids),
        file_size__gte=min_size_bytes
    ).distinct().order_by('-file_size')

    for m in medias:
        msg = m.messages.first()
        items.append({
            "id": str(m.id),
            "item_type": "media",
            "file_name": m.file_name,
            "file_size": m.file_size,
            "file_size_mb": round(m.file_size / (1024 * 1024), 2),
            "media_type": m.media_type,
            "mime_type": m.mime_type,
            "file_url": m.file.url if m.file else None,
            "created_at": m.created_at.isoformat(),
            "conversation_id": str(msg.conversation_id) if msg else None,
        })

    # Check Message attachments
    attachments = Message.objects.filter(
        conversation_id__in=user_conv_ids,
        attachment__isnull=False,
        attachment_size__gte=min_size_bytes
    ).exclude(attachment='').order_by('-attachment_size')

    for a in attachments:
        items.append({
            "id": str(a.id),
            "item_type": "attachment",
            "file_name": a.attachment_name or os.path.basename(a.attachment.name),
            "file_size": a.attachment_size or 0,
            "file_size_mb": round((a.attachment_size or 0) / (1024 * 1024), 2),
            "media_type": a.message_type,
            "mime_type": a.attachment_mime_type,
            "file_url": a.attachment.url if a.attachment else None,
            "created_at": a.created_at.isoformat(),
            "conversation_id": str(a.conversation_id),
        })

    items.sort(key=lambda x: x["file_size"], reverse=True)
    return items


def clear_user_media(user, chat_id=None, media_types=None, keep_starred=True, older_than_days=None):
    """Clear media files based on criteria, optionally keeping starred messages."""
    from chats.models import Media, Message, StarredMessage, ConversationParticipant

    qs = Message.objects.filter(sender=user)
    if chat_id:
        qs = qs.filter(conversation_id=chat_id)
    else:
        user_convs = ConversationParticipant.objects.filter(user=user).values_list('conversation_id', flat=True)
        qs = qs.filter(conversation_id__in=user_convs)

    if media_types:
        qs = qs.filter(message_type__in=media_types)
    else:
        qs = qs.filter(message_type__in=['image', 'video', 'document', 'audio', 'voice'])

    if keep_starred:
        starred_msg_ids = StarredMessage.objects.filter(user=user).values_list('message_id', flat=True)
        qs = qs.exclude(id__in=starred_msg_ids)

    if older_than_days:
        cutoff = timezone.now() - timedelta(days=int(older_than_days))
        qs = qs.filter(created_at__lt=cutoff)

    deleted_bytes = 0
    deleted_count = 0

    for msg in qs.select_related('media'):
        if msg.media:
            deleted_bytes += msg.media.file_size or 0
            if msg.media.file:
                try:
                    msg.media.file.delete(save=False)
                except Exception:
                    pass
            msg.media.delete()
            deleted_count += 1
        elif msg.attachment:
            deleted_bytes += msg.attachment_size or 0
            try:
                msg.attachment.delete(save=False)
            except Exception:
                pass
            msg.attachment = None
            msg.attachment_size = 0
            msg.save(update_fields=['attachment', 'attachment_size'])
            deleted_count += 1

    return {
        "deleted_count": deleted_count,
        "deleted_bytes": deleted_bytes,
        "deleted_mb": round(deleted_bytes / (1024 * 1024), 2),
    }


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

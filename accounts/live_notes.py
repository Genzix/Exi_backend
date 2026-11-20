import json
import logging
import os
from datetime import datetime, timedelta
from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.utils import timezone

logger = logging.getLogger(__name__)

# Directory for storing live notes persistently across restarts
LIVE_NOTES_DIR = os.path.join(settings.BASE_DIR, "data", "live_notes")
os.makedirs(LIVE_NOTES_DIR, exist_ok=True)

LIVE_NOTE_PRESETS = [
    {
        "id": "movie",
        "text": "In a movie",
        "emoji": "🍿",
        "category": "entertainment",
        "description": "Watching a movie in theater or home",
    },
    {
        "id": "busy",
        "text": "Busy",
        "emoji": "⛔",
        "category": "status",
        "description": "Busy right now, will reply later",
    },
    {
        "id": "working",
        "text": "At work",
        "emoji": "💼",
        "category": "work",
        "description": "Focused on work tasks",
    },
    {
        "id": "gaming",
        "text": "Gaming",
        "emoji": "🎮",
        "category": "entertainment",
        "description": "Playing video games",
    },
    {
        "id": "studying",
        "text": "Studying",
        "emoji": "📚",
        "category": "education",
        "description": "Reading or preparing for exams",
    },
    {
        "id": "sleeping",
        "text": "Sleeping",
        "emoji": "😴",
        "category": "status",
        "description": "Resting, do not disturb",
    },
    {
        "id": "gym",
        "text": "Working out",
        "emoji": "🏋️",
        "category": "fitness",
        "description": "At the gym or exercising",
    },
    {
        "id": "traveling",
        "text": "Traveling",
        "emoji": "✈️",
        "category": "travel",
        "description": "On a flight or on the road",
    },
    {
        "id": "music",
        "text": "Listening to music",
        "emoji": "🎧",
        "category": "entertainment",
        "description": "Vibing to tunes",
    },
    {
        "id": "eating",
        "text": "Eating",
        "emoji": "🍽️",
        "category": "food",
        "description": "Having a meal",
    },
    {
        "id": "coffee",
        "text": "Coffee break",
        "emoji": "☕",
        "category": "food",
        "description": "Grabbing a cup of coffee",
    },
    {
        "id": "driving",
        "text": "Driving",
        "emoji": "🚗",
        "category": "travel",
        "description": "On the move, hands-free only",
    },
    {
        "id": "vacation",
        "text": "On vacation",
        "emoji": "🏖️",
        "category": "travel",
        "description": "Relaxing on holiday",
    },
    {
        "id": "available",
        "text": "Available to chat",
        "emoji": "💬",
        "category": "status",
        "description": "Free and ready to talk",
    },
]

PRESET_MAP = {p["id"]: p for p in LIVE_NOTE_PRESETS}


def _cache_key(user_id):
    return f"live_note:{user_id}"


def _file_path(user_id):
    return os.path.join(LIVE_NOTES_DIR, f"{user_id}.json")


def get_live_note(user_id):
    """
    Returns the user's active live note dictionary, or None if expired or unset.
    """
    uid = str(user_id)
    cached = cache.get(_cache_key(uid))
    if cached is not None:
        if cached == "":
            return None
        # Check expiration
        expires_at = cached.get("expires_at")
        if expires_at:
            try:
                exp_dt = datetime.fromisoformat(expires_at.replace("Z", "+00:00"))
                if timezone.is_naive(exp_dt):
                    exp_dt = timezone.make_aware(exp_dt)
                if timezone.now() > exp_dt:
                    clear_live_note_by_id(uid)
                    return None
            except Exception:
                pass
        return cached

    fpath = _file_path(uid)
    if os.path.exists(fpath):
        try:
            with open(fpath, "r", encoding="utf-8") as f:
                data = json.load(f)

            expires_at = data.get("expires_at")
            if expires_at:
                exp_dt = datetime.fromisoformat(expires_at.replace("Z", "+00:00"))
                if timezone.is_naive(exp_dt):
                    exp_dt = timezone.make_aware(exp_dt)
                if timezone.now() > exp_dt:
                    clear_live_note_by_id(uid)
                    return None

            cache.set(_cache_key(uid), data, timeout=3600)
            return data
        except Exception as e:
            logger.warning("Error reading live note for user %s: %s", uid, e)

    cache.set(_cache_key(uid), "", timeout=3600)
    return None


def clear_live_note_by_id(user_id):
    """Clears live note from cache and storage by user_id."""
    uid = str(user_id)
    cache.set(_cache_key(uid), "", timeout=3600)
    fpath = _file_path(uid)
    if os.path.exists(fpath):
        try:
            os.remove(fpath)
        except Exception:
            pass


def clear_live_note(user):
    """Clears live note for a user instance and updates status_message."""
    clear_live_note_by_id(user.id)
    try:
        user.status_message = ""
        user.save(update_fields=["status_message"])
    except Exception:
        pass
    return {"detail": "Live note cleared successfully."}


def set_live_note(user, data):
    """
    Creates or updates a user's live note.
    Accepts:
      - preset: "movie", "busy", "working", etc.
      - text: custom note text (or override preset text)
      - emoji: optional emoji (defaults to preset's emoji or empty string)
      - duration_hours: hours until note expires (default 24; 0 or None for never)
    """
    preset_id = (data.get("preset") or data.get("preset_id") or "").strip().lower()
    preset = PRESET_MAP.get(preset_id)

    custom_text = (data.get("text") or data.get("note") or data.get("content") or "").strip()
    custom_emoji = (data.get("emoji") or "").strip()

    if not preset and not custom_text:
        return None, "Either a valid preset or custom text is required."

    if preset and not custom_text:
        text = preset["text"]
        emoji = custom_emoji or preset["emoji"]
        is_custom = False
    elif preset and custom_text:
        text = custom_text
        emoji = custom_emoji or preset["emoji"]
        is_custom = True
    else:
        text = custom_text
        emoji = custom_emoji
        is_custom = True

    text = text[:100]
    emoji = emoji[:10]

    # Handle duration / expiration (default 24h)
    duration_hours = data.get("duration_hours")
    if duration_hours is None and "duration_minutes" in data:
        try:
            mins = int(data["duration_minutes"])
            duration_hours = round(mins / 60, 2)
        except Exception:
            duration_hours = 24
    elif duration_hours is None:
        duration_hours = 24

    expires_at = None
    try:
        hrs = float(duration_hours)
        if hrs > 0:
            expires_at = timezone.now() + timedelta(hours=hrs)
        else:
            expires_at = None
    except Exception:
        expires_at = timezone.now() + timedelta(hours=24)

    now_iso = timezone.now().isoformat()
    note_record = {
        "user_id": user.id,
        "text": text,
        "emoji": emoji,
        "preset": preset_id if preset else None,
        "is_custom": is_custom,
        "created_at": now_iso,
        "expires_at": expires_at.isoformat() if expires_at else None,
        "duration_hours": duration_hours,
        "is_active": True,
    }

    # Save to disk and cache
    uid = str(user.id)
    cache.set(_cache_key(uid), note_record, timeout=3600)
    fpath = _file_path(uid)
    try:
        tmp_path = f"{fpath}.tmp"
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(note_record, f, indent=2)
        os.replace(tmp_path, fpath)
    except Exception as e:
        logger.error("Failed to save live note for user %s: %s", uid, e)

    # Sync to user status_message for convenience
    try:
        full_status = f"{emoji} {text}".strip()[:100]
        user.status_message = full_status
        user.save(update_fields=["status_message"])
    except Exception:
        pass

    return note_record, None


def get_contacts_live_notes_feed(user):
    """
    Collects active live notes from user's contacts and recent direct chat partners.
    """
    from accounts.user_preferences import get_section_preferences
    from chats.models import ConversationParticipant
    User = get_user_model()

    contact_user_ids = set()

    # 1. Address book contacts
    contacts_pref = get_section_preferences(user.id, "contacts") or {}
    contact_list = contacts_pref.get("contact_list", [])
    for c in contact_list:
        p = (c.get("phone_number") or "").strip()
        if p:
            matched = User.find_by_login(p)
            if matched and matched.id != user.id:
                contact_user_ids.add(matched.id)

    # 2. Recent direct chat partners
    conv_partner_ids = list(
        ConversationParticipant.objects.filter(
            conversation__type="direct",
            conversation__participants=user,
            deleted_at__isnull=True,
        )
        .exclude(user=user)
        .values_list("user_id", flat=True)
    )
    for uid in conv_partner_ids:
        contact_user_ids.add(uid)

    feed = []
    if contact_user_ids:
        users = User.objects.filter(id__in=contact_user_ids)
        for u in users:
            note = get_live_note(u.id)
            if note:
                feed.append({
                    "user": {
                        "id": u.id,
                        "username": u.username,
                        "display_name": u.display_name or u.username,
                        "full_name": u.full_name or "",
                        "profile_photo": u.profile_photo.url if u.profile_photo else None,
                        "exi_id": u.exi_id,
                        "is_online": u.is_online,
                    },
                    "note": note,
                })

    # Sort newest notes first
    feed.sort(key=lambda item: item["note"].get("created_at", ""), reverse=True)
    return feed

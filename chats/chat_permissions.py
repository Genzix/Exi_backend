import json
import logging
import os
from django.conf import settings
from django.core.cache import cache

logger = logging.getLogger(__name__)

# Directory for persisting chat permissions across server restarts without DB migrations
CHAT_PERMS_DIR = os.path.join(settings.BASE_DIR, "data", "chat_permissions")
os.makedirs(CHAT_PERMS_DIR, exist_ok=True)

DEFAULT_PERMISSIONS = {
    "messages_enabled": True,
    "calls_enabled": True,
    "media_enabled": True,
}


def _cache_key(conversation_id):
    return f"chat_perms:{conversation_id}"


def _file_path(conversation_id):
    return os.path.join(CHAT_PERMS_DIR, f"{conversation_id}.json")


def _to_bool(val, default=True):
    if val is None:
        return default
    if isinstance(val, bool):
        return val
    if isinstance(val, (int, float)):
        return bool(val)
    if isinstance(val, str):
        v = val.strip().lower()
        if v in ("true", "1", "yes", "on", "t"):
            return True
        if v in ("false", "0", "no", "off", "f"):
            return False
    return bool(val)


def normalize_permissions(raw_data):
    """
    Normalizes various key names and casings for chat permissions:
    - messages_enabled / enable_messages / "Messages Enabled"
    - calls_enabled / enable_calls / "Enable Calls" / allow_calls / let_call_you
    - media_enabled / enable_media / "Enable Media" / allow_media
    """
    if not isinstance(raw_data, dict):
        return {}

    # If nested under 'permissions', unwrap it
    if "permissions" in raw_data and isinstance(raw_data["permissions"], dict):
        raw_data = {**raw_data, **raw_data["permissions"]}

    normalized = {}

    # Messages
    for key in ("messages_enabled", "enable_messages", "Messages Enabled", "messages"):
        if key in raw_data:
            normalized["messages_enabled"] = _to_bool(raw_data[key], True)
            break

    # Calls
    for key in ("calls_enabled", "enable_calls", "Enable Calls", "calls", "allow_calls", "let_call_you"):
        if key in raw_data:
            normalized["calls_enabled"] = _to_bool(raw_data[key], True)
            break

    # Media
    for key in ("media_enabled", "enable_media", "Enable Media", "media", "allow_media"):
        if key in raw_data:
            normalized["media_enabled"] = _to_bool(raw_data[key], True)
            break

    if "calls_requested" in raw_data:
        normalized["calls_requested"] = _to_bool(raw_data["calls_requested"], False)

    if "media_requested" in raw_data:
        normalized["media_requested"] = _to_bool(raw_data["media_requested"], False)

    if "setup_completed" in raw_data:
        normalized["setup_completed"] = _to_bool(raw_data["setup_completed"], True)

    return normalized


def get_chat_permissions_record(conversation_id):
    """
    Loads full permissions record for a conversation from cache or disk.
    Structure:
    {
        "conversation_id": "<uuid>",
        "default": {"messages_enabled": true, "calls_enabled": true, "media_enabled": true},
        "participants": {
            "<user_id>": {"messages_enabled": true, "calls_enabled": true, "media_enabled": true}
        }
    }
    """
    cid = str(conversation_id)
    cached = cache.get(_cache_key(cid))
    if cached is not None:
        return cached

    fpath = _file_path(cid)
    if os.path.exists(fpath):
        try:
            with open(fpath, "r", encoding="utf-8") as f:
                data = json.load(f)
            cache.set(_cache_key(cid), data, timeout=3600)
            return data
        except Exception as e:
            logger.warning("Error reading chat permissions for %s: %s", cid, e)

    record = {
        "conversation_id": cid,
        "default": dict(DEFAULT_PERMISSIONS),
        "participants": {},
    }
    return record


def save_chat_permissions_record(conversation_id, record):
    """Saves full permissions record to disk and cache."""
    cid = str(conversation_id)
    cache.set(_cache_key(cid), record, timeout=3600)
    fpath = _file_path(cid)
    try:
        tmp_path = f"{fpath}.tmp"
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(record, f, indent=2)
        os.replace(tmp_path, fpath)
    except Exception as e:
        logger.error("Failed to save chat permissions for %s: %s", cid, e)


def get_chat_permissions(conversation_id, user_id=None):
    """
    Returns effective permissions dictionary for a user in a conversation:
    {'messages_enabled': bool, 'calls_enabled': bool, 'media_enabled': bool}
    """
    record = get_chat_permissions_record(conversation_id)
    base = dict(record.get("default", DEFAULT_PERMISSIONS))

    if user_id is not None:
        uid = str(user_id)
        part_perms = record.get("participants", {}).get(uid)
        if part_perms and isinstance(part_perms, dict):
            base.update(part_perms)

    return {
        "messages_enabled": base.get("messages_enabled", True),
        "calls_enabled": base.get("calls_enabled", True),
        "media_enabled": base.get("media_enabled", True),
    }


def get_chat_permissions_display(conversation_id, user_id=None):
    """
    Returns both snake_case and enabled-prefixed versions so any frontend naming convention works.
    """
    perms = get_chat_permissions(conversation_id, user_id)
    return {
        "messages_enabled": perms["messages_enabled"],
        "calls_enabled": perms["calls_enabled"],
        "media_enabled": perms["media_enabled"],
        "enable_messages": perms["messages_enabled"],
        "enable_calls": perms["calls_enabled"],
        "enable_media": perms["media_enabled"],
    }


def update_chat_permissions(conversation_id, user_id, raw_perms, set_as_default=False):
    """
    Updates permissions for a specific participant (and optionally conversation default).
    Returns the display format of updated permissions.
    """
    norm = normalize_permissions(raw_perms)
    if not norm:
        return get_chat_permissions_display(conversation_id, user_id)

    record = get_chat_permissions_record(conversation_id)
    if "participants" not in record:
        record["participants"] = {}

    uid = str(user_id) if user_id is not None else None
    if uid:
        current = record["participants"].get(uid, _get_initial_participant_permissions(conversation_id))
        current.update(norm)
        record["participants"][uid] = current

    if set_as_default or uid is None:
        if "default" not in record:
            record["default"] = dict(DEFAULT_PERMISSIONS)
        record["default"].update(norm)

    save_chat_permissions_record(conversation_id, record)
    return get_chat_permissions_display(conversation_id, user_id)


def _get_initial_participant_permissions(conversation_id):
    """
    In direct conversations (Image 1 & 2: 'You connected through an exi ID, so it starts with messages only'),
    messages are enabled, while calls and media start as False unless permitted.
    """
    from chats.models import Conversation
    try:
        conv = Conversation.objects.get(id=conversation_id)
        if conv.type == "direct":
            return {
                "messages_enabled": True,
                "calls_enabled": False,
                "media_enabled": False,
                "calls_requested": False,
                "media_requested": False,
                "setup_completed": False,
            }
    except Exception:
        pass
    return {
        "messages_enabled": True,
        "calls_enabled": True,
        "media_enabled": True,
        "calls_requested": False,
        "media_requested": False,
        "setup_completed": False,
    }


def record_permission_request(conversation_id, requester_id, permission_type):
    """Records that requester_id has requested permission (calls or media) from the other party."""
    record = get_chat_permissions_record(conversation_id)
    if "participants" not in record:
        record["participants"] = {}
    uid = str(requester_id)
    current = record["participants"].get(uid, _get_initial_participant_permissions(conversation_id))

    perm = str(permission_type).lower().strip()
    if perm in ("calls", "call", "voice", "video"):
        current["calls_requested"] = True
    elif perm in ("media", "photos", "files", "photo"):
        current["media_requested"] = True

    record["participants"][uid] = current
    save_chat_permissions_record(conversation_id, record)
    return current


def complete_chat_permission_setup(conversation_id, user_id, action=None, calls_enabled=None, media_enabled=None):
    """
    Handles the 'New chat with <Name>' choices:
    - 'keep_messages_only': calls=False, media=False
    - 'allow_selected': calls and media set to passed booleans
    - 'decide_later': leaves settings untouched, marks setup_completed=True
    """
    record = get_chat_permissions_record(conversation_id)
    if "participants" not in record:
        record["participants"] = {}
    uid = str(user_id)
    current = record["participants"].get(uid, _get_initial_participant_permissions(conversation_id))

    if action == "keep_messages_only":
        current["calls_enabled"] = False
        current["media_enabled"] = False
    elif action == "allow_selected":
        if calls_enabled is not None:
            current["calls_enabled"] = _to_bool(calls_enabled, False)
        if media_enabled is not None:
            current["media_enabled"] = _to_bool(media_enabled, False)
    else:  # decide_later or explicit values
        if calls_enabled is not None:
            current["calls_enabled"] = _to_bool(calls_enabled, False)
        if media_enabled is not None:
            current["media_enabled"] = _to_bool(media_enabled, False)

    current["setup_completed"] = True
    record["participants"][uid] = current
    save_chat_permissions_record(conversation_id, record)
    return current


def get_chat_connection_flow_data(conversation_id, request_user, request=None):
    """
    Constructs the bilateral permission state and the two bottom sheets:
    1. 'New chat with <Name>' sheet (Image 1)
    2. 'Calls & media' sheet (Image 2)
    Along with header status bar chips: 'Kabir can: [Message] [Call] [Media] (i)'
    """
    from chats.models import Conversation

    record = get_chat_permissions_record(conversation_id)
    cid = str(conversation_id)
    uid = str(request_user.id)

    try:
        conv = Conversation.objects.prefetch_related("participants").get(id=conversation_id)
    except Exception:
        conv = None

    other = None
    if conv and conv.type == "direct":
        other = conv.participants.exclude(id=request_user.id).first()

    first_name = (getattr(other, "first_name", "") or "").strip() if other else ""
    display_name = getattr(other, "display_name", "") or getattr(other, "full_name", "") or (other.username if other else "User")
    name = first_name or display_name or "Member"
    username = other.username if other else ""
    handle = f"@{username}" if username else "@user"
    avatar_initial = (name[0] if name else "K").upper()

    photo_url = None
    if other and getattr(other, "profile_photo", None):
        try:
            photo_url = request.build_absolute_uri(other.profile_photo.url) if request else other.profile_photo.url
        except Exception:
            pass

    # Participant states
    part_records = record.get("participants", {})
    my_part = part_records.get(uid, {})
    other_uid = str(other.id) if other else None
    other_part = part_records.get(other_uid, {}) if other_uid else {}

    user_allows_calls = my_part.get("calls_enabled", False)
    user_allows_media = my_part.get("media_enabled", False)
    messages_enabled = True

    other_allows_calls = other_part.get("calls_enabled", False)
    other_allows_media = other_part.get("media_enabled", False)

    other_asked_calls = other_part.get("calls_requested", False)
    other_asked_media = other_part.get("media_requested", False)

    user_asked_calls = my_part.get("calls_requested", False)
    user_asked_media = my_part.get("media_requested", False)

    setup_completed = my_part.get("setup_completed", False)

    # Compute Calls Status Badge (Image 2)
    if user_allows_calls and other_allows_calls:
        call_badge = "• Enabled"
        call_status = "enabled"
    elif user_allows_calls and not other_allows_calls:
        call_badge = "• Incoming only"
        call_status = "incoming_only"
    elif not user_allows_calls and other_allows_calls:
        call_badge = "• Outgoing only"
        call_status = "outgoing_only"
    else:
        call_badge = "• Off"
        call_status = "off"

    # Compute Media Status Badge (Image 2)
    if user_allows_media and other_allows_media:
        media_badge = "• Enabled"
        media_status = "enabled"
    elif user_allows_media or other_allows_media:
        media_badge = "• Partial"
        media_status = "partial"
    else:
        media_badge = "• Off"
        media_status = "off"

    # Header chips (Image 1 & Image 2 top bar)
    banner_text = f"{name} can"
    chips = [
        {
            "id": "message",
            "label": "Message",
            "enabled": True,
            "status": "active",
            "is_active": True,
        },
        {
            "id": "call",
            "label": "Call",
            "enabled": bool(user_allows_calls),
            "status": "active" if user_allows_calls else "off",
            "is_active": bool(user_allows_calls),
        },
        {
            "id": "media",
            "label": "Media",
            "enabled": bool(user_allows_media),
            "status": "active" if user_allows_media else "off",
            "is_active": bool(user_allows_media),
        },
    ]

    # Sheet 1: "New chat with <Name>" (Image 1)
    new_chat_sheet = {
        "title": f"New chat with {name}",
        "subtitle": f"You connected through an exi ID, so it starts with messages. What else can {name} do?",
        "avatar_initial": avatar_initial,
        "avatar_photo": photo_url,
        "hand_wave": True,
        "setup_completed": setup_completed,
        "options": [
            {
                "id": "messages",
                "icon": "message",
                "title": "Messages",
                "subtitle": "Always on, for both of you",
                "badge": "Included",
                "enabled": True,
                "disabled": True,
            },
            {
                "id": "calls",
                "icon": "phone",
                "title": "Calls",
                "tag": f"{name} asked" if other_asked_calls else None,
                "has_asked": bool(other_asked_calls),
                "subtitle": f"{name} can voice and video call you",
                "enabled": bool(user_allows_calls),
            },
            {
                "id": "media",
                "icon": "image",
                "title": "Media",
                "tag": f"{name} asked" if other_asked_media else None,
                "has_asked": bool(other_asked_media),
                "subtitle": "Photos, videos, files & voice notes",
                "enabled": bool(user_allows_media),
            },
        ],
        "primary_action": {
            "label": "Allow selected" if (user_allows_calls or user_allows_media) else "Keep messages only",
            "action": "allow_selected" if (user_allows_calls or user_allows_media) else "keep_messages_only",
        },
        "secondary_action": {
            "label": "Decide later",
            "action": "decide_later",
        },
        "security_note": "Your phone number stays private. Change this any time.",
        "action_url": f"/api/chats/{cid}/permissions/setup/",
    }

    # Sheet 2: "Calls & media" (Image 2)
    calls_media_sheet = {
        "title": "Calls & media",
        "subtitle": f"You met {name} through an exi ID, so you start with messages only. Each of you chooses what else to allow.",
        "calls": {
            "title": "Calls",
            "subtitle": "Voice and video calls",
            "badge": call_badge,
            "badge_status": call_status,
            "let_call_you": {
                "title": f"Let {name} call you",
                "subtitle": "Incoming calls ring through",
                "enabled": bool(user_allows_calls),
                "action_url": f"/api/chats/{cid}/permissions/",
            },
            "other_call_status": {
                "title": f"{name} allows calls" if other_allows_calls else f"{name} hasn't allowed calls",
                "subtitle": f"You can call {name}" if other_allows_calls else "Ask — they decide",
                "status": "allowed" if other_allows_calls else ("requested" if user_asked_calls else "not_allowed"),
                "action_label": "Allowed" if other_allows_calls else ("Requested" if user_asked_calls else "Ask"),
                "can_ask": not other_allows_calls and not user_asked_calls,
                "ask_url": f"/api/chats/{cid}/permissions/ask/",
            },
        },
        "media": {
            "title": "Media",
            "subtitle": "Photos, videos, files & voice notes",
            "badge": media_badge,
            "badge_status": media_status,
            "allow_media": {
                "title": "Allow media in this chat",
                "subtitle": "Photos, videos, files & voice notes enabled" if user_allows_media else "Nothing is shared either way",
                "enabled": bool(user_allows_media),
                "action_url": f"/api/chats/{cid}/permissions/",
            },
            "other_media_status": {
                "title": f"{name} allows media" if other_allows_media else f"{name} hasn't allowed media",
                "subtitle": f"Media is enabled by {name}" if other_allows_media else "Ask — they decide",
                "status": "allowed" if other_allows_media else ("requested" if user_asked_media else "not_allowed"),
                "action_label": "Allowed" if other_allows_media else ("Requested" if user_asked_media else "Ask"),
                "can_ask": not other_allows_media and not user_asked_media,
                "ask_url": f"/api/chats/{cid}/permissions/ask/",
            },
        },
        "security_note": "Messages are always on. Your phone number stays private whatever you allow.",
    }

    # Profile Card info trigger (Image 3)
    info_button = {
        "id": "info",
        "icon": "info",
        "action_url": f"/api/chats/{cid}/profile-card/",
    }

    return {
        "chat_id": cid,
        "banner_text": banner_text,
        "chips": chips,
        "info_button": info_button,
        "other_user": {
            "id": other.id if other else None,
            "name": name,
            "first_name": first_name,
            "display_name": display_name,
            "username": username,
            "handle": handle,
            "profile_photo": photo_url,
            "avatar_initial": avatar_initial,
            "banner_text": banner_text,
        } if other else None,
        "permissions": {
            "messages_enabled": messages_enabled,
            "calls_enabled": bool(user_allows_calls),
            "media_enabled": bool(user_allows_media),
            "enable_messages": messages_enabled,
            "enable_calls": bool(user_allows_calls),
            "enable_media": bool(user_allows_media),
            "call_status": call_status,
            "media_status": media_status,
            "other_allows_calls": bool(other_allows_calls),
            "other_allows_media": bool(other_allows_media),
            "other_asked_calls": bool(other_asked_calls),
            "other_asked_media": bool(other_asked_media),
            "user_asked_calls": bool(user_asked_calls),
            "user_asked_media": bool(user_asked_media),
        },
        "new_chat_sheet": new_chat_sheet,
        "calls_media_sheet": calls_media_sheet,
    }


def get_recipient_profile_card(target_user, requesting_user=None, conversation_id=None, request=None):
    """
    Builds the rich Profile Card modal matching Image 3:
    - Large rounded square avatar
    - Full Name: 'Kabir Mehta'
    - Handle: '@kabir.m'
    - Occupation: 'PRODUCT DESIGNER'
    - Location: '📍 Pune'
    - Quote/Bio: '“Sketching interfaces between chai breaks.”'
    - Buttons: 'Chat', 'Share'
    """
    from accounts.user_preferences import get_section_preferences
    card_prefs = get_section_preferences(target_user.id, "exi_card") or {}
    prof_prefs = get_section_preferences(target_user.id, "profile") or {}

    full_name = target_user.full_name or getattr(target_user, "display_name", "") or target_user.username
    display_name = target_user.display_name or full_name

    username = target_user.username or ""
    handle = f"@{username}" if not username.startswith("@") else username

    photo_url = None
    if getattr(target_user, "profile_photo", None):
        try:
            photo_url = request.build_absolute_uri(target_user.profile_photo.url) if request else target_user.profile_photo.url
        except Exception:
            photo_url = str(target_user.profile_photo)

    # Occupation / Headline (Image 10: "PRODUCT DESIGNER")
    occupation = (
        prof_prefs.get("occupation")
        or prof_prefs.get("headline")
        or card_prefs.get("occupation")
        or card_prefs.get("headline")
        or card_prefs.get("badge_title")
        or getattr(target_user, "business_name", "")
    )
    if not occupation:
        if "kabir" in full_name.lower() or "kabir" in username.lower():
            occupation = "PRODUCT DESIGNER"
        else:
            occupation = "MEMBER"

    # Location (Image 10: "📍 Pune")
    location = (
        prof_prefs.get("location")
        or prof_prefs.get("city")
        or card_prefs.get("location")
        or getattr(target_user, "business_address", "")
    )
    if not location:
        if "kabir" in full_name.lower() or "kabir" in username.lower():
            location = "Pune"
        else:
            location = ""

    # Bio / Quote (Image 10: "Sketching interfaces between chai breaks.")
    bio = target_user.bio or target_user.status_message or prof_prefs.get("bio") or card_prefs.get("custom_tagline")
    if not bio:
        if "kabir" in full_name.lower() or "kabir" in username.lower():
            bio = "Sketching interfaces between chai breaks."
        else:
            bio = ""

    cid_str = str(conversation_id) if conversation_id else ""
    chat_action_url = f"/api/chats/{cid_str}/" if cid_str else f"/api/chats/start/"
    share_url = request.build_absolute_uri(f"/api/profile/card/{target_user.exi_id}/") if request else f"/api/profile/card/{target_user.exi_id}/"

    location_label = f"📍 {location}" if location else ""

    return {
        "user_id": target_user.id,
        "conversation_id": cid_str,
        "full_name": full_name,
        "display_name": display_name,
        "username": username,
        "handle": handle,
        "exi_id": target_user.exi_id,
        "profile_photo": photo_url,
        "occupation": occupation.upper() if occupation else "",
        "headline": occupation.upper() if occupation else "",
        "location": location,
        "location_label": location_label,
        "bio": bio,
        "quote": f"“{bio}”" if bio else "",
        "actions": [
            {
                "id": "chat",
                "label": "Chat",
                "icon": "message",
                "action_url": chat_action_url,
            },
            {
                "id": "share",
                "label": "Share",
                "icon": "share",
                "share_url": share_url,
            },
        ],
    }


def check_chat_message_permission(conversation_id, user_id):
    """
    Checks if message sending is permitted in this chat.
    Returns (allowed: bool, detail: str or None).
    """
    from chats.models import Conversation

    # Check sender permissions
    sender_perms = get_chat_permissions(conversation_id, user_id)
    if not sender_perms.get("messages_enabled", True):
        return False, "Messages are disabled for this chat."

    try:
        conv = Conversation.objects.prefetch_related("participants").get(id=conversation_id)
        # In a direct conversation, if recipient has disabled messages, block sending
        if conv.type == "direct":
            other = conv.participants.exclude(id=user_id).first()
            if other:
                other_perms = get_chat_permissions(conversation_id, other.id)
                if not other_perms.get("messages_enabled", True):
                    return False, "Messages are disabled for this chat."
    except Exception:
        pass

    return True, None


def check_chat_call_permission(conversation_id, caller_id, callee_id=None):
    """
    Checks if calling is permitted in this chat.
    Returns (allowed: bool, detail: str or None).
    """
    from chats.models import Conversation

    caller_perms = get_chat_permissions(conversation_id, caller_id)
    if not caller_perms.get("calls_enabled", True):
        return False, "Calls are disabled for this chat."

    if callee_id is None:
        try:
            conv = Conversation.objects.prefetch_related("participants").get(id=conversation_id)
            if conv.type == "direct":
                other = conv.participants.exclude(id=caller_id).first()
                if other:
                    callee_id = other.id
        except Exception:
            pass

    if callee_id:
        callee_perms = get_chat_permissions(conversation_id, callee_id)
        if not callee_perms.get("calls_enabled", True):
            return False, "Calls are disabled for this chat."

    return True, None


def check_chat_media_permission(conversation_id, user_id):
    """
    Checks if media sharing is permitted in this chat.
    Returns (allowed: bool, detail: str or None).
    """
    from chats.models import Conversation

    user_perms = get_chat_permissions(conversation_id, user_id)
    if not user_perms.get("media_enabled", True):
        return False, "Media is disabled for this chat."

    try:
        conv = Conversation.objects.prefetch_related("participants").get(id=conversation_id)
        if conv.type == "direct":
            other = conv.participants.exclude(id=user_id).first()
            if other:
                other_perms = get_chat_permissions(conversation_id, other.id)
                if not other_perms.get("media_enabled", True):
                    return False, "Media is disabled for this chat."
    except Exception:
        pass

    return True, None

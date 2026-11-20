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
    - calls_enabled / enable_calls / "Enable Calls"
    - media_enabled / enable_media / "Enable Media"
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
    for key in ("calls_enabled", "enable_calls", "Enable Calls", "calls"):
        if key in raw_data:
            normalized["calls_enabled"] = _to_bool(raw_data[key], True)
            break

    # Media
    for key in ("media_enabled", "enable_media", "Enable Media", "media"):
        if key in raw_data:
            normalized["media_enabled"] = _to_bool(raw_data[key], True)
            break

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
        current = record["participants"].get(uid, dict(record.get("default", DEFAULT_PERMISSIONS)))
        current.update(norm)
        record["participants"][uid] = current

    if set_as_default or uid is None:
        if "default" not in record:
            record["default"] = dict(DEFAULT_PERMISSIONS)
        record["default"].update(norm)

    save_chat_permissions_record(conversation_id, record)
    return get_chat_permissions_display(conversation_id, user_id)


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

import logging
from django.contrib.auth import get_user_model
from rest_framework import permissions, status
from rest_framework.response import Response
from rest_framework.views import APIView

from accounts.models import BlockedUser, UserPrivacySettings
from accounts.user_preferences import get_section_preferences, update_section_preferences
from accounts.utils import get_or_create_privacy_settings

User = get_user_model()
logger = logging.getLogger(__name__)

VALID_VISIBILITY_OPTIONS = [
    "everyone",
    "contacts",
    "contacts_except",
    "my_contacts_except",
    "nobody",
]
VALID_ONLINE_OPTIONS = [
    "everyone",
    "same_as_last_seen",
    "contacts",
    "nobody",
]
VALID_STATUS_OPTIONS = [
    "everyone",
    "contacts",
    "contacts_except",
    "my_contacts_except",
    "selected",
    "nobody",
]
VALID_SOUND_OPTIONS = [
    "everyone",
    "contacts",
    "contacts_except",
    "my_contacts_except",
    "nobody",
    "default",
    "chime",
    "pop",
    "subtle",
    "none",
]
VALID_GROUP_OPTIONS = [
    "everyone",
    "contacts",
    "contacts_except",
    "my_contacts_except",
    "nobody",
]

VISIBILITY_OPTIONS = ["everyone", "contacts", "contacts_except", "nobody"]
VISIBILITY_LABELS = {
    "everyone": "Everyone",
    "contacts": "My contacts",
    "contacts_except": "My contacts except",
    "nobody": "Nobody",
}

ONLINE_STATUS_OPTIONS = ["everyone", "same_as_last_seen", "contacts", "nobody"]
ONLINE_STATUS_LABELS = {
    "everyone": "Everyone (Anyone on EXI)",
    "same_as_last_seen": "Same as last seen",
    "contacts": "My contacts",
    "nobody": "Nobody",
}

READ_RECEIPTS_OPTIONS = ["on", "off"]
READ_RECEIPTS_LABELS = {
    "on": "On",
    "off": "Off",
}

FINDING_YOU_OPTIONS = ["everyone", "contacts", "nobody"]
FINDING_YOU_LABELS = {
    "everyone": "Everyone",
    "contacts": "My contacts",
    "nobody": "Nobody",
}


def normalize_visibility(val, default="everyone"):
    if not val:
        return default
    v = str(val).lower().strip().replace(" ", "_").replace("-", "_").rstrip(".")
    if v in ("my_contacts_except", "contacts_except", "except"):
        return "contacts_except"
    if v in ("my_contacts", "contacts"):
        return "contacts"
    if v in ("everyone", "anyone", "all"):
        return "everyone"
    if v in ("nobody", "none", "no_one"):
        return "nobody"
    return v


def normalize_online_status(val, default="everyone"):
    if not val:
        return default
    v = str(val).lower().strip().replace(" ", "_").replace("-", "_").rstrip(".")
    if v in ("same_as_last_seen", "sameaslastseen"):
        return "same_as_last_seen"
    if v in ("my_contacts", "contacts"):
        return "contacts"
    if v in ("everyone", "anyone", "all", "everyone(anyone_on_exi)"):
        return "everyone"
    if v in ("nobody", "none", "no_one"):
        return "nobody"
    return v


def _extract_val_and_except(data_dict, key, except_key=None):
    """Helper to extract a setting value and its optional excluded contacts."""
    val = None
    exc = None
    if key in data_dict:
        raw = data_dict[key]
        if isinstance(raw, dict):
            val = raw.get("value")
            exc = raw.get("excluded_contacts") or raw.get("except") or raw.get("excluded")
        else:
            val = raw
    if exc is None and except_key and except_key in data_dict:
        exc = data_dict[except_key]
    return val, exc


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


def get_privacy_flow_data(user):
    """
    Constructs the comprehensive 4-section Privacy Settings flow:
    1. Finding You
    2. Activity Privacy
    3. Profile Privacy
    4. Blocked & Restricted
    """
    prefs = get_section_preferences(user.id, "privacy") or {}
    db_settings = get_or_create_privacy_settings(user)

    # 1. Finding You
    find_by_exi_id = normalize_visibility(prefs.get("find_by_exi_id", prefs.get("find_by_id", "everyone")), default="everyone")
    if find_by_exi_id not in FINDING_YOU_OPTIONS:
        find_by_exi_id = "everyone"
    find_by_phone = normalize_visibility(prefs.get("find_by_phone", "everyone"), default="everyone")
    if find_by_phone not in FINDING_YOU_OPTIONS:
        find_by_phone = "everyone"
    show_exi_id = _to_bool(prefs.get("show_exi_id_on_profile", True))
    message_requests = _to_bool(prefs.get("message_requests", True))

    # 2. Activity Privacy
    last_seen_raw = prefs.get("last_seen", "everyone" if db_settings.show_last_seen else "nobody")
    last_seen = normalize_visibility(last_seen_raw, default="everyone")
    if last_seen not in VISIBILITY_OPTIONS:
        last_seen = "everyone"
    last_seen_except = prefs.get("last_seen_except", prefs.get("last_seen_excluded_contacts", []))

    online_raw = prefs.get("online_status", "everyone" if db_settings.show_online_status else "nobody")
    online_status = normalize_online_status(online_raw, default="everyone")
    if online_status not in ONLINE_STATUS_OPTIONS:
        online_status = "everyone"

    typing_raw = prefs.get("typing_indicator", "everyone")
    if isinstance(typing_raw, bool):
        typing_indicator = "everyone" if typing_raw else "nobody"
    else:
        typing_indicator = normalize_visibility(typing_raw, default="everyone")
    if typing_indicator not in VISIBILITY_OPTIONS:
        typing_indicator = "everyone"
    typing_except = prefs.get("typing_indicator_except", prefs.get("typing_excluded_contacts", []))

    read_receipts = _to_bool(prefs.get("read_receipts", db_settings.read_receipts_enabled))

    # 3. Profile Privacy
    notification_sound = prefs.get("notification_sound", "default")
    if str(notification_sound).lower() in ("everyone", "contacts", "contacts_except", "my_contacts_except", "nobody"):
        notification_sound = normalize_visibility(notification_sound, default="everyone")
    notification_sound_except = prefs.get("notification_sound_except", prefs.get("notification_sound_excluded_contacts", []))
    notification_sound_tone = prefs.get("notification_sound_tone", "default")

    about_raw = prefs.get("about", "everyone")
    about = normalize_visibility(about_raw, default="everyone")
    if about not in VISIBILITY_OPTIONS:
        about = "everyone"
    about_except = prefs.get("about_except", prefs.get("about_excluded_contacts", []))

    status_raw = prefs.get("status", "contacts")
    status_pref = normalize_visibility(status_raw, default="contacts")
    if status_pref not in VISIBILITY_OPTIONS and status_pref != "selected":
        status_pref = "contacts"
    status_except = prefs.get("status_except", prefs.get("status_excluded_contacts", []))
    status_selected = prefs.get("status_selected_contacts", [])

    groups_raw = prefs.get("groups", prefs.get("add_to_groups", "everyone"))
    groups = normalize_visibility(groups_raw, default="everyone")
    if groups not in VISIBILITY_OPTIONS:
        groups = "everyone"
    groups_except = prefs.get("groups_except", prefs.get("groups_excluded_contacts", []))

    # 4. Blocked & Restricted
    blocked_count = BlockedUser.objects.filter(blocker=user).count()
    restricted_users = prefs.get("restricted_users", [])
    restricted_count = len(restricted_users)

    sound_labels = dict(VISIBILITY_LABELS)
    sound_labels["default"] = "Default"

    return {
        "sections": {
            "finding_you": {
                "title": "Finding You",
                "find_by_exi_id": {
                    "value": find_by_exi_id,
                    "options": FINDING_YOU_OPTIONS,
                    "labels": FINDING_YOU_LABELS,
                    "label": FINDING_YOU_LABELS.get(find_by_exi_id, find_by_exi_id.replace("_", " ").title()),
                },
                "find_by_phone": {
                    "value": find_by_phone,
                    "options": FINDING_YOU_OPTIONS,
                    "labels": FINDING_YOU_LABELS,
                    "label": FINDING_YOU_LABELS.get(find_by_phone, find_by_phone.replace("_", " ").title()),
                },
                "show_exi_id_on_profile": {
                    "value": show_exi_id,
                    "description": "Turned off: your EXI ID is hidden from your profile.",
                },
                "message_requests": {
                    "value": message_requests,
                    "description": "If turned off, people who are not your contacts cannot start a chat with you.",
                },
            },
            "activity_privacy": {
                "title": "Activity Privacy",
                "last_seen": {
                    "value": last_seen,
                    "options": VISIBILITY_OPTIONS,
                    "labels": VISIBILITY_LABELS,
                    "label": VISIBILITY_LABELS.get(last_seen, last_seen.replace("_", " ").title()),
                    "excluded_contacts": last_seen_except,
                    "excluded_contacts_count": len(last_seen_except),
                },
                "online_status": {
                    "value": online_status,
                    "options": ONLINE_STATUS_OPTIONS,
                    "labels": ONLINE_STATUS_LABELS,
                    "label": ONLINE_STATUS_LABELS.get(online_status, "Same as Last Seen" if online_status == "same_as_last_seen" else online_status.title()),
                },
                "typing_indicator": {
                    "value": typing_indicator,
                    "options": VISIBILITY_OPTIONS,
                    "labels": VISIBILITY_LABELS,
                    "label": VISIBILITY_LABELS.get(typing_indicator, typing_indicator.replace("_", " ").title()),
                    "excluded_contacts": typing_except,
                    "excluded_contacts_count": len(typing_except),
                },
                "read_receipts": {
                    "value": read_receipts,
                    "status": "on" if read_receipts else "off",
                    "options": READ_RECEIPTS_OPTIONS,
                    "labels": READ_RECEIPTS_LABELS,
                    "description": "If turned off, you won't send or receive read receipts.",
                },
            },
            "profile_privacy": {
                "title": "Profile Privacy",
                "notification_sound": {
                    "value": notification_sound,
                    "options": VISIBILITY_OPTIONS,
                    "labels": sound_labels,
                    "label": "Default" if notification_sound == "default" else VISIBILITY_LABELS.get(notification_sound, notification_sound.title()),
                    "excluded_contacts": notification_sound_except,
                    "excluded_contacts_count": len(notification_sound_except),
                    "tone": notification_sound_tone,
                    "tone_options": ["default", "chime", "pop", "subtle", "none"],
                },
                "about": {
                    "value": about,
                    "options": VISIBILITY_OPTIONS,
                    "labels": VISIBILITY_LABELS,
                    "label": VISIBILITY_LABELS.get(about, about.replace("_", " ").title()),
                    "excluded_contacts": about_except,
                    "excluded_contacts_count": len(about_except),
                },
                "status": {
                    "value": status_pref,
                    "options": VISIBILITY_OPTIONS,
                    "labels": VISIBILITY_LABELS,
                    "label": VISIBILITY_LABELS.get(status_pref, status_pref.replace("_", " ").title()),
                    "excluded_contacts": status_except,
                    "excluded_contacts_count": len(status_except),
                    "selected_contacts_count": len(status_selected),
                },
                "groups": {
                    "value": groups,
                    "options": VISIBILITY_OPTIONS,
                    "labels": VISIBILITY_LABELS,
                    "label": VISIBILITY_LABELS.get(groups, groups.replace("_", " ").title()),
                    "excluded_contacts": groups_except,
                    "excluded_contacts_count": len(groups_except),
                    "description": "Who can add you to group chats without an invite.",
                },
            },
            "blocked_and_restricted": {
                "title": "Blocked & Restricted",
                "blocked_contacts": {
                    "count": blocked_count,
                    "action_url": "/api/privacy/blocked/",
                },
                "restricted_contacts": {
                    "count": restricted_count,
                    "action_url": "/api/privacy/restricted/",
                },
            },
        }
    }


def update_privacy_flow_data(user, data):
    """
    Updates privacy preferences. Supports flat data or nested under section names.
    Syncs with UserPrivacySettings model and user_preferences.
    """
    flat = {}
    if not isinstance(data, dict):
        return get_privacy_flow_data(user)

    # Unwrap nested sections if provided
    for section_key in ("finding_you", "activity_privacy", "profile_privacy", "privacy"):
        if section_key in data and isinstance(data[section_key], dict):
            flat.update(data[section_key])

    # Also include top-level keys
    for k, v in data.items():
        if not isinstance(v, dict):
            flat[k] = v

    prefs = get_section_preferences(user.id, "privacy") or {}
    db_settings = get_or_create_privacy_settings(user)
    db_updated = False

    # 1. Finding You
    if "find_by_exi_id" in flat:
        val, _ = _extract_val_and_except(flat, "find_by_exi_id")
        norm = normalize_visibility(val)
        if norm in VALID_VISIBILITY_OPTIONS:
            prefs["find_by_exi_id"] = norm
            prefs["find_by_id"] = norm
    elif "find_by_id" in flat:
        val, _ = _extract_val_and_except(flat, "find_by_id")
        norm = normalize_visibility(val)
        if norm in VALID_VISIBILITY_OPTIONS:
            prefs["find_by_exi_id"] = norm
            prefs["find_by_id"] = norm

    if "find_by_phone" in flat:
        val, _ = _extract_val_and_except(flat, "find_by_phone")
        norm = normalize_visibility(val)
        if norm in VALID_VISIBILITY_OPTIONS:
            prefs["find_by_phone"] = norm

    if "show_exi_id_on_profile" in flat:
        prefs["show_exi_id_on_profile"] = _to_bool(flat["show_exi_id_on_profile"])

    if "message_requests" in flat:
        val = _to_bool(flat["message_requests"])
        prefs["message_requests"] = val
        db_settings.allow_messages_from = "everyone" if val else "contacts"
        db_updated = True

    # 2. Activity Privacy
    val, exc = _extract_val_and_except(flat, "last_seen", "last_seen_except")
    if val is None and "show_last_seen" in flat:
        b = _to_bool(flat["show_last_seen"])
        val = "everyone" if b else "nobody"
    if val is not None:
        norm = normalize_visibility(val)
        if norm in VALID_VISIBILITY_OPTIONS:
            prefs["last_seen"] = norm
            db_settings.show_last_seen = (norm != "nobody")
            db_updated = True
    if exc is not None and isinstance(exc, list):
        prefs["last_seen_except"] = exc
        prefs["last_seen_excluded_contacts"] = exc
    elif "last_seen_excluded_contacts" in flat and isinstance(flat["last_seen_excluded_contacts"], list):
        prefs["last_seen_except"] = flat["last_seen_excluded_contacts"]
        prefs["last_seen_excluded_contacts"] = flat["last_seen_excluded_contacts"]

    val, exc = _extract_val_and_except(flat, "online_status", "online_status_except")
    if val is None and "show_online_status" in flat:
        b = _to_bool(flat["show_online_status"])
        val = "everyone" if b else "nobody"
    if val is not None:
        norm = normalize_online_status(val)
        if norm in VALID_ONLINE_OPTIONS:
            prefs["online_status"] = norm
            db_settings.show_online_status = (norm != "nobody")
            db_updated = True
    if exc is not None and isinstance(exc, list):
        prefs["online_status_except"] = exc

    val, exc = _extract_val_and_except(flat, "typing_indicator", "typing_indicator_except")
    if val is None and ("show_typing_status" in flat or "typing_indicator_enabled" in flat):
        b = _to_bool(flat.get("show_typing_status", flat.get("typing_indicator_enabled")))
        val = "everyone" if b else "nobody"
    if val is not None:
        if isinstance(val, bool):
            norm = "everyone" if val else "nobody"
        else:
            norm = normalize_visibility(val)
        if norm in VALID_VISIBILITY_OPTIONS:
            prefs["typing_indicator"] = norm
    if exc is not None and isinstance(exc, list):
        prefs["typing_indicator_except"] = exc
        prefs["typing_excluded_contacts"] = exc
    elif "typing_excluded_contacts" in flat and isinstance(flat["typing_excluded_contacts"], list):
        prefs["typing_indicator_except"] = flat["typing_excluded_contacts"]
        prefs["typing_excluded_contacts"] = flat["typing_excluded_contacts"]

    raw_rr = flat.get("read_receipts")
    if raw_rr is None:
        raw_rr = flat.get("read_receipts_enabled")
    if raw_rr is not None:
        if isinstance(raw_rr, dict):
            raw_rr = raw_rr.get("value")
        val = _to_bool(raw_rr)
        prefs["read_receipts"] = val
        db_settings.read_receipts_enabled = val
        db_updated = True

    # 3. Profile Privacy
    val, exc = _extract_val_and_except(flat, "notification_sound", "notification_sound_except")
    if val is not None:
        str_val = str(val).lower().strip()
        if str_val in ("default", "chime", "pop", "subtle", "none"):
            prefs["notification_sound"] = str_val
            prefs["notification_sound_tone"] = str_val
        else:
            norm = normalize_visibility(val)
            if norm in VALID_SOUND_OPTIONS:
                prefs["notification_sound"] = norm
                prefs["notification_sound_audience"] = norm
    if "tone" in flat and str(flat["tone"]).lower() in ("default", "chime", "pop", "subtle", "none"):
        prefs["notification_sound_tone"] = str(flat["tone"]).lower()
    if exc is not None and isinstance(exc, list):
        prefs["notification_sound_except"] = exc
        prefs["notification_sound_excluded_contacts"] = exc
    elif "notification_sound_excluded_contacts" in flat and isinstance(flat["notification_sound_excluded_contacts"], list):
        prefs["notification_sound_except"] = flat["notification_sound_excluded_contacts"]
        prefs["notification_sound_excluded_contacts"] = flat["notification_sound_excluded_contacts"]

    val, exc = _extract_val_and_except(flat, "about", "about_except")
    if val is not None:
        norm = normalize_visibility(val)
        if norm in VALID_VISIBILITY_OPTIONS:
            prefs["about"] = norm
    if exc is not None and isinstance(exc, list):
        prefs["about_except"] = exc
        prefs["about_excluded_contacts"] = exc
    elif "about_excluded_contacts" in flat and isinstance(flat["about_excluded_contacts"], list):
        prefs["about_except"] = flat["about_excluded_contacts"]
        prefs["about_excluded_contacts"] = flat["about_excluded_contacts"]

    val, exc = _extract_val_and_except(flat, "status", "status_except")
    if val is not None:
        norm = normalize_visibility(val)
        if norm in VALID_STATUS_OPTIONS or norm in VALID_VISIBILITY_OPTIONS:
            prefs["status"] = norm
    if exc is not None and isinstance(exc, list):
        prefs["status_except"] = exc
        prefs["status_excluded_contacts"] = exc
    elif "status_excluded_contacts" in flat and isinstance(flat["status_excluded_contacts"], list):
        prefs["status_except"] = flat["status_excluded_contacts"]
        prefs["status_excluded_contacts"] = flat["status_excluded_contacts"]
    if "status_selected_contacts" in flat and isinstance(flat["status_selected_contacts"], list):
        prefs["status_selected_contacts"] = flat["status_selected_contacts"]

    val, exc = _extract_val_and_except(flat, "groups", "groups_except")
    if val is None:
        val, exc = _extract_val_and_except(flat, "add_to_groups", "groups_except")
    if val is None:
        val, exc = _extract_val_and_except(flat, "who_can_add_to_groups", "groups_except")
    if val is None:
        val, exc = _extract_val_and_except(flat, "who_can_add_me_to_groups", "groups_except")
    if val is not None:
        norm = normalize_visibility(val)
        if norm in VALID_VISIBILITY_OPTIONS:
            prefs["groups"] = norm
            prefs["add_to_groups"] = norm
            g_prefs = get_section_preferences(user.id, "group_settings") or {}
            g_prefs["who_can_add_to_groups"] = norm
            g_prefs["who_can_add_me_to_groups"] = norm
            update_section_preferences(user.id, "group_settings", g_prefs)
    if exc is not None and isinstance(exc, list):
        prefs["groups_except"] = exc
        prefs["groups_excluded_contacts"] = exc
        g_prefs = get_section_preferences(user.id, "group_settings") or {}
        g_prefs["groups_except"] = exc
        g_prefs["who_can_add_except"] = exc
        update_section_preferences(user.id, "group_settings", g_prefs)
    elif "groups_excluded_contacts" in flat and isinstance(flat["groups_excluded_contacts"], list):
        prefs["groups_except"] = flat["groups_excluded_contacts"]
        prefs["groups_excluded_contacts"] = flat["groups_excluded_contacts"]
        g_prefs = get_section_preferences(user.id, "group_settings") or {}
        g_prefs["groups_except"] = flat["groups_excluded_contacts"]
        g_prefs["who_can_add_except"] = flat["groups_excluded_contacts"]
        update_section_preferences(user.id, "group_settings", g_prefs)

    if "profile_photo" in flat:
        val, _ = _extract_val_and_except(flat, "profile_photo")
        norm = normalize_visibility(val)
        if norm in VALID_VISIBILITY_OPTIONS:
            prefs["profile_photo"] = norm
            db_settings.show_profile_photo = (norm != "nobody")
            db_updated = True

    # Persist
    update_section_preferences(user.id, "privacy", prefs)
    if db_updated:
        db_settings.save()

    return get_privacy_flow_data(user)
    if db_updated:
        db_settings.save()

    return get_privacy_flow_data(user)


# ---------------------------------------------------------------------------
# API Views
# ---------------------------------------------------------------------------

class PrivacyFlowView(APIView):
    """
    GET   /api/privacy/flow/ - Get complete Privacy Settings Flow
    PATCH /api/privacy/flow/ - Update any settings in the flow
    PUT   /api/privacy/flow/ - Update any settings in the flow
    """
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        return Response(get_privacy_flow_data(request.user))

    def patch(self, request):
        updated = update_privacy_flow_data(request.user, request.data)
        return Response({
            "detail": "Privacy settings updated successfully.",
            **updated,
        })

    def put(self, request):
        return self.patch(request)


class PrivacyFindingYouView(APIView):
    """
    GET   /api/privacy/finding-you/ - Get Finding You section
    PATCH /api/privacy/finding-you/ - Update Finding You settings:
          - find_by_exi_id: "everyone" | "contacts" | "nobody"
          - find_by_phone: "everyone" | "contacts" | "nobody"
          - show_exi_id_on_profile: true | false
          - message_requests: true | false
    """
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        data = get_privacy_flow_data(request.user)
        return Response(data["sections"]["finding_you"])

    def patch(self, request):
        updated = update_privacy_flow_data(request.user, request.data)
        return Response({
            "detail": "Finding You settings updated successfully.",
            **updated["sections"]["finding_you"],
        })

    def put(self, request):
        return self.patch(request)


class PrivacyActivityView(APIView):
    """
    GET   /api/privacy/activity/ - Get Activity Privacy section
    PATCH /api/privacy/activity/ - Update Activity Privacy settings:
          - last_seen: "everyone" | "contacts" | "contacts_except" | "nobody" (+ last_seen_except)
          - online_status: "everyone" | "same_as_last_seen" | "contacts" | "nobody"
          - typing_indicator: "everyone" | "contacts" | "contacts_except" | "nobody" (+ typing_indicator_except)
          - read_receipts: true | false (or "on" | "off")
    """
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        data = get_privacy_flow_data(request.user)
        return Response(data["sections"]["activity_privacy"])

    def patch(self, request):
        updated = update_privacy_flow_data(request.user, request.data)
        return Response({
            "detail": "Activity privacy updated successfully.",
            **updated["sections"]["activity_privacy"],
        })

    def put(self, request):
        return self.patch(request)


class PrivacyProfileView(APIView):
    """
    GET   /api/privacy/profile/ - Get Profile Privacy section
    PATCH /api/privacy/profile/ - Update Profile Privacy settings:
          - notification_sound: "everyone" | "contacts" | "contacts_except" | "nobody" | tone
          - about: "everyone" | "contacts" | "contacts_except" | "nobody" (+ about_except)
          - status: "everyone" | "contacts" | "contacts_except" | "nobody" (+ status_except)
          - groups: "everyone" | "contacts" | "contacts_except" | "nobody" (+ groups_except)
    """
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        data = get_privacy_flow_data(request.user)
        return Response(data["sections"]["profile_privacy"])

    def patch(self, request):
        updated = update_privacy_flow_data(request.user, request.data)
        return Response({
            "detail": "Profile privacy updated successfully.",
            **updated["sections"]["profile_privacy"],
        })

    def put(self, request):
        return self.patch(request)


class PrivacyRestrictedUsersView(APIView):
    """
    Manage Restricted contacts:
    - GET  /api/privacy/restricted/ - List all restricted users
    - POST /api/privacy/restrict/   - Restrict a user { "user_id": 123 }
    - POST /api/privacy/unrestrict/ - Unrestrict a user { "user_id": 123 }
    """
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        prefs = get_section_preferences(request.user.id, "privacy") or {}
        restricted_ids = [int(x) for x in prefs.get("restricted_users", []) if str(x).isdigit()]

        users = User.objects.filter(id__in=restricted_ids)
        user_list = []
        for u in users:
            user_list.append({
                "id": u.id,
                "username": u.username,
                "display_name": u.display_name or u.username,
                "full_name": u.full_name or "",
                "profile_photo": u.profile_photo.url if u.profile_photo else None,
                "exi_id": u.exi_id,
            })

        return Response({
            "count": len(user_list),
            "restricted_users": user_list,
        })

    def post(self, request, user_id=None):
        target_id = user_id or request.data.get("user_id") or request.data.get("target_user_id")
        if not target_id:
            return Response({"detail": "user_id is required."}, status=status.HTTP_400_BAD_REQUEST)

        try:
            target = User.objects.get(id=target_id)
        except User.DoesNotExist:
            return Response({"detail": "User not found."}, status=status.HTTP_404_NOT_FOUND)

        action = request.data.get("action")
        # determine if restrict or unrestrict from path or action
        path = request.path.rstrip("/")
        if path.endswith("unrestrict") or action == "unrestrict":
            from chats.chat_settings_flow import toggle_user_restricted
            is_restr = toggle_user_restricted(request.user.id, target.id, restrict=False)
            msg = f"User {target.username} has been unrestricted."
        else:
            from chats.chat_settings_flow import toggle_user_restricted
            is_restr = toggle_user_restricted(request.user.id, target.id, restrict=True)
            msg = f"User {target.username} has been restricted."

        return Response({
            "user_id": target.id,
            "username": target.username,
            "is_restricted": is_restr,
            "detail": msg,
        })

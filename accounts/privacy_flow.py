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

VALID_VISIBILITY_OPTIONS = ["everyone", "contacts", "nobody"]
VALID_ONLINE_OPTIONS = ["everyone", "same_as_last_seen", "nobody"]
VALID_STATUS_OPTIONS = ["everyone", "contacts", "selected", "nobody"]
VALID_SOUND_OPTIONS = ["default", "chime", "pop", "subtle", "none"]


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
    find_by_exi_id = prefs.get("find_by_exi_id", prefs.get("find_by_id", "everyone"))
    find_by_phone = prefs.get("find_by_phone", "everyone")
    show_exi_id = _to_bool(prefs.get("show_exi_id_on_profile", True))
    message_requests = _to_bool(prefs.get("message_requests", True))

    # 2. Activity Privacy
    last_seen = prefs.get("last_seen", "everyone" if db_settings.show_last_seen else "nobody")
    online_status = prefs.get("online_status", "everyone" if db_settings.show_online_status else "nobody")
    typing_indicator = prefs.get("typing_indicator", "everyone")
    read_receipts = _to_bool(prefs.get("read_receipts", db_settings.read_receipts_enabled))

    # 3. Profile Privacy
    notification_sound = prefs.get("notification_sound", "default")
    about = prefs.get("about", "everyone")
    status_pref = prefs.get("status", "contacts")
    status_selected = prefs.get("status_selected_contacts", [])
    groups = prefs.get("groups", prefs.get("add_to_groups", "everyone"))

    # 4. Blocked & Restricted
    blocked_count = BlockedUser.objects.filter(blocker=user).count()
    restricted_users = prefs.get("restricted_users", [])
    restricted_count = len(restricted_users)

    return {
        "sections": {
            "finding_you": {
                "title": "Finding You",
                "find_by_exi_id": {
                    "value": find_by_exi_id,
                    "options": VALID_VISIBILITY_OPTIONS,
                    "label": find_by_exi_id.replace("_", " ").title(),
                },
                "find_by_phone": {
                    "value": find_by_phone,
                    "options": VALID_VISIBILITY_OPTIONS,
                    "label": find_by_phone.replace("_", " ").title(),
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
                    "options": VALID_VISIBILITY_OPTIONS,
                    "label": last_seen.replace("_", " ").title(),
                },
                "online_status": {
                    "value": online_status,
                    "options": VALID_ONLINE_OPTIONS,
                    "label": "Same as Last Seen" if online_status == "same_as_last_seen" else online_status.title(),
                },
                "typing_indicator": {
                    "value": typing_indicator,
                    "options": VALID_VISIBILITY_OPTIONS,
                    "label": typing_indicator.replace("_", " ").title(),
                },
                "read_receipts": {
                    "value": read_receipts,
                    "description": "If turned off, you won't send or receive read receipts.",
                },
            },
            "profile_privacy": {
                "title": "Profile Privacy",
                "notification_sound": {
                    "value": notification_sound,
                    "options": VALID_SOUND_OPTIONS,
                    "label": notification_sound.title(),
                },
                "about": {
                    "value": about,
                    "options": VALID_VISIBILITY_OPTIONS,
                    "label": about.replace("_", " ").title(),
                },
                "status": {
                    "value": status_pref,
                    "options": VALID_STATUS_OPTIONS,
                    "label": status_pref.replace("_", " ").title(),
                    "selected_contacts_count": len(status_selected),
                },
                "groups": {
                    "value": groups,
                    "options": VALID_VISIBILITY_OPTIONS,
                    "label": groups.replace("_", " ").title(),
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
        val = str(flat["find_by_exi_id"]).lower()
        if val in VALID_VISIBILITY_OPTIONS:
            prefs["find_by_exi_id"] = val
            prefs["find_by_id"] = val
    elif "find_by_id" in flat:
        val = str(flat["find_by_id"]).lower()
        if val in VALID_VISIBILITY_OPTIONS:
            prefs["find_by_exi_id"] = val
            prefs["find_by_id"] = val

    if "find_by_phone" in flat:
        val = str(flat["find_by_phone"]).lower()
        if val in VALID_VISIBILITY_OPTIONS:
            prefs["find_by_phone"] = val

    if "show_exi_id_on_profile" in flat:
        prefs["show_exi_id_on_profile"] = _to_bool(flat["show_exi_id_on_profile"])

    if "message_requests" in flat:
        val = _to_bool(flat["message_requests"])
        prefs["message_requests"] = val
        db_settings.allow_messages_from = "everyone" if val else "contacts"
        db_updated = True

    # 2. Activity Privacy
    if "last_seen" in flat:
        val = str(flat["last_seen"]).lower()
        if val in VALID_VISIBILITY_OPTIONS:
            prefs["last_seen"] = val
            db_settings.show_last_seen = (val != "nobody")
            db_updated = True
    elif "show_last_seen" in flat:
        b = _to_bool(flat["show_last_seen"])
        db_settings.show_last_seen = b
        prefs["last_seen"] = "everyone" if b else "nobody"
        db_updated = True

    if "online_status" in flat:
        val = str(flat["online_status"]).lower()
        if val in VALID_ONLINE_OPTIONS:
            prefs["online_status"] = val
            db_settings.show_online_status = (val != "nobody")
            db_updated = True
    elif "show_online_status" in flat:
        b = _to_bool(flat["show_online_status"])
        db_settings.show_online_status = b
        prefs["online_status"] = "everyone" if b else "nobody"
        db_updated = True

    if "typing_indicator" in flat:
        val = flat["typing_indicator"]
        if isinstance(val, bool):
            prefs["typing_indicator"] = "everyone" if val else "nobody"
        elif str(val).lower() in VALID_VISIBILITY_OPTIONS:
            prefs["typing_indicator"] = str(val).lower()
    elif "show_typing_status" in flat or "typing_indicator_enabled" in flat:
        b = _to_bool(flat.get("show_typing_status", flat.get("typing_indicator_enabled")))
        prefs["typing_indicator"] = "everyone" if b else "nobody"

    if "read_receipts" in flat:
        val = _to_bool(flat["read_receipts"])
        prefs["read_receipts"] = val
        db_settings.read_receipts_enabled = val
        db_updated = True
    elif "read_receipts_enabled" in flat:
        val = _to_bool(flat["read_receipts_enabled"])
        prefs["read_receipts"] = val
        db_settings.read_receipts_enabled = val
        db_updated = True

    # 3. Profile Privacy
    if "notification_sound" in flat:
        val = str(flat["notification_sound"]).lower()
        if val in VALID_SOUND_OPTIONS:
            prefs["notification_sound"] = val

    if "about" in flat:
        val = str(flat["about"]).lower()
        if val in VALID_VISIBILITY_OPTIONS:
            prefs["about"] = val

    if "status" in flat:
        val = str(flat["status"]).lower()
        if val in VALID_STATUS_OPTIONS:
            prefs["status"] = val

    if "status_selected_contacts" in flat and isinstance(flat["status_selected_contacts"], list):
        prefs["status_selected_contacts"] = flat["status_selected_contacts"]

    if "groups" in flat:
        val = str(flat["groups"]).lower()
        if val in VALID_VISIBILITY_OPTIONS:
            prefs["groups"] = val
            prefs["add_to_groups"] = val
    elif "add_to_groups" in flat:
        val = str(flat["add_to_groups"]).lower()
        if val in VALID_VISIBILITY_OPTIONS:
            prefs["groups"] = val
            prefs["add_to_groups"] = val

    if "profile_photo" in flat:
        val = str(flat["profile_photo"]).lower()
        if val in VALID_VISIBILITY_OPTIONS:
            prefs["profile_photo"] = val
            db_settings.show_profile_photo = (val != "nobody")
            db_updated = True

    # Persist
    update_section_preferences(user.id, "privacy", prefs)
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
          - last_seen: "everyone" | "contacts" | "nobody"
          - online_status: "everyone" | "same_as_last_seen" | "nobody"
          - typing_indicator: "everyone" | "contacts" | "nobody"
          - read_receipts: true | false
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
          - notification_sound: "default" | "chime" | "pop" | "subtle" | "none"
          - about: "everyone" | "contacts" | "nobody"
          - status: "everyone" | "contacts" | "selected" | "nobody"
          - groups: "everyone" | "contacts" | "nobody"
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

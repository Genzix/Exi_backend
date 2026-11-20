import json
import logging
import os
from datetime import timedelta
from django.conf import settings
from django.contrib.auth import get_user_model
from django.utils import timezone
from rest_framework import permissions, status
from rest_framework.response import Response
from rest_framework.views import APIView

from accounts.models import BlockedUser
from accounts.user_preferences import (
    get_section_preferences,
    update_section_preferences,
    export_user_chat_history,
)
from .models import Conversation, ConversationParticipant, StarredMessage
from .serializers import StarredMessageSerializer

User = get_user_model()
logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Helpers for Theme & Wallpaper, Lists, and Restriction
# ---------------------------------------------------------------------------

def get_chat_theme_and_wallpaper(user_id, conversation_id):
    """Fetches custom theme and wallpaper for a specific chat or falls back to global."""
    chat_themes = get_section_preferences(user_id, "chat_themes") or {}
    custom = chat_themes.get(str(conversation_id))
    appearance = get_section_preferences(user_id, "appearance") or {}

    default_theme = appearance.get("chat_theme", "classic")
    default_accent = appearance.get("accent_color", "#0084FF")
    default_wallpaper = appearance.get("wallpaper", {
        "preset": "default",
        "url": "",
        "color": "#0F172A",
        "blur": 0,
        "opacity": 1.0,
    })

    if not custom:
        return {
            "chat_theme": default_theme,
            "theme": default_theme,
            "accent_color": default_accent,
            "wallpaper": default_wallpaper,
            "is_custom": False,
        }

    return {
        "chat_theme": custom.get("chat_theme", custom.get("theme", default_theme)),
        "theme": custom.get("theme", custom.get("chat_theme", default_theme)),
        "accent_color": custom.get("accent_color", default_accent),
        "wallpaper": custom.get("wallpaper", default_wallpaper),
        "is_custom": True,
    }


def update_chat_theme_and_wallpaper(user_id, conversation_id, data):
    """Updates custom theme/wallpaper for a chat."""
    chat_themes = get_section_preferences(user_id, "chat_themes") or {}
    cid = str(conversation_id)
    current = chat_themes.get(cid, {})

    if "theme" in data:
        current["theme"] = data["theme"]
        current["chat_theme"] = data["theme"]
    if "chat_theme" in data:
        current["chat_theme"] = data["chat_theme"]
        current["theme"] = data["chat_theme"]
    if "accent_color" in data:
        current["accent_color"] = data["accent_color"]
    if "wallpaper" in data:
        current["wallpaper"] = data["wallpaper"]

    chat_themes[cid] = current
    update_section_preferences(user_id, "chat_themes", chat_themes)
    return get_chat_theme_and_wallpaper(user_id, conversation_id)


def is_user_restricted(user_id, target_user_id):
    """Checks if target_user_id is in user's restricted_users list."""
    privacy = get_section_preferences(user_id, "privacy") or {}
    restricted_list = privacy.get("restricted_users", [])
    return int(target_user_id) in [int(x) for x in restricted_list if str(x).isdigit()]


def toggle_user_restricted(user_id, target_user_id, restrict=None):
    """Toggles or sets restrict flag for a target user."""
    privacy = get_section_preferences(user_id, "privacy") or {}
    restricted_list = [int(x) for x in privacy.get("restricted_users", []) if str(x).isdigit()]
    tid = int(target_user_id)

    if restrict is True:
        if tid not in restricted_list:
            restricted_list.append(tid)
        is_restr = True
    elif restrict is False:
        if tid in restricted_list:
            restricted_list.remove(tid)
        is_restr = False
    else:
        if tid in restricted_list:
            restricted_list.remove(tid)
            is_restr = False
        else:
            restricted_list.append(tid)
            is_restr = True

    privacy["restricted_users"] = restricted_list
    update_section_preferences(user_id, "privacy", privacy)
    return is_restr


def get_chat_lists_membership(user_id, conversation_id):
    """Returns all chat lists and whether conversation_id is included in each."""
    prefs = get_section_preferences(user_id, "chat_lists") or {}
    lists = prefs.get("lists", [])
    cid = str(conversation_id)

    result_lists = []
    in_lists = []
    for lst in lists:
        chat_ids = lst.get("chat_ids", [])
        is_in = cid in chat_ids
        if is_in:
            in_lists.append(lst.get("id"))
        result_lists.append({
            "id": lst.get("id"),
            "name": lst.get("name"),
            "color": lst.get("color", "#0084FF"),
            "is_in_list": is_in,
        })

    return {
        "chat_id": cid,
        "lists": result_lists,
        "in_list_ids": in_lists,
        "total_lists": len(result_lists),
    }


def update_chat_lists_membership(user_id, conversation_id, data):
    """Adds or removes a chat to/from lists."""
    prefs = get_section_preferences(user_id, "chat_lists") or {}
    lists = prefs.get("lists", [])
    cid = str(conversation_id)

    # Mode 1: sync list of list_ids: {"list_ids": ["id1", "id2"]}
    if "list_ids" in data:
        target_ids = set(data["list_ids"])
        for lst in lists:
            lid = lst.get("id")
            current = lst.get("chat_ids", [])
            if lid in target_ids and cid not in current:
                current.append(cid)
            elif lid not in target_ids and cid in current:
                current.remove(cid)
            lst["chat_ids"] = current
        update_section_preferences(user_id, "chat_lists", {"lists": lists})
        return get_chat_lists_membership(user_id, conversation_id)

    # Mode 2: single list toggle / add / remove
    list_id = data.get("list_id")
    action = data.get("action", "toggle")  # "add", "remove", "toggle"

    for lst in lists:
        if lst.get("id") == list_id:
            current = lst.get("chat_ids", [])
            if action == "add":
                if cid not in current:
                    current.append(cid)
            elif action == "remove":
                if cid in current:
                    current.remove(cid)
            else:  # toggle
                if cid in current:
                    current.remove(cid)
                else:
                    current.append(cid)
            lst["chat_ids"] = current
            break

    update_section_preferences(user_id, "chat_lists", {"lists": lists})
    return get_chat_lists_membership(user_id, conversation_id)


def get_chat_settings_flow_data(request_user, conversation_id):
    """
    Constructs the complete structured Navigation & Settings Flow object
    matching the 3 requested UI sections:
    1. Conversation
    2. Preferences & Tools
    3. Management & Danger Zone
    """
    try:
        participant = ConversationParticipant.objects.select_related("conversation").get(
            user=request_user, conversation_id=conversation_id, deleted_at__isnull=True
        )
    except ConversationParticipant.DoesNotExist:
        return None

    cid = str(conversation_id)
    conv = participant.conversation

    # 1. Favorite Chat status
    favorites_prefs = get_section_preferences(request_user.id, "favorites") or {}
    favorite_ids = set(favorites_prefs.get("chat_ids", []))
    is_favorited = cid in favorite_ids

    # 2. Disappearing Messages status
    disappearing_seconds = conv.disappearing_duration
    if disappearing_seconds == 86400:
        duration_label = "24 hours"
    elif disappearing_seconds == 604800:
        duration_label = "7 days"
    elif disappearing_seconds == 7776000:
        duration_label = "90 days"
    elif disappearing_seconds:
        duration_label = f"{disappearing_seconds}s"
    else:
        duration_label = "Off"

    # 3. Chat Theme & Wallpaper
    theme_wallpaper = get_chat_theme_and_wallpaper(request_user.id, conversation_id)

    # 4. Add to List status
    list_membership = get_chat_lists_membership(request_user.id, conversation_id)

    # 5. Starred Messages count
    starred_count = StarredMessage.objects.filter(
        user=request_user, message__conversation_id=conversation_id
    ).count()

    # 6. Other user (for Direct chat block & restrict)
    other_user_info = None
    is_blocked = False
    is_restricted = False
    if conv.type == "direct":
        other = conv.participants.exclude(id=request_user.id).first()
        if other:
            is_blocked = BlockedUser.objects.filter(blocker=request_user, blocked=other).exists()
            is_restricted = is_user_restricted(request_user.id, other.id)
            other_user_info = {
                "id": other.id,
                "username": other.username,
                "display_name": other.display_name or other.username,
                "full_name": other.full_name or "",
            }

    # 7. Permissions
    from .chat_permissions import get_chat_permissions_display
    chat_perms = get_chat_permissions_display(conversation_id, request_user.id)

    return {
        "chat_id": cid,
        "type": conv.type,
        "sections": {
            "conversation": {
                "title": "Conversation",
                "favourite_chat": {
                    "is_favorited": is_favorited,
                    "action_url": f"/api/chats/{cid}/favorite/",
                },
                "disappearing_messages": {
                    "is_enabled": bool(disappearing_seconds),
                    "duration": disappearing_seconds,
                    "duration_label": duration_label,
                    "action_url": f"/api/chats/{cid}/disappearing/",
                },
                "chat_theme_wallpaper": {
                    "theme": theme_wallpaper["theme"],
                    "chat_theme": theme_wallpaper["chat_theme"],
                    "accent_color": theme_wallpaper["accent_color"],
                    "wallpaper": theme_wallpaper["wallpaper"],
                    "is_custom": theme_wallpaper["is_custom"],
                    "action_url": f"/api/chats/{cid}/theme/",
                },
                "add_to_list": {
                    "in_list_ids": list_membership["in_list_ids"],
                    "lists": list_membership["lists"],
                    "total_lists": list_membership["total_lists"],
                    "action_url": f"/api/chats/{cid}/lists/",
                },
                "starred_messages": {
                    "count": starred_count,
                    "action_url": f"/api/chats/{cid}/starred/",
                },
            },
            "preferences_tools": {
                "title": "Preferences & Tools",
                "mute_notifications": {
                    "is_muted": participant.is_muted,
                    "action_url": f"/api/chats/{cid}/mute/",
                },
                "export_chat": {
                    "action_url": f"/api/chats/{cid}/export/",
                },
                "chat_settings": {
                    "permissions": chat_perms,
                    "action_url": f"/api/chats/{cid}/settings/",
                },
            },
            "management_danger_zone": {
                "title": "Management & Danger Zone",
                "archive_chat": {
                    "is_archived": participant.is_archived,
                    "action_url": f"/api/chats/{cid}/archive/",
                },
                "block_or_restrict": {
                    "is_blocked": is_blocked,
                    "is_restricted": is_restricted,
                    "other_user": other_user_info,
                    "block_url": f"/api/chats/{cid}/block/",
                    "restrict_url": f"/api/chats/{cid}/restrict/",
                },
                "clear_chat_history": {
                    "cleared_at": participant.cleared_at.isoformat() if participant.cleared_at else None,
                    "action_url": f"/api/chats/{cid}/clear/",
                },
                "delete_conversation": {
                    "action_url": f"/api/chats/{cid}/delete/",
                },
            },
        },
    }


# ---------------------------------------------------------------------------
# API Views for Settings Flow
# ---------------------------------------------------------------------------

class ChatSettingsFlowView(APIView):
    """
    GET /api/chats/<chat_id>/settings-flow/
    GET /api/chats/<chat_id>/navigation/
    Returns the comprehensive Chat Settings navigation structure.
    """
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request, chat_id):
        flow_data = get_chat_settings_flow_data(request.user, chat_id)
        if not flow_data:
            return Response({"detail": "Conversation not found."}, status=status.HTTP_404_NOT_FOUND)
        return Response(flow_data)


class ChatThemeWallpaperView(APIView):
    """
    GET   /api/chats/<chat_id>/theme/      - Get custom theme & wallpaper
    PATCH /api/chats/<chat_id>/theme/      - Update custom theme & wallpaper
    POST  /api/chats/<chat_id>/theme/      - Update custom theme & wallpaper
    """
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request, chat_id):
        try:
            ConversationParticipant.objects.get(
                user=request.user, conversation_id=chat_id, deleted_at__isnull=True
            )
        except ConversationParticipant.DoesNotExist:
            return Response({"detail": "Conversation not found."}, status=status.HTTP_404_NOT_FOUND)

        data = get_chat_theme_and_wallpaper(request.user.id, chat_id)
        return Response({"chat_id": str(chat_id), **data})

    def patch(self, request, chat_id):
        try:
            ConversationParticipant.objects.get(
                user=request.user, conversation_id=chat_id, deleted_at__isnull=True
            )
        except ConversationParticipant.DoesNotExist:
            return Response({"detail": "Conversation not found."}, status=status.HTTP_404_NOT_FOUND)

        updated = update_chat_theme_and_wallpaper(request.user.id, chat_id, request.data)
        return Response({
            "chat_id": str(chat_id),
            "detail": "Chat theme and wallpaper updated successfully.",
            **updated,
        })

    def post(self, request, chat_id):
        return self.patch(request, chat_id)


class ChatListsMembershipView(APIView):
    """
    GET  /api/chats/<chat_id>/lists/  - View which lists this chat belongs to
    POST /api/chats/<chat_id>/lists/  - Add/remove or sync chat in lists
         Body: { "list_id": "...", "action": "add"|"remove"|"toggle" }
         OR:   { "list_ids": ["id1", "id2"] }
    """
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request, chat_id):
        try:
            ConversationParticipant.objects.get(
                user=request.user, conversation_id=chat_id, deleted_at__isnull=True
            )
        except ConversationParticipant.DoesNotExist:
            return Response({"detail": "Conversation not found."}, status=status.HTTP_404_NOT_FOUND)

        data = get_chat_lists_membership(request.user.id, chat_id)
        return Response(data)

    def post(self, request, chat_id):
        try:
            ConversationParticipant.objects.get(
                user=request.user, conversation_id=chat_id, deleted_at__isnull=True
            )
        except ConversationParticipant.DoesNotExist:
            return Response({"detail": "Conversation not found."}, status=status.HTTP_404_NOT_FOUND)

        data = update_chat_lists_membership(request.user.id, chat_id, request.data)
        return Response({
            "detail": "Chat list membership updated successfully.",
            **data,
        })


class ChatStarredMessagesView(APIView):
    """
    GET /api/chats/<chat_id>/starred/ - Get starred messages for this specific chat
    """
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request, chat_id):
        try:
            ConversationParticipant.objects.get(
                user=request.user, conversation_id=chat_id, deleted_at__isnull=True
            )
        except ConversationParticipant.DoesNotExist:
            return Response({"detail": "Conversation not found."}, status=status.HTTP_404_NOT_FOUND)

        starred = StarredMessage.objects.filter(
            user=request.user, message__conversation_id=chat_id
        ).select_related("message", "message__sender")

        return Response({
            "chat_id": str(chat_id),
            "count": starred.count(),
            "results": StarredMessageSerializer(starred, many=True).data,
        })


class ChatExportView(APIView):
    """
    GET  /api/chats/<chat_id>/export/ - Export message history for this chat
    POST /api/chats/<chat_id>/export/ - Export message history for this chat
    """
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request, chat_id):
        try:
            ConversationParticipant.objects.get(
                user=request.user, conversation_id=chat_id, deleted_at__isnull=True
            )
        except ConversationParticipant.DoesNotExist:
            return Response({"detail": "Conversation not found."}, status=status.HTTP_404_NOT_FOUND)

        data = export_user_chat_history(request.user, chat_id=chat_id)
        return Response(data)

    def post(self, request, chat_id):
        return self.get(request, chat_id)


class ChatBlockView(APIView):
    """
    GET  /api/chats/<chat_id>/block/ - Check block status of other user in direct chat
    POST /api/chats/<chat_id>/block/ - Block/unblock other user in direct chat
         Body (optional): { "action": "block" | "unblock" }
    """
    permission_classes = [permissions.IsAuthenticated]

    def _get_other_user(self, user, chat_id):
        try:
            p = ConversationParticipant.objects.select_related("conversation").get(
                user=user, conversation_id=chat_id, deleted_at__isnull=True
            )
        except ConversationParticipant.DoesNotExist:
            return None, "Conversation not found."

        if p.conversation.type != "direct":
            return None, "Block is only supported in direct conversations."

        other = p.conversation.participants.exclude(id=user.id).first()
        if not other:
            return None, "Recipient not found."
        return other, None

    def get(self, request, chat_id):
        other, err = self._get_other_user(request.user, chat_id)
        if err:
            return Response({"detail": err}, status=status.HTTP_400_BAD_REQUEST)

        is_blocked = BlockedUser.objects.filter(blocker=request.user, blocked=other).exists()
        return Response({
            "chat_id": str(chat_id),
            "other_user_id": other.id,
            "username": other.username,
            "is_blocked": is_blocked,
        })

    def post(self, request, chat_id):
        other, err = self._get_other_user(request.user, chat_id)
        if err:
            return Response({"detail": err}, status=status.HTTP_400_BAD_REQUEST)

        action = request.data.get("action")
        existing_block = BlockedUser.objects.filter(blocker=request.user, blocked=other).first()

        if action == "unblock" or (action is None and existing_block):
            if existing_block:
                existing_block.delete()
            is_blocked = False
            msg = f"User {other.username} has been unblocked."
        else:
            if not existing_block:
                BlockedUser.objects.create(
                    blocker=request.user,
                    blocked=other,
                    reason=request.data.get("reason", "")
                )
            is_blocked = True
            msg = f"User {other.username} has been blocked."

        return Response({
            "chat_id": str(chat_id),
            "other_user_id": other.id,
            "username": other.username,
            "is_blocked": is_blocked,
            "detail": msg,
        })


class ChatRestrictView(APIView):
    """
    GET  /api/chats/<chat_id>/restrict/ - Check restrict status of other user
    POST /api/chats/<chat_id>/restrict/ - Restrict/unrestrict other user
         Body (optional): { "action": "restrict" | "unrestrict" }
    """
    permission_classes = [permissions.IsAuthenticated]

    def _get_other_user(self, user, chat_id):
        try:
            p = ConversationParticipant.objects.select_related("conversation").get(
                user=user, conversation_id=chat_id, deleted_at__isnull=True
            )
        except ConversationParticipant.DoesNotExist:
            return None, "Conversation not found."

        if p.conversation.type != "direct":
            return None, "Restrict is only supported in direct conversations."

        other = p.conversation.participants.exclude(id=user.id).first()
        if not other:
            return None, "Recipient not found."
        return other, None

    def get(self, request, chat_id):
        other, err = self._get_other_user(request.user, chat_id)
        if err:
            return Response({"detail": err}, status=status.HTTP_400_BAD_REQUEST)

        is_restr = is_user_restricted(request.user.id, other.id)
        return Response({
            "chat_id": str(chat_id),
            "other_user_id": other.id,
            "username": other.username,
            "is_restricted": is_restr,
        })

    def post(self, request, chat_id):
        other, err = self._get_other_user(request.user, chat_id)
        if err:
            return Response({"detail": err}, status=status.HTTP_400_BAD_REQUEST)

        action = request.data.get("action")
        if action == "restrict":
            is_restr = toggle_user_restricted(request.user.id, other.id, restrict=True)
        elif action == "unrestrict":
            is_restr = toggle_user_restricted(request.user.id, other.id, restrict=False)
        else:
            is_restr = toggle_user_restricted(request.user.id, other.id)

        msg = f"User {other.username} has been {'restricted' if is_restr else 'unrestricted'}."
        return Response({
            "chat_id": str(chat_id),
            "other_user_id": other.id,
            "username": other.username,
            "is_restricted": is_restr,
            "detail": msg,
        })

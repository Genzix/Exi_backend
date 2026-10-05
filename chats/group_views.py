"""
Group Chat Views
Handles: Create Group, Group Info, Group Permissions, Invite Link, Admin Controls

Group metadata (name, description, owner, admins, invite code, permissions)
is stored in Django's cache layer (with a persistent fallback to disk via
the user_preferences pattern) to avoid requiring new DB migrations.
"""
import json
import os
import secrets
import string

from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.db import transaction
from django.utils import timezone
from rest_framework import permissions, status
from rest_framework.response import Response
from rest_framework.views import APIView

from .models import Conversation, ConversationParticipant

User = get_user_model()

# ---------------------------------------------------------------------------
# Group metadata helpers (cache + disk fallback)
# ---------------------------------------------------------------------------

_GROUP_META_DIR = None


def _get_group_meta_dir():
    global _GROUP_META_DIR
    if _GROUP_META_DIR is None:
        from django.conf import settings
        _GROUP_META_DIR = os.path.join(settings.BASE_DIR, "data", "group_meta")
        os.makedirs(_GROUP_META_DIR, exist_ok=True)
    return _GROUP_META_DIR


def _group_cache_key(group_id):
    return f"group_meta:{group_id}"


def _group_file_path(group_id):
    return os.path.join(_get_group_meta_dir(), f"{group_id}.json")


def _get_group_meta(group_id):
    gid = str(group_id)
    data = cache.get(_group_cache_key(gid))
    if data is not None:
        return data
    fpath = _group_file_path(gid)
    if os.path.exists(fpath):
        try:
            with open(fpath, "r", encoding="utf-8") as f:
                data = json.load(f)
            cache.set(_group_cache_key(gid), data, timeout=3600)
            return data
        except Exception:
            pass
    return None


def _save_group_meta(group_id, data):
    gid = str(group_id)
    cache.set(_group_cache_key(gid), data, timeout=3600)
    try:
        fpath = _group_file_path(gid)
        with open(fpath, "w", encoding="utf-8") as f:
            json.dump(data, f)
    except Exception:
        pass


def _default_group_meta(name, owner_id, description=""):
    return {
        "name": name,
        "description": description,
        "icon": "",
        "owner_id": owner_id,
        "admin_ids": [owner_id],
        "invite_code": _generate_invite_code(),
        "invite_enabled": True,
        "permissions": {
            "send_messages": "everyone",
            "send_media": "everyone",
            "add_members": "admins",
            "change_group_info": "admins",
            "pin_messages": "admins",
        },
        "created_at": timezone.now().isoformat(),
    }


def _generate_invite_code():
    chars = string.ascii_letters + string.digits
    return "".join(secrets.choice(chars) for _ in range(16))


def _is_admin(user_id, meta):
    return user_id == meta.get("owner_id") or user_id in (meta.get("admin_ids") or [])


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _get_participant(user, group_id):
    try:
        return ConversationParticipant.objects.select_related("conversation").get(
            user=user,
            conversation_id=group_id,
            deleted_at__isnull=True,
        )
    except ConversationParticipant.DoesNotExist:
        return None


def _group_response(conversation, meta, request):
    """Serialise a group conversation + its metadata."""
    members_qs = ConversationParticipant.objects.filter(
        conversation=conversation, deleted_at__isnull=True
    ).select_related("user")
    admin_ids = meta.get("admin_ids") or []
    members = []
    for p in members_qs:
        members.append({
            "user_id": p.user_id,
            "exi_id": p.user.exi_id,
            "display_name": p.user.display_name or p.user.full_name or p.user.username,
            "is_online": p.user.is_online,
            "is_admin": p.user_id in admin_ids or p.user_id == meta.get("owner_id"),
            "is_owner": p.user_id == meta.get("owner_id"),
            "joined_at": p.joined_at,
        })

    invite_code = meta.get("invite_code") if meta.get("invite_enabled") else None
    invite_link = None
    if invite_code and request:
        invite_link = request.build_absolute_uri(f"/invite/{invite_code}")

    return {
        "group_id": str(conversation.id),
        "name": meta.get("name", ""),
        "description": meta.get("description", ""),
        "icon": meta.get("icon", ""),
        "owner_id": meta.get("owner_id"),
        "admin_ids": admin_ids,
        "member_count": len(members),
        "members": members,
        "invite_link": invite_link,
        "invite_code": invite_code,
        "permissions": meta.get("permissions", {}),
        "disappearing_duration": conversation.disappearing_duration,
        "created_at": conversation.created_at,
        "updated_at": conversation.updated_at,
    }


# ==========================================
# 1. Create Group
# ==========================================

class CreateGroupView(APIView):
    """
    POST /api/groups/
    Create a new group conversation.
    Body: { name, description, member_ids: [int, ...] }
    """
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request):
        name = (request.data.get("name") or "").strip()
        if not name:
            return Response({"detail": "Group name is required."}, status=status.HTTP_400_BAD_REQUEST)
        if len(name) > 100:
            return Response({"detail": "Group name must be at most 100 characters."}, status=status.HTTP_400_BAD_REQUEST)

        description = (request.data.get("description") or "").strip()[:500]
        member_ids = request.data.get("member_ids") or []
        if not isinstance(member_ids, list):
            return Response({"detail": "member_ids must be a list."}, status=status.HTTP_400_BAD_REQUEST)

        valid_members = list(User.objects.filter(id__in=member_ids, is_active=True))
        all_ids = list({u.id for u in valid_members} | {request.user.id})
        if len(all_ids) > 256:
            return Response({"detail": "Group cannot exceed 256 members."}, status=status.HTTP_400_BAD_REQUEST)

        with transaction.atomic():
            conversation = Conversation.objects.create(type="group")
            for uid in all_ids:
                ConversationParticipant.objects.create(
                    conversation=conversation,
                    user_id=uid,
                )

        meta = _default_group_meta(name, request.user.id, description)
        _save_group_meta(conversation.id, meta)

        return Response(
            _group_response(conversation, meta, request),
            status=status.HTTP_201_CREATED,
        )


# ==========================================
# 2. Group Info
# ==========================================

class GroupInfoView(APIView):
    """
    GET  /api/groups/<group_id>/  - Get group info
    PATCH/PUT /api/groups/<group_id>/ - Update group info (admin only)
    """
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request, group_id):
        participant = _get_participant(request.user, group_id)
        if not participant:
            return Response({"detail": "Group not found."}, status=status.HTTP_404_NOT_FOUND)
        conversation = participant.conversation
        if conversation.type != "group":
            return Response({"detail": "Not a group conversation."}, status=status.HTTP_400_BAD_REQUEST)

        meta = _get_group_meta(group_id) or _default_group_meta(
            f"Group {str(conversation.id)[:6]}", request.user.id
        )
        return Response(_group_response(conversation, meta, request))

    def patch(self, request, group_id):
        participant = _get_participant(request.user, group_id)
        if not participant:
            return Response({"detail": "Group not found."}, status=status.HTTP_404_NOT_FOUND)
        conversation = participant.conversation
        if conversation.type != "group":
            return Response({"detail": "Not a group conversation."}, status=status.HTTP_400_BAD_REQUEST)

        meta = _get_group_meta(group_id) or _default_group_meta(
            f"Group {str(conversation.id)[:6]}", request.user.id
        )
        if not _is_admin(request.user.id, meta):
            return Response({"detail": "Only admins can update group info."}, status=status.HTTP_403_FORBIDDEN)

        if "name" in request.data:
            name = (request.data["name"] or "").strip()
            if not name:
                return Response({"detail": "Group name cannot be empty."}, status=status.HTTP_400_BAD_REQUEST)
            meta["name"] = name[:100]

        if "description" in request.data:
            meta["description"] = (request.data.get("description") or "").strip()[:500]

        if "icon" in request.data:
            meta["icon"] = (request.data.get("icon") or "")[:512]

        _save_group_meta(group_id, meta)
        return Response(_group_response(conversation, meta, request))

    put = patch


# ==========================================
# 3. Group Members
# ==========================================

class GroupMembersView(APIView):
    """
    GET  /api/groups/<group_id>/members/  - List members
    POST /api/groups/<group_id>/members/  - Add members (admin or any member if permitted)
    """
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request, group_id):
        participant = _get_participant(request.user, group_id)
        if not participant:
            return Response({"detail": "Group not found."}, status=status.HTTP_404_NOT_FOUND)
        conversation = participant.conversation
        if conversation.type != "group":
            return Response({"detail": "Not a group."}, status=status.HTTP_400_BAD_REQUEST)

        meta = _get_group_meta(group_id) or {}
        admin_ids = meta.get("admin_ids") or []
        participants = ConversationParticipant.objects.filter(
            conversation=conversation, deleted_at__isnull=True
        ).select_related("user").order_by("joined_at")

        members = []
        for p in participants:
            members.append({
                "user_id": p.user_id,
                "exi_id": p.user.exi_id,
                "display_name": p.user.display_name or p.user.full_name or p.user.username,
                "is_online": p.user.is_online,
                "is_admin": p.user_id in admin_ids or p.user_id == meta.get("owner_id"),
                "is_owner": p.user_id == meta.get("owner_id"),
                "joined_at": p.joined_at,
            })
        return Response({"group_id": str(group_id), "member_count": len(members), "members": members})

    def post(self, request, group_id):
        participant = _get_participant(request.user, group_id)
        if not participant:
            return Response({"detail": "Group not found."}, status=status.HTTP_404_NOT_FOUND)
        conversation = participant.conversation
        if conversation.type != "group":
            return Response({"detail": "Not a group."}, status=status.HTTP_400_BAD_REQUEST)

        meta = _get_group_meta(group_id) or {}
        perms = meta.get("permissions", {})
        add_members_perm = perms.get("add_members", "admins")
        if add_members_perm == "admins" and not _is_admin(request.user.id, meta):
            return Response({"detail": "Only admins can add members."}, status=status.HTTP_403_FORBIDDEN)

        member_ids = request.data.get("member_ids") or []
        if not isinstance(member_ids, list) or not member_ids:
            return Response({"detail": "member_ids list is required."}, status=status.HTTP_400_BAD_REQUEST)

        current_count = ConversationParticipant.objects.filter(
            conversation=conversation, deleted_at__isnull=True
        ).count()
        if current_count + len(member_ids) > 256:
            return Response({"detail": "Group cannot exceed 256 members."}, status=status.HTTP_400_BAD_REQUEST)

        added = []
        for uid in member_ids:
            try:
                User.objects.get(id=uid, is_active=True)
            except User.DoesNotExist:
                continue
            p, created = ConversationParticipant.objects.get_or_create(
                conversation=conversation,
                user_id=uid,
                defaults={"deleted_at": None},
            )
            if not created and p.deleted_at is not None:
                p.deleted_at = None
                p.save(update_fields=["deleted_at"])
                added.append(uid)
            elif created:
                added.append(uid)

        return Response({"detail": f"{len(added)} member(s) added.", "added_user_ids": added})


class GroupMemberRemoveView(APIView):
    """
    DELETE /api/groups/<group_id>/members/<user_id>/
    Remove a member (admin only, or self-leave).
    """
    permission_classes = [permissions.IsAuthenticated]

    def delete(self, request, group_id, user_id):
        participant = _get_participant(request.user, group_id)
        if not participant:
            return Response({"detail": "Group not found."}, status=status.HTTP_404_NOT_FOUND)
        conversation = participant.conversation
        if conversation.type != "group":
            return Response({"detail": "Not a group."}, status=status.HTTP_400_BAD_REQUEST)

        meta = _get_group_meta(group_id) or {}
        is_self = request.user.id == user_id
        if not is_self and not _is_admin(request.user.id, meta):
            return Response({"detail": "Only admins can remove members."}, status=status.HTTP_403_FORBIDDEN)

        if meta.get("owner_id") == user_id and not is_self:
            return Response({"detail": "Cannot remove the group owner."}, status=status.HTTP_403_FORBIDDEN)

        try:
            target = ConversationParticipant.objects.get(
                conversation=conversation, user_id=user_id, deleted_at__isnull=True
            )
        except ConversationParticipant.DoesNotExist:
            return Response({"detail": "Member not found."}, status=status.HTTP_404_NOT_FOUND)

        target.deleted_at = timezone.now()
        target.save(update_fields=["deleted_at"])
        return Response({"detail": "Member removed from group."})


# ==========================================
# 4. Group Permissions
# ==========================================

class GroupPermissionsView(APIView):
    """
    GET  /api/groups/<group_id>/permissions/  - Get permissions
    PATCH /api/groups/<group_id>/permissions/ - Update permissions (admin only)
    """
    permission_classes = [permissions.IsAuthenticated]

    _VALID_KEYS = {"send_messages", "send_media", "add_members", "change_group_info", "pin_messages"}
    _VALID_VALUES = {"everyone", "admins", "nobody"}

    def get(self, request, group_id):
        participant = _get_participant(request.user, group_id)
        if not participant:
            return Response({"detail": "Group not found."}, status=status.HTTP_404_NOT_FOUND)
        if participant.conversation.type != "group":
            return Response({"detail": "Not a group."}, status=status.HTTP_400_BAD_REQUEST)

        meta = _get_group_meta(group_id) or {}
        perms = meta.get("permissions", {
            "send_messages": "everyone",
            "send_media": "everyone",
            "add_members": "admins",
            "change_group_info": "admins",
            "pin_messages": "admins",
        })
        return Response({"group_id": str(group_id), "permissions": perms})

    def patch(self, request, group_id):
        participant = _get_participant(request.user, group_id)
        if not participant:
            return Response({"detail": "Group not found."}, status=status.HTTP_404_NOT_FOUND)
        conversation = participant.conversation
        if conversation.type != "group":
            return Response({"detail": "Not a group."}, status=status.HTTP_400_BAD_REQUEST)

        meta = _get_group_meta(group_id) or {}
        if not _is_admin(request.user.id, meta):
            return Response({"detail": "Only admins can change permissions."}, status=status.HTTP_403_FORBIDDEN)

        incoming = request.data.get("permissions") if "permissions" in request.data else request.data
        if not isinstance(incoming, dict):
            return Response({"detail": "permissions must be an object."}, status=status.HTTP_400_BAD_REQUEST)

        current_perms = meta.get("permissions", {
            "send_messages": "everyone",
            "send_media": "everyone",
            "add_members": "admins",
            "change_group_info": "admins",
            "pin_messages": "admins",
        })
        for key, val in incoming.items():
            if key not in self._VALID_KEYS:
                return Response({"detail": f"Unknown permission: '{key}'."}, status=status.HTTP_400_BAD_REQUEST)
            if val not in self._VALID_VALUES:
                return Response(
                    {"detail": f"'{key}' must be one of: {', '.join(self._VALID_VALUES)}."},
                    status=status.HTTP_400_BAD_REQUEST,
                )
            current_perms[key] = val

        meta["permissions"] = current_perms
        _save_group_meta(group_id, meta)
        return Response({"group_id": str(group_id), "permissions": current_perms})

    put = patch


# ==========================================
# 5. Group Invite Link
# ==========================================

class GroupInviteLinkView(APIView):
    """
    GET    /api/groups/<group_id>/invite/ - Get current invite link
    POST   /api/groups/<group_id>/invite/ - Regenerate invite link (admin)
    DELETE /api/groups/<group_id>/invite/ - Revoke invite link (admin)
    """
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request, group_id):
        participant = _get_participant(request.user, group_id)
        if not participant:
            return Response({"detail": "Group not found."}, status=status.HTTP_404_NOT_FOUND)
        if participant.conversation.type != "group":
            return Response({"detail": "Not a group."}, status=status.HTTP_400_BAD_REQUEST)

        meta = _get_group_meta(group_id) or {}
        invite_code = meta.get("invite_code") if meta.get("invite_enabled", True) else None
        invite_link = request.build_absolute_uri(f"/invite/{invite_code}") if invite_code else None
        return Response({
            "group_id": str(group_id),
            "invite_code": invite_code,
            "invite_link": invite_link,
            "invite_enabled": meta.get("invite_enabled", True),
        })

    def post(self, request, group_id):
        participant = _get_participant(request.user, group_id)
        if not participant:
            return Response({"detail": "Group not found."}, status=status.HTTP_404_NOT_FOUND)
        if participant.conversation.type != "group":
            return Response({"detail": "Not a group."}, status=status.HTTP_400_BAD_REQUEST)

        meta = _get_group_meta(group_id) or {}
        if not _is_admin(request.user.id, meta):
            return Response({"detail": "Only admins can regenerate the invite link."}, status=status.HTTP_403_FORBIDDEN)

        meta["invite_code"] = _generate_invite_code()
        meta["invite_enabled"] = True
        _save_group_meta(group_id, meta)
        invite_link = request.build_absolute_uri(f"/invite/{meta['invite_code']}")
        return Response({
            "group_id": str(group_id),
            "invite_code": meta["invite_code"],
            "invite_link": invite_link,
            "detail": "Invite link regenerated.",
        })

    def delete(self, request, group_id):
        participant = _get_participant(request.user, group_id)
        if not participant:
            return Response({"detail": "Group not found."}, status=status.HTTP_404_NOT_FOUND)
        if participant.conversation.type != "group":
            return Response({"detail": "Not a group."}, status=status.HTTP_400_BAD_REQUEST)

        meta = _get_group_meta(group_id) or {}
        if not _is_admin(request.user.id, meta):
            return Response({"detail": "Only admins can revoke the invite link."}, status=status.HTTP_403_FORBIDDEN)

        meta["invite_enabled"] = False
        meta["invite_code"] = ""
        _save_group_meta(group_id, meta)
        return Response({"group_id": str(group_id), "detail": "Invite link revoked.", "invite_enabled": False})


class JoinViaInviteView(APIView):
    """
    POST /api/groups/join/
    Join a group using an invite code.
    Body: { invite_code: str }
    """
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request):
        invite_code = (request.data.get("invite_code") or "").strip()
        if not invite_code:
            return Response({"detail": "invite_code is required."}, status=status.HTTP_400_BAD_REQUEST)

        # Search all group conversations for this invite code
        group_conversations = Conversation.objects.filter(type="group")
        matched_conv = None
        matched_meta = None
        for conv in group_conversations:
            meta = _get_group_meta(conv.id)
            if meta and meta.get("invite_enabled") and meta.get("invite_code") == invite_code:
                matched_conv = conv
                matched_meta = meta
                break

        if not matched_conv:
            return Response({"detail": "Invalid or expired invite link."}, status=status.HTTP_404_NOT_FOUND)

        current_count = ConversationParticipant.objects.filter(
            conversation=matched_conv, deleted_at__isnull=True
        ).count()
        if current_count >= 256:
            return Response({"detail": "Group is full (max 256 members)."}, status=status.HTTP_400_BAD_REQUEST)

        p, created = ConversationParticipant.objects.get_or_create(
            conversation=matched_conv,
            user=request.user,
            defaults={"deleted_at": None},
        )
        if not created and p.deleted_at is not None:
            p.deleted_at = None
            p.save(update_fields=["deleted_at"])

        return Response(
            _group_response(matched_conv, matched_meta, request),
            status=status.HTTP_200_OK,
        )


# ==========================================
# 6. Admin Controls
# ==========================================

class GroupAdminControlsView(APIView):
    """
    POST /api/groups/<group_id>/admins/
    Promote or demote group admins.
    Body: { action: "promote"|"demote", user_id: int }
    """
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request, group_id):
        participant = _get_participant(request.user, group_id)
        if not participant:
            return Response({"detail": "Group not found."}, status=status.HTTP_404_NOT_FOUND)
        conversation = participant.conversation
        if conversation.type != "group":
            return Response({"detail": "Not a group."}, status=status.HTTP_400_BAD_REQUEST)

        meta = _get_group_meta(group_id) or {}
        if not _is_admin(request.user.id, meta):
            return Response({"detail": "Only admins can manage admin roles."}, status=status.HTTP_403_FORBIDDEN)

        action = request.data.get("action")
        target_user_id = request.data.get("user_id")

        if action not in ("promote", "demote"):
            return Response({"detail": "action must be 'promote' or 'demote'."}, status=status.HTTP_400_BAD_REQUEST)
        if not target_user_id:
            return Response({"detail": "user_id is required."}, status=status.HTTP_400_BAD_REQUEST)

        if not ConversationParticipant.objects.filter(
            conversation=conversation, user_id=target_user_id, deleted_at__isnull=True
        ).exists():
            return Response({"detail": "User is not a member of this group."}, status=status.HTTP_404_NOT_FOUND)

        admin_ids = list(meta.get("admin_ids") or [])
        if action == "promote":
            if target_user_id not in admin_ids:
                admin_ids.append(target_user_id)
            detail = "User promoted to admin."
        else:
            if meta.get("owner_id") == target_user_id:
                return Response({"detail": "Cannot demote the group owner."}, status=status.HTTP_400_BAD_REQUEST)
            admin_ids = [uid for uid in admin_ids if uid != target_user_id]
            detail = "User demoted from admin."

        meta["admin_ids"] = admin_ids
        _save_group_meta(group_id, meta)
        return Response({"detail": detail, "admin_ids": admin_ids})


class GroupLeaveView(APIView):
    """
    POST /api/groups/<group_id>/leave/
    Leave a group conversation. Transfers ownership if the owner leaves.
    """
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request, group_id):
        participant = _get_participant(request.user, group_id)
        if not participant:
            return Response({"detail": "Group not found."}, status=status.HTTP_404_NOT_FOUND)
        conversation = participant.conversation
        if conversation.type != "group":
            return Response({"detail": "Not a group."}, status=status.HTTP_400_BAD_REQUEST)

        meta = _get_group_meta(group_id) or {}
        is_owner = meta.get("owner_id") == request.user.id

        if is_owner:
            remaining = list(
                ConversationParticipant.objects.filter(
                    conversation=conversation, deleted_at__isnull=True
                ).exclude(user=request.user).values_list("user_id", flat=True)
            )
            if not remaining:
                participant.deleted_at = timezone.now()
                participant.save(update_fields=["deleted_at"])
                return Response({"detail": "You have left and the group has been dissolved."})

            admin_ids = [uid for uid in (meta.get("admin_ids") or []) if uid != request.user.id]
            new_owner = admin_ids[0] if admin_ids else remaining[0]
            meta["owner_id"] = new_owner
            if new_owner not in admin_ids:
                admin_ids.append(new_owner)
            meta["admin_ids"] = admin_ids
            _save_group_meta(group_id, meta)

        participant.deleted_at = timezone.now()
        participant.save(update_fields=["deleted_at"])
        return Response({"detail": "You have left the group."})


class GroupDismissView(APIView):
    """
    DELETE /api/groups/<group_id>/
    Dissolve the group (owner only). Soft-deletes all participants.
    """
    permission_classes = [permissions.IsAuthenticated]

    def delete(self, request, group_id):
        participant = _get_participant(request.user, group_id)
        if not participant:
            return Response({"detail": "Group not found."}, status=status.HTTP_404_NOT_FOUND)
        conversation = participant.conversation
        if conversation.type != "group":
            return Response({"detail": "Not a group."}, status=status.HTTP_400_BAD_REQUEST)

        meta = _get_group_meta(group_id) or {}
        if meta.get("owner_id") != request.user.id:
            return Response({"detail": "Only the group owner can dissolve the group."}, status=status.HTTP_403_FORBIDDEN)

        ConversationParticipant.objects.filter(
            conversation=conversation, deleted_at__isnull=True
        ).update(deleted_at=timezone.now())

        meta["dissolved"] = True
        _save_group_meta(group_id, meta)
        return Response({"detail": "Group dissolved successfully."})


# ==========================================
# 7. Favorites & Chat Lists
# ==========================================

class FavoriteChatsView(APIView):
    """
    GET  /api/chats/favorites/   - List favorited chats
    POST /api/chats/<chat_id>/favorite/ - Toggle favorite
    """
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        from accounts.user_preferences import get_section_preferences
        favorites_prefs = get_section_preferences(request.user.id, "favorites")
        favorite_ids = set(favorites_prefs.get("chat_ids", []))

        participants = ConversationParticipant.objects.filter(
            user=request.user,
            deleted_at__isnull=True,
            conversation_id__in=favorite_ids,
        ).select_related("conversation").order_by("-conversation__updated_at")

        from .serializers import RecentConversationSerializer
        return Response({
            "count": participants.count(),
            "favorites": RecentConversationSerializer(participants, many=True).data,
        })


class FavoriteChatToggleView(APIView):
    """
    POST /api/chats/<chat_id>/favorite/
    Toggle a chat as favorite.
    """
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request, chat_id):
        try:
            ConversationParticipant.objects.get(
                user=request.user, conversation_id=chat_id, deleted_at__isnull=True
            )
        except ConversationParticipant.DoesNotExist:
            return Response({"detail": "Conversation not found."}, status=status.HTTP_404_NOT_FOUND)

        from accounts.user_preferences import get_section_preferences, update_section_preferences
        prefs = get_section_preferences(request.user.id, "favorites")
        ids = prefs.get("chat_ids", [])
        cid = str(chat_id)

        if cid in ids:
            ids.remove(cid)
            is_favorited = False
        else:
            ids.append(cid)
            is_favorited = True

        update_section_preferences(request.user.id, "favorites", {"chat_ids": ids})
        return Response({
            "chat_id": cid,
            "is_favorited": is_favorited,
            "detail": f"Chat {'added to' if is_favorited else 'removed from'} favorites.",
        })


class ChatListsView(APIView):
    """
    GET  /api/chats/lists/      - Get all chat lists
    POST /api/chats/lists/      - Create a new chat list
    """
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        from accounts.user_preferences import get_section_preferences
        prefs = get_section_preferences(request.user.id, "chat_lists")
        return Response({"lists": prefs.get("lists", [])})

    def post(self, request):
        name = (request.data.get("name") or "").strip()
        if not name:
            return Response({"detail": "List name is required."}, status=status.HTTP_400_BAD_REQUEST)

        from accounts.user_preferences import get_section_preferences, update_section_preferences
        prefs = get_section_preferences(request.user.id, "chat_lists")
        lists = prefs.get("lists", [])

        import uuid
        new_list = {
            "id": str(uuid.uuid4()),
            "name": name[:50],
            "description": (request.data.get("description") or "").strip()[:200],
            "chat_ids": [],
            "color": request.data.get("color", "#0084FF"),
            "created_at": timezone.now().isoformat(),
        }
        lists.append(new_list)
        update_section_preferences(request.user.id, "chat_lists", {"lists": lists})
        return Response(new_list, status=status.HTTP_201_CREATED)


class ChatListDetailView(APIView):
    """
    GET    /api/chats/lists/<list_id>/        - Get list details
    PATCH  /api/chats/lists/<list_id>/        - Update list
    DELETE /api/chats/lists/<list_id>/        - Delete list
    POST   /api/chats/lists/<list_id>/chats/  - Add/remove chats
    """
    permission_classes = [permissions.IsAuthenticated]

    def _get_lists(self, user_id):
        from accounts.user_preferences import get_section_preferences
        return get_section_preferences(user_id, "chat_lists").get("lists", [])

    def _save_lists(self, user_id, lists):
        from accounts.user_preferences import update_section_preferences
        update_section_preferences(user_id, "chat_lists", {"lists": lists})

    def _find(self, lists, list_id):
        for i, lst in enumerate(lists):
            if lst.get("id") == list_id:
                return i, lst
        return None, None

    def get(self, request, list_id):
        lists = self._get_lists(request.user.id)
        _, lst = self._find(lists, list_id)
        if lst is None:
            return Response({"detail": "List not found."}, status=status.HTTP_404_NOT_FOUND)
        return Response(lst)

    def patch(self, request, list_id):
        lists = self._get_lists(request.user.id)
        idx, lst = self._find(lists, list_id)
        if lst is None:
            return Response({"detail": "List not found."}, status=status.HTTP_404_NOT_FOUND)

        if "name" in request.data:
            name = (request.data["name"] or "").strip()
            if not name:
                return Response({"detail": "Name cannot be empty."}, status=status.HTTP_400_BAD_REQUEST)
            lst["name"] = name[:50]
        if "description" in request.data:
            lst["description"] = (request.data.get("description") or "").strip()[:200]
        if "color" in request.data:
            lst["color"] = request.data["color"]

        lists[idx] = lst
        self._save_lists(request.user.id, lists)
        return Response(lst)

    def delete(self, request, list_id):
        lists = self._get_lists(request.user.id)
        idx, lst = self._find(lists, list_id)
        if lst is None:
            return Response({"detail": "List not found."}, status=status.HTTP_404_NOT_FOUND)
        lists.pop(idx)
        self._save_lists(request.user.id, lists)
        return Response(status=status.HTTP_204_NO_CONTENT)


class ChatListChatsView(APIView):
    """
    POST /api/chats/lists/<list_id>/chats/
    Add or remove chats from a list.
    Body: { action: "add"|"remove", chat_ids: [str] }
    """
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request, list_id):
        from accounts.user_preferences import get_section_preferences, update_section_preferences
        prefs = get_section_preferences(request.user.id, "chat_lists")
        lists = prefs.get("lists", [])

        idx = None
        for i, lst in enumerate(lists):
            if lst.get("id") == list_id:
                idx = i
                break

        if idx is None:
            return Response({"detail": "List not found."}, status=status.HTTP_404_NOT_FOUND)

        action = request.data.get("action", "add")
        chat_ids = [str(cid) for cid in (request.data.get("chat_ids") or [])]

        if action not in ("add", "remove"):
            return Response({"detail": "action must be 'add' or 'remove'."}, status=status.HTTP_400_BAD_REQUEST)

        current_ids = lists[idx].get("chat_ids", [])
        if action == "add":
            for cid in chat_ids:
                if cid not in current_ids:
                    current_ids.append(cid)
        else:
            current_ids = [cid for cid in current_ids if cid not in chat_ids]

        lists[idx]["chat_ids"] = current_ids
        update_section_preferences(request.user.id, "chat_lists", {"lists": lists})
        return Response({"list_id": list_id, "chat_ids": current_ids, "action": action})

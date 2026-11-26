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


def _default_group_meta(name, owner_id, description="", require_approval=False, admin_preset="admins", info_visibility="everyone"):
    perms = {
        "send_messages": "everyone",
        "send_media": "everyone",
        "add_members": "admins" if admin_preset == "admins" else "everyone",
        "change_group_info": "admins" if admin_preset == "admins" else "everyone",
        "pin_messages": "admins" if admin_preset == "admins" else "everyone",
    }
    return {
        "name": name,
        "description": description,
        "icon": "",
        "owner_id": owner_id,
        "admin_ids": [owner_id],
        "invite_code": _generate_invite_code(),
        "invite_enabled": True,
        "require_approval": require_approval,
        "info_visibility": info_visibility,
        "pending_approvals": [],
        "permissions": perms,
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

    pending_approvals = meta.get("pending_approvals") or []
    current_uid = request.user.id if (request and hasattr(request, "user") and request.user.is_authenticated) else None
    is_admin = _is_admin(current_uid, meta) if current_uid else False

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
        "require_approval": meta.get("require_approval", False),
        "info_visibility": meta.get("info_visibility", "everyone"),
        "pending_approvals_count": len(pending_approvals) if is_admin else 0,
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

        from accounts.user_preferences import (
            get_section_preferences,
            can_user_add_to_group,
            create_group_invitation,
        )

        valid_members = list(User.objects.filter(id__in=member_ids, is_active=True))
        all_candidate_ids = list({u.id for u in valid_members} | {request.user.id})
        if len(all_candidate_ids) > 256:
            return Response({"detail": "Group cannot exceed 256 members."}, status=status.HTTP_400_BAD_REQUEST)

        # Check who allows being added to groups
        allowed_ids = {request.user.id}
        invited_users = []
        restrictions = []

        for u in valid_members:
            if u.id == request.user.id:
                continue
            allowed, reason = can_user_add_to_group(request.user, u)
            if allowed:
                allowed_ids.add(u.id)
            else:
                invited_users.append(u)
                restrictions.append({
                    "user_id": u.id,
                    "username": u.username,
                    "reason": reason,
                })

        with transaction.atomic():
            conversation = Conversation.objects.create(type="group")
            for uid in allowed_ids:
                ConversationParticipant.objects.create(
                    conversation=conversation,
                    user_id=uid,
                )

        owner_g_prefs = get_section_preferences(request.user.id, "group_settings") or {}
        default_approval = bool(owner_g_prefs.get("member_approval_by_default", False))
        admin_preset = owner_g_prefs.get("admin_controls_preset", "admins")

        meta = _default_group_meta(
            name,
            request.user.id,
            description,
            require_approval=default_approval,
            admin_preset=admin_preset,
        )
        _save_group_meta(conversation.id, meta)

        # Send invitations to users who cannot be added directly
        for u in invited_users:
            create_group_invitation(conversation.id, request.user, u)

        resp_data = _group_response(conversation, meta, request)
        if invited_users:
            resp_data["invited_members"] = [u.id for u in invited_users]
            resp_data["restrictions"] = restrictions
            resp_data["note"] = f"{len(allowed_ids)-1} member(s) added directly, {len(invited_users)} invitation(s) sent due to privacy preferences."

        return Response(
            resp_data,
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
        invited = []
        restrictions = []

        from accounts.user_preferences import can_user_add_to_group, create_group_invitation

        for uid in member_ids:
            try:
                target_user = User.objects.get(id=uid, is_active=True)
            except User.DoesNotExist:
                continue

            allowed, reason = can_user_add_to_group(request.user, target_user)
            if not allowed:
                create_group_invitation(group_id, request.user, target_user)
                invited.append(uid)
                restrictions.append({
                    "user_id": uid,
                    "username": target_user.username,
                    "reason": reason,
                })
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

        msg_parts = []
        if added:
            msg_parts.append(f"{len(added)} member(s) added")
        if invited:
            msg_parts.append(f"{len(invited)} invitation(s) sent")
        detail_msg = ", ".join(msg_parts) + "." if msg_parts else "No members added."

        return Response({
            "detail": detail_msg,
            "added_user_ids": added,
            "invited_user_ids": invited,
            "restrictions": restrictions,
        })


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

        # If member approval is required, check if already active or place into pending_approvals
        if matched_meta.get("require_approval", False):
            existing_p = ConversationParticipant.objects.filter(
                conversation=matched_conv, user=request.user, deleted_at__isnull=True
            ).first()
            if existing_p:
                return Response(_group_response(matched_conv, matched_meta, request), status=status.HTTP_200_OK)

            pending = matched_meta.setdefault("pending_approvals", [])
            if any(item.get("user_id") == request.user.id for item in pending):
                return Response({
                    "status": "pending_approval",
                    "group_id": str(matched_conv.id),
                    "name": matched_meta.get("name"),
                    "detail": "You already have a pending join request awaiting admin approval.",
                }, status=status.HTTP_200_OK)

            pending.append({
                "user_id": request.user.id,
                "username": request.user.username,
                "display_name": getattr(request.user, "display_name", None) or getattr(request.user, "full_name", None) or request.user.username,
                "exi_id": getattr(request.user, "exi_id", ""),
                "requested_at": timezone.now().isoformat(),
            })
            _save_group_meta(matched_conv.id, matched_meta)
            return Response({
                "status": "pending_approval",
                "group_id": str(matched_conv.id),
                "name": matched_meta.get("name"),
                "detail": "Membership approval is enabled for this group. Your join request has been submitted to group admins.",
            }, status=status.HTTP_202_ACCEPTED)

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
    Admin controls and role management for a group:
    - GET   /api/groups/<group_id>/admins/ or /api/groups/<group_id>/admin-controls/
    - POST  /api/groups/<group_id>/admins/  { action: "promote"|"demote"|"transfer_ownership", user_id }
    - PATCH /api/groups/<group_id>/admin-controls/  { permissions, require_approval, info_visibility }
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
        return Response({
            "group_id": str(group_id),
            "owner_id": meta.get("owner_id"),
            "admin_ids": meta.get("admin_ids") or [],
            "permissions": meta.get("permissions", {}),
            "require_approval": meta.get("require_approval", False),
            "info_visibility": meta.get("info_visibility", "everyone"),
            "is_admin": _is_admin(request.user.id, meta),
            "is_owner": request.user.id == meta.get("owner_id"),
        })

    def patch(self, request, group_id):
        participant = _get_participant(request.user, group_id)
        if not participant:
            return Response({"detail": "Group not found."}, status=status.HTTP_404_NOT_FOUND)
        conversation = participant.conversation
        if conversation.type != "group":
            return Response({"detail": "Not a group."}, status=status.HTTP_400_BAD_REQUEST)

        meta = _get_group_meta(group_id) or {}
        if not _is_admin(request.user.id, meta):
            return Response({"detail": "Only admins can update admin controls."}, status=status.HTTP_403_FORBIDDEN)

        data = request.data
        if "require_approval" in data:
            meta["require_approval"] = bool(data["require_approval"])

        if "info_visibility" in data:
            vis = str(data["info_visibility"]).lower().strip()
            if vis in ("everyone", "members_only", "hidden"):
                meta["info_visibility"] = vis

        if "permissions" in data and isinstance(data["permissions"], dict):
            perms = meta.setdefault("permissions", {})
            for k, v in data["permissions"].items():
                if k in ("send_messages", "send_media", "add_members", "change_group_info", "pin_messages"):
                    if str(v).lower() in ("everyone", "admins", "nobody"):
                        perms[k] = str(v).lower()

        _save_group_meta(group_id, meta)
        return Response({
            "group_id": str(group_id),
            "detail": "Admin controls updated.",
            "permissions": meta.get("permissions", {}),
            "require_approval": meta.get("require_approval", False),
            "info_visibility": meta.get("info_visibility", "everyone"),
        })

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

        if action not in ("promote", "demote", "transfer_ownership"):
            return Response({"detail": "action must be 'promote', 'demote', or 'transfer_ownership'."}, status=status.HTTP_400_BAD_REQUEST)
        if not target_user_id:
            return Response({"detail": "user_id is required."}, status=status.HTTP_400_BAD_REQUEST)

        try:
            target_user_id = int(target_user_id)
        except (ValueError, TypeError):
            return Response({"detail": "Invalid user_id."}, status=status.HTTP_400_BAD_REQUEST)

        if not ConversationParticipant.objects.filter(
            conversation=conversation, user_id=target_user_id, deleted_at__isnull=True
        ).exists():
            return Response({"detail": "User is not a member of this group."}, status=status.HTTP_404_NOT_FOUND)

        admin_ids = list(meta.get("admin_ids") or [])

        if action == "promote":
            if target_user_id not in admin_ids:
                admin_ids.append(target_user_id)
            detail = "User promoted to admin."
        elif action == "demote":
            if meta.get("owner_id") == target_user_id:
                return Response({"detail": "Cannot demote the group owner."}, status=status.HTTP_400_BAD_REQUEST)
            admin_ids = [uid for uid in admin_ids if uid != target_user_id]
            detail = "User demoted from admin."
        elif action == "transfer_ownership":
            if request.user.id != meta.get("owner_id"):
                return Response({"detail": "Only the group owner can transfer ownership."}, status=status.HTTP_403_FORBIDDEN)
            meta["owner_id"] = target_user_id
            if target_user_id not in admin_ids:
                admin_ids.append(target_user_id)
            if request.user.id not in admin_ids:
                admin_ids.append(request.user.id)
            detail = f"Ownership transferred to user {target_user_id}."

        meta["admin_ids"] = admin_ids
        _save_group_meta(group_id, meta)
        return Response({
            "detail": detail,
            "owner_id": meta.get("owner_id"),
            "admin_ids": admin_ids,
        })


class GroupApprovalsView(APIView):
    """
    Manage member join requests for groups with Member Approval enabled:
    - GET  /api/groups/<group_id>/approvals/ - List pending requests
    - POST /api/groups/<group_id>/approvals/<user_id>/approve/ - Approve user
    - POST /api/groups/<group_id>/approvals/<user_id>/reject/ - Reject user
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
        if not _is_admin(request.user.id, meta):
            return Response({"detail": "Only admins can view pending approvals."}, status=status.HTTP_403_FORBIDDEN)

        pending = meta.get("pending_approvals") or []
        return Response({
            "group_id": str(group_id),
            "require_approval": meta.get("require_approval", False),
            "pending_count": len(pending),
            "pending_approvals": pending,
        })

    def post(self, request, group_id, user_id=None, action=None):
        participant = _get_participant(request.user, group_id)
        if not participant:
            return Response({"detail": "Group not found."}, status=status.HTTP_404_NOT_FOUND)
        conversation = participant.conversation
        if conversation.type != "group":
            return Response({"detail": "Not a group."}, status=status.HTTP_400_BAD_REQUEST)

        meta = _get_group_meta(group_id) or {}
        if not _is_admin(request.user.id, meta):
            return Response({"detail": "Only admins can approve or reject join requests."}, status=status.HTTP_403_FORBIDDEN)

        if not user_id:
            user_id = request.data.get("user_id")
        if not action:
            action = request.data.get("action", "approve")

        if not user_id:
            return Response({"detail": "user_id is required."}, status=status.HTTP_400_BAD_REQUEST)

        try:
            target_user = User.objects.get(id=int(user_id), is_active=True)
        except (User.DoesNotExist, ValueError):
            return Response({"detail": "User not found."}, status=status.HTTP_404_NOT_FOUND)

        pending = meta.get("pending_approvals") or []
        target_uid = int(user_id)
        action = str(action).lower().strip()

        if action == "approve":
            p, created = ConversationParticipant.objects.get_or_create(
                conversation=conversation,
                user=target_user,
                defaults={"deleted_at": None},
            )
            if not created and p.deleted_at is not None:
                p.deleted_at = None
                p.save(update_fields=["deleted_at"])

            meta["pending_approvals"] = [item for item in pending if item.get("user_id") != target_uid]
            _save_group_meta(group_id, meta)
            return Response({
                "detail": f"{target_user.username} approved and added to group.",
                "user_id": target_uid,
                "action": "approved",
                "remaining_pending_count": len(meta["pending_approvals"]),
            })
        elif action == "reject":
            meta["pending_approvals"] = [item for item in pending if item.get("user_id") != target_uid]
            _save_group_meta(group_id, meta)
            return Response({
                "detail": f"Join request for {target_user.username} rejected.",
                "user_id": target_uid,
                "action": "rejected",
                "remaining_pending_count": len(meta["pending_approvals"]),
            })
        else:
            return Response({"detail": "action must be 'approve' or 'reject'."}, status=status.HTTP_400_BAD_REQUEST)


class GroupVisibilityView(APIView):
    """
    GET  /api/groups/<group_id>/visibility/ - Get group visibility
    PATCH /api/groups/<group_id>/visibility/ - Update group visibility (admins only)
    """
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request, group_id):
        participant = _get_participant(request.user, group_id)
        if not participant:
            return Response({"detail": "Group not found."}, status=status.HTTP_404_NOT_FOUND)
        meta = _get_group_meta(group_id) or {}
        vis = meta.get("info_visibility", "everyone")
        return Response({
            "group_id": str(group_id),
            "info_visibility": vis,
            "options": ["everyone", "members_only", "hidden"],
            "labels": {
                "everyone": "Everyone",
                "members_only": "Members Only",
                "hidden": "Hidden from Search",
            }
        })

    def patch(self, request, group_id):
        participant = _get_participant(request.user, group_id)
        if not participant:
            return Response({"detail": "Group not found."}, status=status.HTTP_404_NOT_FOUND)
        meta = _get_group_meta(group_id) or {}
        if not _is_admin(request.user.id, meta):
            return Response({"detail": "Only admins can change group visibility."}, status=status.HTTP_403_FORBIDDEN)

        vis = request.data.get("info_visibility") or request.data.get("visibility")
        if vis is not None:
            vis = str(vis).lower().strip()
            if vis not in ("everyone", "members_only", "hidden"):
                return Response({"detail": "info_visibility must be 'everyone', 'members_only', or 'hidden'."}, status=status.HTTP_400_BAD_REQUEST)
            meta["info_visibility"] = vis
            _save_group_meta(group_id, meta)

        return Response({
            "group_id": str(group_id),
            "info_visibility": meta.get("info_visibility", "everyone"),
            "detail": "Group visibility updated.",
        })

    put = patch


class UserGroupInvitationsView(APIView):
    """
    Manage invitations sent to the authenticated user:
    - GET  /api/groups/invitations/ - List pending invitations
    - POST /api/groups/invitations/<invitation_id>/accept/ - Accept invitation
    - POST /api/groups/invitations/<invitation_id>/decline/ - Decline invitation
    """
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        from accounts.user_preferences import get_user_group_invitations
        status_filter = request.query_params.get("status", "pending")
        if status_filter == "all":
            status_filter = None
        invs = get_user_group_invitations(request.user.id, status_filter=status_filter)
        return Response({
            "count": len(invs),
            "invitations": invs,
        })

    def post(self, request, invitation_id=None, action=None):
        from accounts.user_preferences import accept_group_invitation, decline_group_invitation

        if not invitation_id:
            invitation_id = request.data.get("invitation_id")
        if not action:
            action = request.data.get("action", "accept")

        if not invitation_id:
            return Response({"detail": "invitation_id is required."}, status=status.HTTP_400_BAD_REQUEST)

        action = str(action).lower().strip()
        if action == "accept":
            success, detail, group_id = accept_group_invitation(request.user, invitation_id)
            if not success:
                return Response({"detail": detail}, status=status.HTTP_400_BAD_REQUEST)
            return Response({
                "detail": detail,
                "group_id": group_id,
                "invitation_id": invitation_id,
                "status": "accepted",
            })
        elif action == "decline":
            success, detail = decline_group_invitation(request.user, invitation_id)
            if not success:
                return Response({"detail": detail}, status=status.HTTP_400_BAD_REQUEST)
            return Response({
                "detail": detail,
                "invitation_id": invitation_id,
                "status": "declined",
            })
        else:
            return Response({"detail": "action must be 'accept' or 'decline'."}, status=status.HTTP_400_BAD_REQUEST)


class RecentGroupsListView(APIView):
    """
    GET /api/groups/recent/ - List user's recent groups with metadata and filters
    Query params:
    - ?search=<keyword>
    - ?role=owner|admin|member
    - ?limit=<int>
    """
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        from accounts.user_preferences import get_user_recent_groups
        search = request.query_params.get("search")
        role = request.query_params.get("role")
        limit_param = request.query_params.get("limit", 20)
        try:
            limit = int(limit_param)
        except (ValueError, TypeError):
            limit = 20

        groups = get_user_recent_groups(request.user, request=request, search=search, role=role, limit=limit)
        return Response({
            "count": len(groups),
            "groups": groups,
        })


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
    GET  /api/chats/<chat_id>/favorite/ - Get favorite status
    POST /api/chats/<chat_id>/favorite/ - Toggle or set a chat as favorite
         Body (optional): { "is_favorited": true|false } OR { "action": "favorite"|"unfavorite"|"toggle" }
    """
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request, chat_id):
        try:
            participant = ConversationParticipant.objects.get(
                user=request.user, conversation_id=chat_id, deleted_at__isnull=True
            )
        except ConversationParticipant.DoesNotExist:
            return Response({"detail": "Conversation not found."}, status=status.HTTP_404_NOT_FOUND)

        from accounts.user_preferences import get_section_preferences
        prefs = get_section_preferences(request.user.id, "favorites") or {}
        ids = prefs.get("chat_ids", [])
        cid = str(chat_id)
        is_fav = (cid in ids) or bool(participant.is_pinned)
        return Response({
            "chat_id": cid,
            "title": "Favourite Chat",
            "subtitle": "Quick access on home screen",
            "is_favorited": is_fav,
            "value": is_fav,
            "status": "on" if is_fav else "off",
        })

    def post(self, request, chat_id):
        try:
            participant = ConversationParticipant.objects.get(
                user=request.user, conversation_id=chat_id, deleted_at__isnull=True
            )
        except ConversationParticipant.DoesNotExist:
            return Response({"detail": "Conversation not found."}, status=status.HTTP_404_NOT_FOUND)

        from accounts.user_preferences import get_section_preferences, update_section_preferences
        prefs = get_section_preferences(request.user.id, "favorites") or {}
        ids = list(prefs.get("chat_ids", []))
        cid = str(chat_id)

        req_data = request.data if hasattr(request, "data") and isinstance(request.data, dict) else {}
        action = req_data.get("action")
        is_fav_param = req_data.get("is_favorited") if "is_favorited" in req_data else req_data.get("favorite")

        if is_fav_param is not None:
            is_favorited = str(is_fav_param).lower() in ("true", "1", "yes", "on")
        elif action == "favorite":
            is_favorited = True
        elif action == "unfavorite":
            is_favorited = False
        else:
            is_favorited = not ((cid in ids) or bool(participant.is_pinned))

        if is_favorited:
            if cid not in ids:
                ids.append(cid)
            participant.is_pinned = True
        else:
            if cid in ids:
                ids.remove(cid)
            participant.is_pinned = False

        participant.save(update_fields=["is_pinned"])
        update_section_preferences(request.user.id, "favorites", {"chat_ids": ids})

        return Response({
            "chat_id": cid,
            "title": "Favourite Chat",
            "subtitle": "Quick access on home screen",
            "is_favorited": is_favorited,
            "value": is_favorited,
            "status": "on" if is_favorited else "off",
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

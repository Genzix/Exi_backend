import hashlib
import re
import secrets
import string
import uuid
from datetime import timedelta
from django.core.cache import cache
from django.db import models
from django.db.models import Count, Q
from django.utils import timezone

from django.contrib.auth import get_user_model
from rest_framework import generics, permissions, status
from rest_framework.response import Response
from rest_framework.views import APIView

from .utils import (
    clear_login_failures,
    get_login_lockout,
    record_login_failure,
    register_or_update_device,
    throttle_request,
)
from .serializers import (
    ForgotPasswordSerializer,
    LoginSerializer,
    OnlineStatusSerializer,
    ProfileSerializer,
    ProfileSetupSerializer,
    ProfileUpdateSerializer,
    PublicProfileSerializer,
    RegisterSerializer,
    ResetPasswordSerializer,
    SendOTPSerializer,
    VerifyOTPSerializer,
    get_tokens,
)

User = get_user_model()


def auth_payload(user, request):
    return {
        **get_tokens(user),
        "user": ProfileSerializer(user, context={"request": request}).data,
    }


def too_many_requests(detail, retry_after):
    response = Response(
        {"detail": detail, "retry_after": retry_after},
        status=status.HTTP_429_TOO_MANY_REQUESTS,
    )
    response["Retry-After"] = str(retry_after)
    return response


# ---------- Screen 1: Login ----------

class LoginView(APIView):
    permission_classes = [permissions.AllowAny]

    def post(self, request):
        identifier = str(request.data.get("email_or_phone") or "").strip()

        allowed, retry_after = throttle_request(request, "login", limit=20, window_seconds=300)
        if not allowed:
            return too_many_requests("Too many login attempts. Try again later.", retry_after)

        lockout = get_login_lockout(identifier) if identifier else 0
        if lockout:
            return too_many_requests(
                "Account temporarily locked after too many failed attempts.", lockout
            )

        serializer = LoginSerializer(data=request.data)
        if not serializer.is_valid():
            if identifier:
                record_login_failure(identifier)
            return Response(
                {"detail": "Invalid email/phone or password."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        user = serializer.validated_data["user"]
        clear_login_failures(identifier)
        user.set_online()
        register_or_update_device(user, request, request.data)
        return Response(auth_payload(user, request))


class LogoutView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request):
        request.user.set_offline()
        device_id = request.data.get('device_id')
        if device_id:
            from .models import UserDevice
            UserDevice.objects.filter(
                user=request.user,
                device_id=device_id,
            ).update(is_active=False, push_token='')
        return Response({"detail": "Logged out successfully."})


class ForgotPasswordView(APIView):
    permission_classes = [permissions.AllowAny]

    def post(self, request):
        allowed, retry_after = throttle_request(
            request, "forgot-password", limit=5, window_seconds=900
        )
        if not allowed:
            return too_many_requests("Too many reset requests. Try again later.", retry_after)

        serializer = ForgotPasswordSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        return Response(serializer.save())


class ResetPasswordView(APIView):
    permission_classes = [permissions.AllowAny]

    def post(self, request):
        allowed, retry_after = throttle_request(
            request, "reset-password", limit=10, window_seconds=900
        )
        if not allowed:
            return too_many_requests("Too many reset attempts. Try again later.", retry_after)

        serializer = ResetPasswordSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        serializer.save()
        return Response({"detail": "Password updated successfully."})


# ---------- Screen 2: Registration ----------

class RegisterView(APIView):
    permission_classes = [permissions.AllowAny]

    def post(self, request):
        allowed, retry_after = throttle_request(
            request, "register", limit=5, window_seconds=3600
        )
        if not allowed:
            return too_many_requests("Too many registration attempts.", retry_after)

        serializer = RegisterSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        user = serializer.save()
        register_or_update_device(user, request, request.data)
        return Response(auth_payload(user, request), status=status.HTTP_201_CREATED)


# ---------- Screen 3: Profile Setup ----------

class ProfileSetupView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        return Response(ProfileSerializer(request.user, context={"request": request}).data)

    def post(self, request):
        serializer = ProfileSetupSerializer(
            request.user,
            data=request.data,
            context={"request": request},
        )
        serializer.is_valid(raise_exception=True)
        user = serializer.save()
        return Response(ProfileSerializer(user, context={"request": request}).data)


# ---------- Profile ----------

class MyProfileView(generics.RetrieveUpdateAPIView):
    permission_classes = [permissions.IsAuthenticated]

    def get_object(self):
        return self.request.user

    def get_serializer_class(self):
        if self.request.method in ("PUT", "PATCH"):
            return ProfileUpdateSerializer
        return ProfileSerializer


class ProfileByExiIdView(generics.RetrieveAPIView):
    queryset = User.objects.all()
    serializer_class = PublicProfileSerializer
    permission_classes = [permissions.IsAuthenticated]
    lookup_field = "exi_id"


class OnlineStatusView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request):
        serializer = OnlineStatusSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        if serializer.validated_data["is_online"]:
            request.user.set_online()
        else:
            request.user.set_offline()

        return Response(ProfileSerializer(request.user, context={"request": request}).data)


# ---------- OTP Verification ----------

class SendOTPView(APIView):
    permission_classes = [permissions.AllowAny]

    def post(self, request):
        identifier = str(request.data.get("email_or_phone") or "").strip().lower()

        allowed, retry_after = throttle_request(request, "send-otp", limit=10, window_seconds=3600)
        if not allowed:
            return too_many_requests("Too many OTP requests. Try again later.", retry_after)

        if identifier:
            allowed, retry_after = throttle_request(
                request,
                "send-otp-identifier",
                limit=3,
                window_seconds=600,
                identifier=identifier,
            )
            if not allowed:
                return too_many_requests(
                    "An OTP was already sent. Wait before requesting another.", retry_after
                )

        serializer = SendOTPSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        return Response(serializer.save())


class VerifyOTPView(APIView):
    permission_classes = [permissions.AllowAny]

    def post(self, request):
        allowed, retry_after = throttle_request(request, "verify-otp", limit=15, window_seconds=900)
        if not allowed:
            return too_many_requests("Too many verification attempts.", retry_after)

        serializer = VerifyOTPSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        return Response(serializer.save())


# --- Appended from security_views.py ---

from django.contrib.auth import get_user_model
from rest_framework import generics, permissions, status
from rest_framework.response import Response
from rest_framework.views import APIView

from .utils import (
    available_prekey_count,
    get_or_create_privacy_settings,
    get_user_key_bundles,
    register_or_update_device,
    store_device_keys,
    MIN_PREKEY_COUNT,
)
from .models import BlockedUser, UserDevice
from .serializers import (
    BlockUserSerializer,
    BlockedUserSerializer,
    ChangePasswordSerializer,
    ChangePhoneSerializer,
    DeleteAccountSerializer,
    DeviceKeyUploadSerializer,
    DeviceRegisterSerializer,
    DeviceSerializer,
    PhoneChangeRequestSerializer,
    PhoneChangeVerifySerializer,
    PrivacySettingsSerializer,
    UsernameCheckSerializer,
    UsernameUpdateSerializer,
)
from .serializers import get_tokens, ProfileSerializer

User = get_user_model()


# ---------- Account: Username Management ----------

class UsernameCheckView(APIView):
    permission_classes = [permissions.AllowAny]

    def get(self, request):
        username = request.query_params.get("username", "")
        serializer = UsernameCheckSerializer(data={"username": username}, context={"request": request})
        serializer.is_valid(raise_exception=True)
        return Response(serializer.to_representation(serializer.validated_data))

    def post(self, request):
        serializer = UsernameCheckSerializer(data=request.data, context={"request": request})
        serializer.is_valid(raise_exception=True)
        return Response(serializer.to_representation(serializer.validated_data))


class UsernameUpdateView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request):
        serializer = UsernameUpdateSerializer(data=request.data, context={"request": request})
        serializer.is_valid(raise_exception=True)
        user = serializer.save()
        return Response(ProfileSerializer(user, context={"request": request}).data)

    put = post
    patch = post


# ---------- Account: Phone Number Management ----------

class PhoneChangeRequestView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request):
        serializer = PhoneChangeRequestSerializer(data=request.data, context={"request": request})
        serializer.is_valid(raise_exception=True)
        return Response(serializer.save())


class PhoneChangeVerifyView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request):
        serializer = PhoneChangeVerifySerializer(data=request.data, context={"request": request})
        serializer.is_valid(raise_exception=True)
        user = serializer.save()
        return Response(ProfileSerializer(user, context={"request": request}).data)


class ChangePhoneView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request):
        if "otp_code" in request.data:
            serializer = PhoneChangeVerifySerializer(data=request.data, context={"request": request})
        else:
            serializer = ChangePhoneSerializer(data=request.data, context={"request": request})
        serializer.is_valid(raise_exception=True)
        user = serializer.save()
        return Response(ProfileSerializer(user, context={"request": request}).data)

    put = post
    patch = post


# ---------- Account: Linked Devices ----------

class DeviceRegisterView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request):
        serializer = DeviceRegisterSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        device = register_or_update_device(request.user, request, serializer.validated_data)
        return Response(
            DeviceSerializer(device, context={'current_device_id': device.device_id}).data,
            status=status.HTTP_200_OK,
        )


class DeviceListView(generics.ListAPIView):
    serializer_class = DeviceSerializer
    permission_classes = [permissions.IsAuthenticated]

    def get_queryset(self):
        return UserDevice.objects.filter(user=self.request.user, is_active=True)

    def get_serializer_context(self):
        context = super().get_serializer_context()
        context['current_device_id'] = self.request.query_params.get('device_id', '')
        return context


class DeviceRemoveView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request, device_id):
        try:
            device = UserDevice.objects.get(
                user=request.user,
                device_id=device_id,
            )
        except UserDevice.DoesNotExist:
            return Response({'detail': 'Device not found.'}, status=status.HTTP_404_NOT_FOUND)
        return Response(
            DeviceSerializer(device, context={'current_device_id': device_id}).data
        )

    def delete(self, request, device_id):
        try:
            device = UserDevice.objects.get(
                user=request.user,
                device_id=device_id,
            )
        except UserDevice.DoesNotExist:
            return Response({'detail': 'Device not found.'}, status=status.HTTP_404_NOT_FOUND)

        device.is_active = False
        device.push_token = ''
        device.save(update_fields=['is_active', 'push_token'])
        return Response(status=status.HTTP_204_NO_CONTENT)


class LogoutOtherDevicesView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request):
        current_device_id = request.data.get('device_id', '')
        request.user.revoke_all_sessions()

        qs = UserDevice.objects.filter(user=request.user, is_active=True)
        if current_device_id:
            qs.exclude(device_id=current_device_id).update(is_active=False, push_token='')
        else:
            qs.update(is_active=False, push_token='')

        if current_device_id:
            UserDevice.objects.filter(
                user=request.user,
                device_id=current_device_id,
            ).update(is_active=True)

        tokens = get_tokens(request.user)
        return Response(
            {
                'detail': 'Logged out from all other devices.',
                **tokens,
            }
        )


class ChangePasswordView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request):
        serializer = ChangePasswordSerializer(data=request.data, context={'request': request})
        serializer.is_valid(raise_exception=True)
        serializer.save()
        tokens = get_tokens(request.user)
        return Response(
            {
                'detail': 'Password changed successfully. Other sessions have been revoked.',
                **tokens,
            }
        )


# ---------- Privacy: Settings, Last Seen, Online Status, Read Receipts, Typing, Profile Photo ----------

class PrivacySettingsView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        settings = get_or_create_privacy_settings(request.user)
        from .privacy_flow import get_privacy_flow_data
        data = PrivacySettingsSerializer(settings).data
        flow_data = get_privacy_flow_data(request.user)
        data["flow"] = flow_data
        data["sections"] = flow_data["sections"]
        return Response(data)

    def patch(self, request):
        settings = get_or_create_privacy_settings(request.user)
        serializer = PrivacySettingsSerializer(settings, data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        serializer.save()
        from .privacy_flow import update_privacy_flow_data
        update_privacy_flow_data(request.user, request.data)
        return self.get(request)

    put = patch


# ---------- Privacy: Blocked Users ----------

class BlockUserView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request, user_id=None):
        data = request.data.copy() if hasattr(request.data, 'copy') else dict(request.data)
        if user_id is not None:
            data['user_id'] = user_id

        serializer = BlockUserSerializer(data=data, context={'request': request})
        serializer.is_valid(raise_exception=True)
        blocked = serializer.validated_data['blocked_user']
        reason = serializer.validated_data.get('reason', '')

        block, created = BlockedUser.objects.get_or_create(
            blocker=request.user,
            blocked=blocked,
            defaults={'reason': reason},
        )
        if not created and reason:
            block.reason = reason
            block.save(update_fields=['reason'])

        return Response(
            BlockedUserSerializer(block).data,
            status=status.HTTP_201_CREATED if created else status.HTTP_200_OK,
        )

    def delete(self, request, user_id):
        try:
            block = BlockedUser.objects.get(
                blocker=request.user,
                blocked_id=user_id,
            )
        except BlockedUser.DoesNotExist:
            return Response({'detail': 'User is not blocked.'}, status=status.HTTP_404_NOT_FOUND)

        block.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)


class UnblockUserView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request, user_id=None):
        target_id = user_id or request.data.get('user_id')
        exi_id = request.data.get('exi_id')
        username = request.data.get('username')

        blocked_user = None
        if target_id:
            blocked_user = User.objects.filter(id=target_id).first()
        elif exi_id:
            blocked_user = User.objects.filter(exi_id=exi_id).first()
        elif username:
            blocked_user = User.objects.filter(username__iexact=username).first()

        if not blocked_user:
            return Response({'detail': 'User not found.'}, status=status.HTTP_404_NOT_FOUND)

        deleted_count, _ = BlockedUser.objects.filter(
            blocker=request.user,
            blocked=blocked_user,
        ).delete()

        if not deleted_count:
            return Response({'detail': 'User is not blocked.'}, status=status.HTTP_404_NOT_FOUND)

        return Response({'detail': 'User unblocked successfully.'}, status=status.HTTP_200_OK)

    delete = post


class BlockedUserListView(generics.ListAPIView):
    serializer_class = BlockedUserSerializer
    permission_classes = [permissions.IsAuthenticated]

    def get_queryset(self):
        return BlockedUser.objects.filter(blocker=self.request.user).select_related('blocked')


class DeactivateAccountView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request):
        user = request.user
        user.is_active = False
        user.set_offline()
        user.revoke_all_sessions()
        user.save(update_fields=['is_active', 'token_version'])
        UserDevice.objects.filter(user=user).update(is_active=False, push_token='')
        return Response({'detail': 'Account deactivated successfully.'})


class DeleteAccountView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def delete(self, request):
        serializer = DeleteAccountSerializer(data=request.data, context={'request': request})
        serializer.is_valid(raise_exception=True)

        user = request.user
        user.revoke_all_sessions()
        UserDevice.objects.filter(user=user).delete()
        BlockedUser.objects.filter(blocker=user).delete()
        BlockedUser.objects.filter(blocked=user).delete()
        user.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)


# ---------- End-to-end encryption key exchange ----------

class DeviceKeyUploadView(APIView):
    """Publish this device's public key bundle so peers can open E2EE sessions."""

    permission_classes = [permissions.IsAuthenticated]

    def post(self, request):
        serializer = DeviceKeyUploadSerializer(data=request.data, context={'request': request})
        serializer.is_valid(raise_exception=True)

        device = store_device_keys(
            serializer.context['device'],
            serializer.validated_data,
        )
        remaining = available_prekey_count(device)
        return Response(
            {
                'detail': 'Encryption keys published.',
                'device_id': device.device_id,
                'prekeys_available': remaining,
                'needs_more_prekeys': remaining < MIN_PREKEY_COUNT,
            }
        )

    def get(self, request):
        device_id = request.query_params.get('device_id', '')
        try:
            device = UserDevice.objects.get(user=request.user, device_id=device_id)
        except UserDevice.DoesNotExist:
            return Response({'detail': 'Device not found.'}, status=status.HTTP_404_NOT_FOUND)

        remaining = available_prekey_count(device)
        return Response(
            {
                'device_id': device.device_id,
                'has_encryption_keys': device.supports_e2ee,
                'prekeys_available': remaining,
                'needs_more_prekeys': remaining < MIN_PREKEY_COUNT,
                'keys_updated_at': device.keys_updated_at,
            }
        )


class UserKeyBundleView(APIView):
    """Fetch a peer's public key bundles (one one-time prekey per device is consumed)."""

    permission_classes = [permissions.IsAuthenticated]

    def get(self, request, exi_id):
        from chats.utils import is_blocked

        try:
            other = User.objects.get(exi_id=exi_id, is_active=True)
        except User.DoesNotExist:
            return Response({'detail': 'User not found.'}, status=status.HTTP_404_NOT_FOUND)

        if is_blocked(request.user, other):
            return Response(
                {'detail': 'Cannot fetch keys for this user.'},
                status=status.HTTP_403_FORBIDDEN,
            )

        bundles = get_user_key_bundles(other)
        if not bundles:
            return Response(
                {'detail': 'This user has no encryption-ready devices.'},
                status=status.HTTP_404_NOT_FOUND,
            )

        return Response({'exi_id': other.exi_id, 'devices': bundles})


# --- Appended: Module 10 — Admin panel APIs ---

from datetime import timedelta

from django.db.models import Count, Q
from django.db.models.functions import TruncDate
from django.utils import timezone
from rest_framework.permissions import IsAdminUser

from .models import AdminActivityLog, UserDevice
from .serializers import (
    AdminActivityLogSerializer,
    AdminBanSerializer,
    AdminReportSerializer,
    AdminReportUpdateSerializer,
    AdminUserDetailSerializer,
    AdminUserListSerializer,
    AdminUserUpdateSerializer,
)
from .utils import log_admin_action


def _admin_ban_user(user, reason=''):
    user.is_active = False
    user.set_offline()
    user.revoke_all_sessions()
    user.save(update_fields=['is_active', 'is_online', 'last_seen', 'token_version'])
    UserDevice.objects.filter(user=user).update(is_active=False, push_token='')
    return user


def _admin_unban_user(user):
    user.is_active = True
    user.save(update_fields=['is_active'])
    return user


class AdminUserListView(generics.ListAPIView):
    """List / search users for staff."""

    permission_classes = [permissions.IsAuthenticated, IsAdminUser]
    serializer_class = AdminUserListSerializer

    def get_queryset(self):
        qs = User.objects.all().annotate(
            reports_received_count=Count('reports_received', distinct=True),
            reports_filed_count=Count('reports_filed', distinct=True),
        )
        q = (self.request.query_params.get('q') or '').strip()
        if q:
            qs = qs.filter(
                Q(email__icontains=q)
                | Q(phone__icontains=q)
                | Q(full_name__icontains=q)
                | Q(display_name__icontains=q)
                | Q(exi_id__icontains=q)
                | Q(username__icontains=q)
            )
        is_active = self.request.query_params.get('is_active')
        if is_active in ('true', 'false', '1', '0'):
            qs = qs.filter(is_active=is_active in ('true', '1'))
        is_staff = self.request.query_params.get('is_staff')
        if is_staff in ('true', 'false', '1', '0'):
            qs = qs.filter(is_staff=is_staff in ('true', '1'))
        return qs.order_by('-date_joined')


class AdminUserDetailView(APIView):
    permission_classes = [permissions.IsAuthenticated, IsAdminUser]

    def get(self, request, user_id):
        try:
            user = User.objects.annotate(
                reports_received_count=Count('reports_received', distinct=True),
                reports_filed_count=Count('reports_filed', distinct=True),
            ).get(id=user_id)
        except User.DoesNotExist:
            return Response({'detail': 'User not found.'}, status=status.HTTP_404_NOT_FOUND)

        log_admin_action(
            request,
            action='user_view',
            summary=f'Viewed user {user.exi_id}',
            target_user=user,
            target_type='user',
            target_id=user.id,
        )
        return Response(AdminUserDetailSerializer(user, context={'request': request}).data)

    def patch(self, request, user_id):
        try:
            user = User.objects.get(id=user_id)
        except User.DoesNotExist:
            return Response({'detail': 'User not found.'}, status=status.HTTP_404_NOT_FOUND)

        if user.is_superuser and not request.user.is_superuser:
            return Response(
                {'detail': 'Cannot modify a superuser.'},
                status=status.HTTP_403_FORBIDDEN,
            )

        serializer = AdminUserUpdateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        changes = {}

        if 'is_staff' in data:
            if not request.user.is_superuser:
                return Response(
                    {'detail': 'Only superusers can change staff status.'},
                    status=status.HTTP_403_FORBIDDEN,
                )
            if user.id == request.user.id and data['is_staff'] is False:
                return Response(
                    {'detail': 'You cannot remove your own staff access.'},
                    status=status.HTTP_400_BAD_REQUEST,
                )
            user.is_staff = data['is_staff']
            changes['is_staff'] = data['is_staff']

        if 'is_active' in data:
            if user.id == request.user.id and data['is_active'] is False:
                return Response(
                    {'detail': 'You cannot deactivate your own account here.'},
                    status=status.HTTP_400_BAD_REQUEST,
                )
            user.is_active = data['is_active']
            changes['is_active'] = data['is_active']
            if not data['is_active']:
                user.revoke_all_sessions()
                UserDevice.objects.filter(user=user).update(is_active=False, push_token='')

        for field in ('display_name', 'full_name'):
            if field in data:
                setattr(user, field, data[field])
                changes[field] = data[field]

        if changes:
            user.save()
            log_admin_action(
                request,
                action='user_update',
                summary=f'Updated user {user.exi_id}',
                target_user=user,
                target_type='user',
                target_id=user.id,
                metadata={'changes': changes},
            )

        return Response(AdminUserDetailSerializer(user, context={'request': request}).data)


class AdminUserBanView(APIView):
    permission_classes = [permissions.IsAuthenticated, IsAdminUser]

    def post(self, request, user_id):
        try:
            user = User.objects.get(id=user_id)
        except User.DoesNotExist:
            return Response({'detail': 'User not found.'}, status=status.HTTP_404_NOT_FOUND)

        if user.id == request.user.id:
            return Response({'detail': 'You cannot ban yourself.'}, status=status.HTTP_400_BAD_REQUEST)
        if user.is_superuser and not request.user.is_superuser:
            return Response({'detail': 'Cannot ban a superuser.'}, status=status.HTTP_403_FORBIDDEN)

        serializer = AdminBanSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        reason = serializer.validated_data.get('reason', '')

        _admin_ban_user(user, reason)
        log_admin_action(
            request,
            action='user_ban',
            summary=f'Banned user {user.exi_id}',
            target_user=user,
            target_type='user',
            target_id=user.id,
            metadata={'reason': reason},
        )
        return Response(
            {
                'detail': 'User banned successfully.',
                'user': AdminUserListSerializer(user).data,
            }
        )


class AdminUserUnbanView(APIView):
    permission_classes = [permissions.IsAuthenticated, IsAdminUser]

    def post(self, request, user_id):
        try:
            user = User.objects.get(id=user_id)
        except User.DoesNotExist:
            return Response({'detail': 'User not found.'}, status=status.HTTP_404_NOT_FOUND)

        _admin_unban_user(user)
        log_admin_action(
            request,
            action='user_unban',
            summary=f'Unbanned user {user.exi_id}',
            target_user=user,
            target_type='user',
            target_id=user.id,
        )
        return Response(
            {
                'detail': 'User unbanned successfully.',
                'user': AdminUserListSerializer(user).data,
            }
        )


class AdminUserForceLogoutView(APIView):
    permission_classes = [permissions.IsAuthenticated, IsAdminUser]

    def post(self, request, user_id):
        try:
            user = User.objects.get(id=user_id)
        except User.DoesNotExist:
            return Response({'detail': 'User not found.'}, status=status.HTTP_404_NOT_FOUND)

        user.revoke_all_sessions()
        UserDevice.objects.filter(user=user).update(is_active=False, push_token='')
        log_admin_action(
            request,
            action='user_force_logout',
            summary=f'Forced logout for {user.exi_id}',
            target_user=user,
            target_type='user',
            target_id=user.id,
        )
        return Response({'detail': 'All sessions revoked for this user.'})


class AdminReportListView(generics.ListAPIView):
    permission_classes = [permissions.IsAuthenticated, IsAdminUser]
    serializer_class = AdminReportSerializer

    def get_queryset(self):
        from chats.models import Report

        qs = Report.objects.select_related('reporter', 'reported_user').all()
        status_filter = self.request.query_params.get('status')
        if status_filter in ('pending', 'reviewed', 'resolved'):
            qs = qs.filter(status=status_filter)
        reason = self.request.query_params.get('reason')
        if reason:
            qs = qs.filter(reason=reason)
        reported_user = self.request.query_params.get('reported_user')
        if reported_user:
            qs = qs.filter(reported_user_id=reported_user)
        return qs.order_by('-created_at')


class AdminReportDetailView(APIView):
    permission_classes = [permissions.IsAuthenticated, IsAdminUser]

    def get(self, request, report_id):
        from chats.models import Report

        try:
            report = Report.objects.select_related('reporter', 'reported_user').get(id=report_id)
        except Report.DoesNotExist:
            return Response({'detail': 'Report not found.'}, status=status.HTTP_404_NOT_FOUND)
        return Response(AdminReportSerializer(report).data)

    def patch(self, request, report_id):
        from chats.models import Report

        try:
            report = Report.objects.select_related('reporter', 'reported_user').get(id=report_id)
        except Report.DoesNotExist:
            return Response({'detail': 'Report not found.'}, status=status.HTTP_404_NOT_FOUND)

        serializer = AdminReportUpdateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        old_status = report.status
        report.status = data['status']
        notes_extra = (data.get('resolution_notes') or '').strip()
        if notes_extra:
            stamp = timezone.now().isoformat()
            addition = f'\n[staff {request.user.exi_id} @ {stamp}] {notes_extra}'
            report.notes = (report.notes or '') + addition
        report.save(update_fields=['status', 'notes'])

        banned = False
        if data.get('ban_user'):
            target = report.reported_user
            if target.id != request.user.id and not (target.is_superuser and not request.user.is_superuser):
                _admin_ban_user(target, reason=f'Report {report.id}')
                banned = True
                log_admin_action(
                    request,
                    action='user_ban',
                    summary=f'Banned user {target.exi_id} via report {report.id}',
                    target_user=target,
                    target_type='report',
                    target_id=report.id,
                    metadata={'report_id': str(report.id)},
                )

        action_map = {
            'reviewed': 'report_review',
            'resolved': 'report_resolve',
            'pending': 'moderation_action',
        }
        log_admin_action(
            request,
            action=action_map.get(report.status, 'moderation_action'),
            summary=f'Report {report.id}: {old_status} → {report.status}',
            target_user=report.reported_user,
            target_type='report',
            target_id=report.id,
            metadata={
                'old_status': old_status,
                'new_status': report.status,
                'banned': banned,
            },
        )

        return Response(
            {
                'detail': 'Report updated.',
                'report': AdminReportSerializer(report).data,
                'user_banned': banned,
            }
        )


class AdminModerationOverviewView(APIView):
    """Basic moderation dashboard counters."""

    permission_classes = [permissions.IsAuthenticated, IsAdminUser]

    def get(self, request):
        from chats.models import Report

        pending = Report.objects.filter(status='pending').count()
        reviewed = Report.objects.filter(status='reviewed').count()
        resolved = Report.objects.filter(status='resolved').count()
        banned_users = User.objects.filter(is_active=False).count()
        recent_reports = Report.objects.select_related(
            'reporter', 'reported_user'
        ).order_by('-created_at')[:10]

        log_admin_action(
            request,
            action='moderation_action',
            summary='Opened moderation overview',
            target_type='moderation',
        )

        return Response(
            {
                'pending_reports': pending,
                'reviewed_reports': reviewed,
                'resolved_reports': resolved,
                'banned_users': banned_users,
                'active_users': User.objects.filter(is_active=True).count(),
                'online_users': User.objects.filter(is_online=True, is_active=True).count(),
                'recent_reports': AdminReportSerializer(recent_reports, many=True).data,
            }
        )


class AdminActivityLogListView(generics.ListAPIView):
    permission_classes = [permissions.IsAuthenticated, IsAdminUser]
    serializer_class = AdminActivityLogSerializer

    def get_queryset(self):
        qs = AdminActivityLog.objects.select_related('actor', 'target_user').all()
        action = self.request.query_params.get('action')
        if action:
            qs = qs.filter(action=action)
        actor_id = self.request.query_params.get('actor')
        if actor_id:
            qs = qs.filter(actor_id=actor_id)
        target_user = self.request.query_params.get('target_user')
        if target_user:
            qs = qs.filter(target_user_id=target_user)
        return qs.order_by('-created_at')


class AdminAnalyticsView(APIView):
    """Usage analytics aggregated from existing tables."""

    permission_classes = [permissions.IsAuthenticated, IsAdminUser]

    def get(self, request):
        from chats.models import Call, Conversation, Message, Report

        days = request.query_params.get('days', '30')
        try:
            days = max(1, min(int(days), 90))
        except (TypeError, ValueError):
            days = 30

        since = timezone.now() - timedelta(days=days)

        users_total = User.objects.count()
        users_active = User.objects.filter(is_active=True).count()
        users_new = User.objects.filter(date_joined__gte=since).count()
        messages_total = Message.objects.count()
        messages_period = Message.objects.filter(created_at__gte=since).count()
        conversations_total = Conversation.objects.count()
        calls_total = Call.objects.count()
        calls_period = Call.objects.filter(started_at__gte=since).count()
        reports_pending = Report.objects.filter(status='pending').count()
        devices_active = UserDevice.objects.filter(is_active=True).count()

        signups_by_day = list(
            User.objects.filter(date_joined__gte=since)
            .annotate(day=TruncDate('date_joined'))
            .values('day')
            .annotate(count=Count('id'))
            .order_by('day')
        )
        messages_by_day = list(
            Message.objects.filter(created_at__gte=since)
            .annotate(day=TruncDate('created_at'))
            .values('day')
            .annotate(count=Count('id'))
            .order_by('day')
        )
        calls_by_day = list(
            Call.objects.filter(started_at__gte=since)
            .annotate(day=TruncDate('started_at'))
            .values('day')
            .annotate(count=Count('id'))
            .order_by('day')
        )
        reports_by_reason = list(
            Report.objects.values('reason').annotate(count=Count('id')).order_by('-count')
        )
        message_types = list(
            Message.objects.values('message_type').annotate(count=Count('id')).order_by('-count')
        )

        log_admin_action(
            request,
            action='analytics_view',
            summary=f'Viewed analytics ({days}d)',
            target_type='analytics',
            metadata={'days': days},
        )

        return Response(
            {
                'period_days': days,
                'totals': {
                    'users': users_total,
                    'active_users': users_active,
                    'banned_users': users_total - users_active,
                    'new_users': users_new,
                    'messages': messages_total,
                    'messages_in_period': messages_period,
                    'conversations': conversations_total,
                    'calls': calls_total,
                    'calls_in_period': calls_period,
                    'pending_reports': reports_pending,
                    'active_devices': devices_active,
                    'online_users': User.objects.filter(is_online=True, is_active=True).count(),
                },
                'signups_by_day': [
                    {'day': row['day'].isoformat() if row['day'] else None, 'count': row['count']}
                    for row in signups_by_day
                ],
                'messages_by_day': [
                    {'day': row['day'].isoformat() if row['day'] else None, 'count': row['count']}
                    for row in messages_by_day
                ],
                'calls_by_day': [
                    {'day': row['day'].isoformat() if row['day'] else None, 'count': row['count']}
                    for row in calls_by_day
                ],
                'reports_by_reason': reports_by_reason,
                'messages_by_type': message_types,
            }
        )



from .models import normalize_phone
from .user_preferences import (
    clear_user_media,
    create_chat_backup,
    delete_manage_storage_items,
    export_user_chat_history,
    format_file_size,
    get_chat_storage_breakdown,
    get_large_files,
    get_manage_storage_category,
    get_manage_storage_overview,
    get_section_preferences,
    get_storage_usage,
    get_user_preferences,
    update_section_preferences,
    update_user_preferences,
)
from .utils import (
    are_contacts,
    get_or_create_privacy_settings,
)


# ==========================================
# 0. Unified Settings View
# ==========================================

class UnifiedSettingsView(APIView):
    """Get or update all user settings in one call (notifications, chats, appearance, storage, privacy)."""
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        user_id = request.user.id
        prefs = get_user_preferences(user_id)
        privacy = get_or_create_privacy_settings(request.user)

        from .serializers import PrivacySettingsSerializer

        return Response({
            "notifications": prefs.get("notifications", {}),
            "chats": prefs.get("chats", {}),
            "appearance": prefs.get("appearance", {}),
            "storage": prefs.get("storage", {}),
            "privacy": PrivacySettingsSerializer(privacy).data,
        })

    def patch(self, request):
        user_id = request.user.id
        data = request.data.copy() if hasattr(request.data, "copy") else dict(request.data)

        # Handle privacy if provided
        privacy_data = data.pop("privacy", None)
        if privacy_data:
            from .serializers import PrivacySettingsSerializer
            privacy = get_or_create_privacy_settings(request.user)
            p_serializer = PrivacySettingsSerializer(privacy, data=privacy_data, partial=True)
            p_serializer.is_valid(raise_exception=True)
            p_serializer.save()

        updated_prefs = update_user_preferences(user_id, data)
        privacy = get_or_create_privacy_settings(request.user)
        from .serializers import PrivacySettingsSerializer

        return Response({
            "notifications": updated_prefs.get("notifications", {}),
            "chats": updated_prefs.get("chats", {}),
            "appearance": updated_prefs.get("appearance", {}),
            "storage": updated_prefs.get("storage", {}),
            "privacy": PrivacySettingsSerializer(privacy).data,
            "detail": "Settings updated successfully."
        })

    put = patch


# ==========================================
# 1. Notifications Settings
# ==========================================

class NotificationSettingsView(APIView):
    """
    Manage Notification Settings:
    - Message Alerts, Group Alerts, Call Alerts
    - Sounds, Vibration, Preview, Mute All
    """
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        prefs = get_section_preferences(request.user.id, "notifications")
        return Response(prefs)

    def patch(self, request):
        data = request.data
        if not isinstance(data, dict):
            return Response({"detail": "Invalid data format."}, status=status.HTTP_400_BAD_REQUEST)

        # Validation for vibration options
        valid_vibrations = ["default", "short", "long", "off"]
        for vib_key in ["message_vibration", "group_vibration", "call_vibration"]:
            if vib_key in data and data[vib_key] not in valid_vibrations:
                return Response(
                    {"detail": f"{vib_key} must be one of: {', '.join(valid_vibrations)}."},
                    status=status.HTTP_400_BAD_REQUEST,
                )

        updated = update_section_preferences(request.user.id, "notifications", data)
        return Response({
            "detail": "Notification settings updated successfully.",
            "settings": updated
        })

    put = patch


class NotificationMuteView(APIView):
    """Mute or unmute all notifications with predefined duration or custom timestamp."""
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request):
        action = request.data.get("action", "mute")  # "mute" or "unmute"
        duration = request.data.get("duration")  # "8_hours", "1_week", "always", "off" or minutes (int)
        until = request.data.get("until")

        if action == "unmute" or duration == "off":
            updated = update_section_preferences(request.user.id, "notifications", {
                "mute_all": False,
                "mute_until": None,
            })
            return Response({
                "detail": "Notifications unmuted successfully.",
                "is_muted": False,
                "settings": updated,
            })

        mute_until_iso = None
        if until:
            mute_until_iso = str(until)
        elif duration == "8_hours":
            mute_until_iso = (timezone.now() + timedelta(hours=8)).isoformat()
        elif duration == "1_week":
            mute_until_iso = (timezone.now() + timedelta(days=7)).isoformat()
        elif duration == "always":
            mute_until_iso = None
        elif isinstance(duration, (int, float)):
            mute_until_iso = (timezone.now() + timedelta(minutes=int(duration))).isoformat()

        updated = update_section_preferences(request.user.id, "notifications", {
            "mute_all": True,
            "mute_until": mute_until_iso,
        })
        return Response({
            "detail": "Notifications muted successfully.",
            "is_muted": True,
            "mute_until": mute_until_iso,
            "settings": updated,
        })


# ==========================================
# 2. Chats Settings
# ==========================================

class ChatPreferencesView(APIView):
    """
    Manage Chat Settings:
    - Disappearing Messages default timer
    - Enter to Send
    - Keep Chats Archived
    - Auto Backup Frequency
    """
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        prefs = get_section_preferences(request.user.id, "chats")
        return Response(prefs)

    def patch(self, request):
        data = request.data
        if not isinstance(data, dict):
            return Response({"detail": "Invalid data format."}, status=status.HTTP_400_BAD_REQUEST)

        valid_frequencies = ["off", "daily", "weekly", "monthly"]
        if "auto_backup_frequency" in data and data["auto_backup_frequency"] not in valid_frequencies:
            return Response(
                {"detail": f"auto_backup_frequency must be one of: {', '.join(valid_frequencies)}."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        updated = update_section_preferences(request.user.id, "chats", data)
        return Response({
            "detail": "Chat settings updated successfully.",
            "settings": updated
        })

    put = patch


class ChatBackupView(APIView):
    """Get backup status or create a new manual chat backup."""
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        prefs = get_section_preferences(request.user.id, "chats")
        return Response({
            "last_backup_at": prefs.get("last_backup_at"),
            "last_backup_size": prefs.get("last_backup_size", 0),
            "last_backup_size_kb": round(prefs.get("last_backup_size", 0) / 1024, 2),
            "auto_backup_frequency": prefs.get("auto_backup_frequency", "off"),
            "backup_over_wifi_only": prefs.get("backup_over_wifi_only", True),
            "include_videos": prefs.get("include_videos", True),
        })

    def post(self, request):
        include_videos = request.data.get("include_videos", True)
        result = create_chat_backup(request.user, include_videos=bool(include_videos))
        return Response({
            "detail": "Chat backup created successfully.",
            **result["backup_info"],
            "backup_payload": result["backup_payload"],
        }, status=status.HTTP_201_CREATED)


class ChatRestoreView(APIView):
    """Restore chat messages and preferences from backup data."""
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request):
        backup_data = request.data.get("backup_payload") or request.data
        if not isinstance(backup_data, dict) or "conversations" not in backup_data:
            return Response(
                {"detail": "Invalid backup payload format. 'conversations' key required."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        # Restore settings if present
        if "settings" in backup_data and isinstance(backup_data["settings"], dict):
            update_user_preferences(request.user.id, backup_data["settings"])

        return Response({
            "detail": "Chat backup restored successfully.",
            "total_conversations_restored": len(backup_data.get("conversations", [])),
            "total_messages_restored": backup_data.get("total_messages", 0),
        })


class ChatHistoryExportView(APIView):
    """Export formatted chat history (all or specific chat)."""
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        chat_id = request.query_params.get("chat_id")
        export_data = export_user_chat_history(request.user, chat_id=chat_id)
        return Response(export_data)


class ChatHistoryClearAllView(APIView):
    """Clear message history across all user conversations."""
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request):
        from chats.models import ConversationParticipant
        count = ConversationParticipant.objects.filter(
            user=request.user,
            deleted_at__isnull=True
        ).update(cleared_at=timezone.now())

        return Response({
            "detail": "All chat histories cleared successfully.",
            "cleared_chats_count": count,
        })


class ChatHistoryDeleteAllView(APIView):
    """Delete all conversations for the user."""
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request):
        from chats.models import ConversationParticipant
        count = ConversationParticipant.objects.filter(
            user=request.user,
            deleted_at__isnull=True
        ).update(
            deleted_at=timezone.now(),
            is_pinned=False,
            is_archived=False,
        )

        return Response({
            "detail": "All chats deleted successfully.",
            "deleted_chats_count": count,
        })


class ChatArchiveAllView(APIView):
    """Archive all active chats for the user."""
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request):
        from chats.models import ConversationParticipant
        count = ConversationParticipant.objects.filter(
            user=request.user,
            deleted_at__isnull=True,
            is_archived=False,
        ).update(is_archived=True)

        return Response({
            "detail": "All active chats archived successfully.",
            "archived_count": count,
        })


class ChatUnarchiveAllView(APIView):
    """Unarchive all archived chats for the user."""
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request):
        from chats.models import ConversationParticipant
        count = ConversationParticipant.objects.filter(
            user=request.user,
            is_archived=True,
        ).update(is_archived=False)

        return Response({
            "detail": "All chats unarchived successfully.",
            "unarchived_count": count,
        })


# ==========================================
# 3. Appearance Settings
# ==========================================

class AppearanceSettingsView(APIView):
    """
    Manage Appearance Settings:
    - Light / Dark / System Theme Mode
    - Chat Theme & Accent Colors
    - Wallpaper Preset / Custom URL / Opacity
    - Font Size
    """
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        prefs = get_section_preferences(request.user.id, "appearance")
        return Response(prefs)

    def patch(self, request):
        data = request.data
        if not isinstance(data, dict):
            return Response({"detail": "Invalid data format."}, status=status.HTTP_400_BAD_REQUEST)

        valid_modes = ["light", "dark", "system"]
        theme_val = data.get("theme_mode") or data.get("theme")
        if theme_val and theme_val not in valid_modes:
            return Response(
                {"detail": f"theme_mode must be one of: {', '.join(valid_modes)}."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        valid_font_sizes = ["small", "medium", "large", "extra_large"]
        if "font_size" in data and data["font_size"] not in valid_font_sizes:
            return Response(
                {"detail": f"font_size must be one of: {', '.join(valid_font_sizes)}."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        valid_chat_themes = ["classic", "emerald", "ocean", "sunset", "midnight", "rose", "lavender", "custom"]
        if "chat_theme" in data and data["chat_theme"] not in valid_chat_themes:
            return Response(
                {"detail": f"chat_theme must be one of: {', '.join(valid_chat_themes)}."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        updated = update_section_preferences(request.user.id, "appearance", data)
        return Response({
            "detail": "Appearance settings updated successfully.",
            "settings": updated
        })

    put = patch


# ==========================================
# 4. Storage Settings & Management
# ==========================================

def to_bool(val, default=False):
    """Robustly parse boolean from json, form-data, or query parameters."""
    if val is None:
        return default
    if isinstance(val, bool):
        return val
    if isinstance(val, (int, float)):
        return bool(val)
    return str(val).lower().strip() in ("true", "1", "t", "yes")


class StorageSettingsView(APIView):
    """
    10.4 Auto-Download Preferences & Low Data Usage:
    Separate network conditions:
    - mobile_data: [photos, audio, videos, documents]
    - wifi: [photos, audio, videos, documents]
    - roaming: [photos, audio, videos, documents]
    - low_data_usage_calls: bool
    """
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        from .user_preferences import get_storage_data_flow
        prefs = get_section_preferences(request.user.id, "storage")
        usage_summary = get_storage_usage(request.user)
        flow_data = get_storage_data_flow(request.user)
        return Response({
            "settings": prefs,
            "usage_summary": usage_summary,
            "flow": flow_data["flow"],
            "cache_and_temp": flow_data["flow"]["clear_cache_and_temp"],
            "auto_download_wifi": flow_data["flow"]["auto_download_wifi"],
            "auto_download_cellular": flow_data["flow"]["auto_download_cellular"],
        })

    def patch(self, request):
        from .user_preferences import update_storage_data_flow, get_storage_data_flow
        if hasattr(request.data, "getlist"):
            data = {k: request.data.getlist(k) if len(request.data.getlist(k)) > 1 else request.data.get(k) for k in request.data.keys()}
        else:
            data = request.data.copy() if hasattr(request.data, "copy") else dict(request.data)
        if not isinstance(data, dict):
            return Response({"detail": "Invalid data format."}, status=status.HTTP_400_BAD_REQUEST)

        valid_categories = {"photos", "photo", "image", "videos", "video", "documents", "document", "doc", "audio", "voice"}

        # Validate network condition lists
        for net_key in ["mobile_data", "cellular", "wifi", "roaming", "auto_download_mobile", "auto_download_cellular", "auto_download_wifi", "auto_download_roaming"]:
            if net_key in data:
                val = data[net_key]
                if isinstance(val, str):
                    val = [v.strip() for v in val.split(",") if v.strip()]
                    data[net_key] = val
                if not isinstance(val, list):
                    return Response(
                        {"detail": f"{net_key} must be a list of media types (photos, videos, documents, audio)."},
                        status=status.HTTP_400_BAD_REQUEST,
                    )
                for item in val:
                    if str(item).lower() not in valid_categories:
                        return Response(
                            {"detail": f"Invalid media type '{item}' in {net_key}. Allowed: photos, videos, documents, audio."},
                            status=status.HTTP_400_BAD_REQUEST,
                        )

        if "low_data_usage_calls" in data:
            data["low_data_usage_calls"] = to_bool(data["low_data_usage_calls"], False)

        # Sync cellular and mobile_data
        if "cellular" in data and "mobile_data" not in data:
            data["mobile_data"] = data["cellular"]
        elif "mobile_data" in data and "cellular" not in data:
            data["cellular"] = data["mobile_data"]

        updated = update_section_preferences(request.user.id, "storage", data)
        flow_data = get_storage_data_flow(request.user)
        return Response({
            "detail": "Storage settings updated successfully.",
            "settings": updated,
            "flow": flow_data["flow"],
        })

    put = patch


class StorageDataFlowView(APIView):
    """
    Storage & Data backend flow:
    - Clear Cache & Temporary Data
    - Auto-download Media on Wi-Fi
    - Auto-download on Cellular

    GET   /api/settings/storage/flow/  — Get full flow structure
    PATCH /api/settings/storage/flow/  — Update settings or clear cache
    """
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        from .user_preferences import get_storage_data_flow
        return Response(get_storage_data_flow(request.user))

    def patch(self, request):
        from .user_preferences import update_storage_data_flow, get_storage_data_flow
        updated = update_storage_data_flow(request.user, request.data)
        return Response({
            "detail": "Storage & Data settings updated successfully.",
            **updated,
        })

    put = patch


class StorageCacheView(APIView):
    """
    Cache & Temporary Data:
    GET  /api/settings/storage/cache/        — Get current cache & temp file usage
    POST /api/settings/storage/clear-cache/   — Clear cache & temporary data
    POST /api/settings/storage/cache/clear/  — Clear cache & temporary data
    """
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        from .user_preferences import get_cache_and_temp_usage
        usage = get_cache_and_temp_usage(request.user)
        return Response({
            "title": "Clear Cache & Temporary Data",
            "description": "Frees up storage used by cached files, temporary media, and preview thumbnails without deleting your chat messages or saved media.",
            "can_clear": True,
            **usage,
        })

    def post(self, request):
        from .user_preferences import clear_cache_and_temp_data
        result = clear_cache_and_temp_data(request.user)
        return Response(result)


class StorageWifiAutoDownloadView(APIView):
    """
    Auto-download Media on Wi-Fi:
    GET   /api/settings/storage/wifi/  — Get Wi-Fi auto-download preferences
    PATCH /api/settings/storage/wifi/  — Update Wi-Fi auto-download preferences
    """
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        from .user_preferences import get_storage_data_flow
        flow = get_storage_data_flow(request.user)
        return Response(flow["flow"]["auto_download_wifi"])

    def patch(self, request):
        from .user_preferences import update_storage_data_flow, get_storage_data_flow
        val = request.data.get("selected") or request.data.get("media_types") or request.data
        updated = update_storage_data_flow(request.user, {
            "auto_download_wifi": val,
        })
        return Response({
            "detail": "Wi-Fi auto-download settings updated successfully.",
            **updated["flow"]["auto_download_wifi"],
        })

    put = patch


class StorageCellularAutoDownloadView(APIView):
    """
    Auto-download on Cellular:
    GET   /api/settings/storage/cellular/  — Get Cellular auto-download preferences
    PATCH /api/settings/storage/cellular/  — Update Cellular auto-download preferences
    """
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        from .user_preferences import get_storage_data_flow
        flow = get_storage_data_flow(request.user)
        return Response(flow["flow"]["auto_download_cellular"])

    def patch(self, request):
        from .user_preferences import update_storage_data_flow, get_storage_data_flow
        val = request.data.get("selected") or request.data.get("media_types") or request.data
        updated = update_storage_data_flow(request.user, {
            "auto_download_cellular": val,
        })
        return Response({
            "detail": "Cellular auto-download settings updated successfully.",
            **updated["flow"]["auto_download_cellular"],
        })

    put = patch


class StorageUsageView(APIView):
    """
    10.1 Storage Usage:
    Returns categorized breakdown:
    Photos, Videos, Documents, Audio, Total.
    With raw bytes, item counts, and human-readable formatted sizes (e.g. 2.4 GB, 850 MB).
    """
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        usage = get_storage_usage(request.user)
        return Response(usage)


class StorageChatsView(APIView):
    """List chats ranked by storage usage with individual stats and formatted sizes."""
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        chats_breakdown = get_chat_storage_breakdown(request.user)
        return Response({
            "total_chats": len(chats_breakdown),
            "chats": chats_breakdown,
        })


class ManageStorageView(APIView):
    """
    10.2 Manage Storage Overview:
    Summarizes counts, total bytes, and formatted sizes for:
    - Large Files
    - Frequently Forwarded
    - Old Media
    - Unused Media
    """
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        days_threshold = request.query_params.get("days_threshold", "30")
        min_size_mb = request.query_params.get("min_size_mb", "5.0")
        try:
            days_threshold = int(days_threshold)
        except ValueError:
            days_threshold = 30
        try:
            min_size_mb = float(min_size_mb)
        except ValueError:
            min_size_mb = 5.0

        overview = get_manage_storage_overview(
            request.user,
            days_threshold=days_threshold,
            min_size_mb=min_size_mb
        )
        return Response(overview)


class ManageStorageCategoryView(APIView):
    """
    10.2 Manage Storage Category Listing:
    Lists items belonging to one of:
    - large_files
    - frequently_forwarded
    - old_media
    - unused_media
    Supports sorting: 'largest', 'newest', 'oldest'.
    Supports pagination: limit, offset.
    """
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        category = request.query_params.get("category", "large_files")
        sort = request.query_params.get("sort", "largest")
        limit = request.query_params.get("limit", "50")
        offset = request.query_params.get("offset", "0")
        days_threshold = request.query_params.get("days_threshold", "30")
        min_size_mb = request.query_params.get("min_size_mb", "5.0")

        try:
            limit = max(1, min(100, int(limit)))
            offset = max(0, int(offset))
            days_threshold = int(days_threshold)
            min_size_mb = float(min_size_mb)
        except ValueError:
            limit = 50
            offset = 0
            days_threshold = 30
            min_size_mb = 5.0

        data = get_manage_storage_category(
            user=request.user,
            category=category,
            sort=sort,
            limit=limit,
            offset=offset,
            days_threshold=days_threshold,
            min_size_mb=min_size_mb,
        )
        return Response(data)


class ManageStorageDeleteView(APIView):
    """
    10.2 Batch Delete Action:
    Allows:
    - Select Multiple: item_ids list
    - Select All: select_all=True with category
    CRITICAL MESSAGE RETENTION:
    Deleting downloaded media does NOT automatically delete the message
    unless delete_message=True is explicitly passed.
    """
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request):
        item_ids = request.data.get("item_ids") or []
        select_all = to_bool(request.data.get("select_all", False), False)
        category = request.data.get("category")
        delete_message = to_bool(request.data.get("delete_message", False), False)
        days_threshold = request.data.get("days_threshold", 30)
        min_size_mb = request.data.get("min_size_mb", 5.0)

        if not select_all and not item_ids:
            return Response(
                {"detail": "Either item_ids list or select_all=true with category is required."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        if select_all and not category:
            return Response(
                {"detail": "Category is required when select_all is true."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        result = delete_manage_storage_items(
            user=request.user,
            item_ids=item_ids,
            select_all=select_all,
            category=category,
            delete_message=delete_message,
            days_threshold=days_threshold,
            min_size_mb=min_size_mb,
        )

        return Response({
            "detail": "Selected items deleted successfully.",
            **result,
        })


class StorageLargeFilesView(APIView):
    """
    10.3 Large Files:
    List files exceeding size threshold (default 5MB or ?min_size_mb=).
    Supports sorting: 'largest', 'newest', 'oldest'.
    Supports media_type filter: 'photos', 'videos', 'documents', 'audio'.
    Supports pagination: page, page_size, limit, offset.
    """
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        min_mb = request.query_params.get("min_size_mb", "5.0")
        sort = request.query_params.get("sort", "largest")
        media_type = request.query_params.get("media_type")

        # Pagination parameters
        page = request.query_params.get("page")
        page_size = request.query_params.get("page_size") or request.query_params.get("limit", "50")

        try:
            min_mb_float = float(min_mb)
        except ValueError:
            min_mb_float = 5.0

        try:
            limit = max(1, min(100, int(page_size)))
        except ValueError:
            limit = 50

        if page:
            try:
                page_int = max(1, int(page))
                offset = (page_int - 1) * limit
            except ValueError:
                offset = 0
        else:
            try:
                offset = max(0, int(request.query_params.get("offset", "0")))
            except ValueError:
                offset = 0

        min_bytes = int(min_mb_float * 1024 * 1024)
        result = get_large_files(
            request.user,
            min_size_bytes=min_bytes,
            sort=sort,
            media_type=media_type,
            limit=limit,
            offset=offset,
        )

        return Response(result)


class StorageLargeFilesDeleteView(APIView):
    """
    10.3 Delete selected large media or message attachments.
    CRITICAL: delete_message=False preserves the message text and history.
    """
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request):
        file_ids = request.data.get("file_ids") or request.data.get("item_ids") or []
        delete_message = to_bool(request.data.get("delete_message", False), False)

        if not file_ids:
            return Response(
                {"detail": "file_ids list is required."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        result = delete_manage_storage_items(
            user=request.user,
            item_ids=file_ids,
            delete_message=delete_message,
        )

        return Response({
            "detail": "Files deleted successfully.",
            **result,
        })


class StorageClearMediaView(APIView):
    """
    10.5 Clear Media:
    Allows clearing:
    - clear_photos: bool
    - clear_videos: bool
    - clear_documents: bool
    - clear_audio: bool
    - media_types: list of categories
    CRITICAL MESSAGE RETENTION:
    Deleting downloaded media should NOT automatically delete the message
    unless the user explicitly chooses that (delete_message=True).
    """
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request):
        chat_id = request.data.get("chat_id")
        clear_photos = to_bool(request.data.get("clear_photos", False), False)
        clear_videos = to_bool(request.data.get("clear_videos", False), False)
        clear_documents = to_bool(request.data.get("clear_documents", False), False)
        clear_audio = to_bool(request.data.get("clear_audio", False), False)
        media_types = request.data.get("media_types")
        keep_starred = to_bool(request.data.get("keep_starred", True), True)
        older_than_days = request.data.get("older_than_days")
        delete_message = to_bool(request.data.get("delete_message", False), False)

        result = clear_user_media(
            user=request.user,
            chat_id=chat_id,
            clear_photos=clear_photos,
            clear_videos=clear_videos,
            clear_documents=clear_documents,
            clear_audio=clear_audio,
            media_types=media_types,
            keep_starred=keep_starred,
            older_than_days=older_than_days,
            delete_message=delete_message,
            dry_run=False,
        )

        return Response({
            "detail": "Media cleared successfully.",
            **result,
        })


class StorageClearMediaPreviewView(APIView):
    """
    10.5 Clear Media Preview:
    Simulates clearing media and returns estimated freed bytes, formatted sizes,
    and category breakdown without modifying any data.
    """
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request):
        chat_id = request.data.get("chat_id")
        clear_photos = to_bool(request.data.get("clear_photos", False), False)
        clear_videos = to_bool(request.data.get("clear_videos", False), False)
        clear_documents = to_bool(request.data.get("clear_documents", False), False)
        clear_audio = to_bool(request.data.get("clear_audio", False), False)
        media_types = request.data.get("media_types")
        keep_starred = to_bool(request.data.get("keep_starred", True), True)
        older_than_days = request.data.get("older_than_days")
        delete_message = to_bool(request.data.get("delete_message", False), False)

        result = clear_user_media(
            user=request.user,
            chat_id=chat_id,
            clear_photos=clear_photos,
            clear_videos=clear_videos,
            clear_documents=clear_documents,
            clear_audio=clear_audio,
            media_types=media_types,
            keep_starred=keep_starred,
            older_than_days=older_than_days,
            delete_message=delete_message,
            dry_run=True,
        )

        return Response({
            "detail": "Media clear preview calculated.",
            **result,
        })


# ============================================================
# SECURITY SECTION
# ============================================================

class SecurityFlowView(APIView):
    """
    Security settings master flow:
    1. App Security (App Lock, Change Passcode, Use Biometrics)
    2. Two-Step Verification (Enable Two-Step Verification)
    3. Encryption (End-to-End Encryption)
    4. Active Sessions (Manage Devices)
    5. Security Alerts (Show Security Notifications)

    GET   /api/security/flow/  — Full security flow
    PATCH /api/security/flow/  — Master update across sections
    """
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        from .user_preferences import get_security_flow_data
        return Response(get_security_flow_data(request.user, request))

    def patch(self, request):
        from .user_preferences import update_security_flow_data
        try:
            updated = update_security_flow_data(request.user, request.data, request)
        except ValueError as e:
            return Response({"detail": str(e)}, status=status.HTTP_400_BAD_REQUEST)
        return Response({
            "detail": "Security settings updated successfully.",
            **updated,
        })

    put = patch


class AppSecurityView(APIView):
    """
    App Security:
    - App Lock
    - Change Passcode
    - Use Biometrics

    GET   /api/security/app-security/
    PATCH /api/security/app-security/
    """
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        from .user_preferences import get_security_flow_data
        data = get_security_flow_data(request.user, request)
        return Response(data["flow"]["app_security"])

    def patch(self, request):
        from .user_preferences import update_security_flow_data
        try:
            updated = update_security_flow_data(request.user, {
                "app_security": request.data,
                **request.data,
            }, request)
        except ValueError as e:
            return Response({"detail": str(e)}, status=status.HTTP_400_BAD_REQUEST)
        return Response({
            "detail": "App Security settings updated successfully.",
            **updated["flow"]["app_security"],
        })

    put = patch


class ChangePasscodeView(APIView):
    """
    Passcode configuration & verification:
    POST /api/security/passcode/change/ — Set or change passcode (4-8 digits)
    POST /api/security/passcode/verify/ — Verify passcode
    """
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request):
        action = request.data.get("action", "change")
        pin = str(request.data.get("passcode") or request.data.get("pin") or "").strip()

        if action == "verify" or "verify" in request.path:
            from .user_preferences import verify_passcode
            if not pin:
                return Response({"detail": "Passcode is required."}, status=status.HTTP_400_BAD_REQUEST)
            if verify_passcode(pin, request.user.id):
                return Response({"detail": "Passcode verified successfully.", "verified": True})
            return Response({"detail": "Incorrect passcode.", "verified": False}, status=status.HTTP_400_BAD_REQUEST)

        # Set / change
        if not pin.isdigit() or not (4 <= len(pin) <= 8):
            return Response({"detail": "Passcode must be 4–8 digits."}, status=status.HTTP_400_BAD_REQUEST)

        from .user_preferences import update_security_flow_data
        try:
            update_security_flow_data(request.user, {
                "passcode": pin,
                "current_passcode": request.data.get("current_passcode") or request.data.get("current_pin"),
            }, request)
        except ValueError as e:
            return Response({"detail": str(e)}, status=status.HTTP_400_BAD_REQUEST)

        return Response({
            "detail": "Passcode changed successfully.",
            "passcode_set": True,
            "app_lock_enabled": True,
        })


class EncryptionInfoView(APIView):
    """
    End-to-End Encryption:
    GET /api/security/encryption/ — Status, protocol, safety numbers, verified keys
    """
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        from .user_preferences import get_security_flow_data
        data = get_security_flow_data(request.user, request)
        return Response(data["flow"]["encryption"])


class SecurityDevicesManageView(APIView):
    """
    Active Sessions / Manage Devices:
    GET  /api/security/devices/               — List active sessions
    POST /api/security/devices/logout-others/ — Terminate all other sessions
    """
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        from .user_preferences import get_security_flow_data
        data = get_security_flow_data(request.user, request)
        return Response(data["flow"]["active_sessions"])

    def post(self, request):
        from accounts.models import UserDevice
        current_device_id = request.data.get("current_device_id") or request.query_params.get("device_id")
        qs = UserDevice.objects.filter(user=request.user, is_active=True)
        if current_device_id:
            qs = qs.exclude(device_id=current_device_id)
        terminated_count = qs.update(is_active=False)
        return Response({
            "detail": f"Successfully logged out {terminated_count} other session(s).",
            "terminated_count": terminated_count,
        })


class SecurityAlertsView(APIView):
    """
    Security Alerts:
    GET   /api/security/alerts/  — Get security notifications setting
    PATCH /api/security/alerts/  — Update settings
    """
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        from .user_preferences import get_security_flow_data
        data = get_security_flow_data(request.user, request)
        return Response(data["flow"]["security_alerts"])

    def patch(self, request):
        from .user_preferences import update_security_flow_data
        updated = update_security_flow_data(request.user, {
            "security_alerts": request.data,
            **request.data,
        }, request)
        return Response({
            "detail": "Security alerts settings updated successfully.",
            **updated["flow"]["security_alerts"],
        })

    put = patch


# ----------------------------------------
# App Lock — PIN / Biometrics
# ----------------------------------------

class AppLockSettingsView(APIView):
    """
    GET  /api/security/app-lock/  — Get App Lock status
    POST /api/security/app-lock/  — Enable/disable App Lock + set method
    """
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        prefs = get_section_preferences(request.user.id, "security")
        return Response({
            "app_lock_enabled": prefs.get("app_lock_enabled", False),
            "app_lock_method": prefs.get("app_lock_method", "pin"),   # "pin" | "biometrics" | "both"
            "lock_after_seconds": prefs.get("lock_after_seconds", 60),
            "lock_on_background": prefs.get("lock_on_background", True),
            "pin_set": bool(prefs.get("app_lock_pin_hash")),
        })

    def post(self, request):
        data = request.data
        prefs = get_section_preferences(request.user.id, "security")

        enabled = data.get("app_lock_enabled")
        if enabled is not None:
            prefs["app_lock_enabled"] = bool(enabled)

        method = data.get("app_lock_method")
        if method is not None:
            if method not in ("pin", "biometrics", "both"):
                return Response(
                    {"detail": "app_lock_method must be 'pin', 'biometrics', or 'both'."},
                    status=status.HTTP_400_BAD_REQUEST,
                )
            prefs["app_lock_method"] = method

        lock_after = data.get("lock_after_seconds")
        if lock_after is not None:
            try:
                prefs["lock_after_seconds"] = max(0, int(lock_after))
            except (TypeError, ValueError):
                return Response({"detail": "lock_after_seconds must be an integer."}, status=status.HTTP_400_BAD_REQUEST)

        lock_bg = data.get("lock_on_background")
        if lock_bg is not None:
            prefs["lock_on_background"] = bool(lock_bg)

        update_section_preferences(request.user.id, "security", prefs)
        return Response({"detail": "App Lock settings updated.", **{
            k: prefs[k] for k in ("app_lock_enabled", "app_lock_method", "lock_after_seconds", "lock_on_background")
            if k in prefs
        }})


class AppLockPinSetView(APIView):
    """
    POST /api/security/app-lock/pin/
    Set or change the App Lock PIN (4–8 digits).
    Body: { pin: str, current_pin: str (required when changing) }
    """
    permission_classes = [permissions.IsAuthenticated]

    def _hash_pin(self, pin, user_id):
        return hashlib.sha256(f"{user_id}:{pin}".encode()).hexdigest()

    def post(self, request):
        pin = str(request.data.get("pin") or "").strip()
        if not pin.isdigit() or not (4 <= len(pin) <= 8):
            return Response({"detail": "PIN must be 4–8 digits."}, status=status.HTTP_400_BAD_REQUEST)

        prefs = get_section_preferences(request.user.id, "security")
        existing_hash = prefs.get("app_lock_pin_hash")

        if existing_hash:
            current_pin = str(request.data.get("current_pin") or "").strip()
            if not current_pin:
                return Response({"detail": "current_pin is required when changing PIN."}, status=status.HTTP_400_BAD_REQUEST)
            if self._hash_pin(current_pin, request.user.id) != existing_hash:
                return Response({"detail": "Incorrect current PIN."}, status=status.HTTP_400_BAD_REQUEST)

        prefs["app_lock_pin_hash"] = self._hash_pin(pin, request.user.id)
        prefs["app_lock_enabled"] = True
        prefs.setdefault("app_lock_method", "pin")
        update_section_preferences(request.user.id, "security", prefs)
        return Response({"detail": "App Lock PIN set successfully.", "app_lock_enabled": True})


class AppLockPinVerifyView(APIView):
    """
    POST /api/security/app-lock/verify/
    Verify the App Lock PIN.
    Body: { pin: str }
    """
    permission_classes = [permissions.IsAuthenticated]

    def _hash_pin(self, pin, user_id):
        return hashlib.sha256(f"{user_id}:{pin}".encode()).hexdigest()

    def post(self, request):
        pin = str(request.data.get("pin") or "").strip()
        prefs = get_section_preferences(request.user.id, "security")
        stored_hash = prefs.get("app_lock_pin_hash")

        if not stored_hash:
            return Response({"detail": "No PIN configured."}, status=status.HTTP_400_BAD_REQUEST)

        if self._hash_pin(pin, request.user.id) == stored_hash:
            return Response({"detail": "PIN verified.", "verified": True})
        return Response({"detail": "Incorrect PIN.", "verified": False}, status=status.HTTP_400_BAD_REQUEST)


# ----------------------------------------
# Two-Step Verification
# ----------------------------------------

class TwoStepVerificationView(APIView):
    """
    GET  /api/security/two-step/  — Get 2SV status
    POST /api/security/two-step/  — Enable/disable 2SV or set PIN
    """
    permission_classes = [permissions.IsAuthenticated]

    def _hash_pin(self, pin, user_id):
        return hashlib.sha256(f"2sv:{user_id}:{pin}".encode()).hexdigest()

    def get(self, request):
        prefs = get_section_preferences(request.user.id, "security")
        return Response({
            "two_step_enabled": prefs.get("two_step_enabled", False),
            "two_step_pin_set": bool(prefs.get("two_step_pin_hash")),
            "two_step_email": prefs.get("two_step_email", ""),
            "last_changed_at": prefs.get("two_step_last_changed_at"),
        })

    def post(self, request):
        action = request.data.get("action", "update")
        prefs = get_section_preferences(request.user.id, "security")

        if action == "enable":
            pin = str(request.data.get("pin") or "").strip()
            if not pin.isdigit() or not (4 <= len(pin) <= 8):
                return Response({"detail": "PIN must be 4–8 digits."}, status=status.HTTP_400_BAD_REQUEST)
            email = (request.data.get("email") or "").strip()
            prefs["two_step_pin_hash"] = self._hash_pin(pin, request.user.id)
            prefs["two_step_email"] = email[:254]
            prefs["two_step_enabled"] = True
            prefs["two_step_last_changed_at"] = timezone.now().isoformat()
            update_section_preferences(request.user.id, "security", prefs)
            return Response({"detail": "Two-step verification enabled.", "two_step_enabled": True})

        elif action == "disable":
            pin = str(request.data.get("pin") or "").strip()
            stored_hash = prefs.get("two_step_pin_hash")
            if stored_hash and self._hash_pin(pin, request.user.id) != stored_hash:
                return Response({"detail": "Incorrect PIN."}, status=status.HTTP_400_BAD_REQUEST)
            prefs["two_step_enabled"] = False
            update_section_preferences(request.user.id, "security", prefs)
            return Response({"detail": "Two-step verification disabled.", "two_step_enabled": False})

        elif action == "change_pin":
            current_pin = str(request.data.get("current_pin") or "").strip()
            new_pin = str(request.data.get("new_pin") or "").strip()
            stored_hash = prefs.get("two_step_pin_hash")

            if stored_hash and self._hash_pin(current_pin, request.user.id) != stored_hash:
                return Response({"detail": "Incorrect current PIN."}, status=status.HTTP_400_BAD_REQUEST)
            if not new_pin.isdigit() or not (4 <= len(new_pin) <= 8):
                return Response({"detail": "New PIN must be 4–8 digits."}, status=status.HTTP_400_BAD_REQUEST)

            prefs["two_step_pin_hash"] = self._hash_pin(new_pin, request.user.id)
            prefs["two_step_last_changed_at"] = timezone.now().isoformat()
            update_section_preferences(request.user.id, "security", prefs)
            return Response({"detail": "Two-step verification PIN changed."})

        return Response({"detail": "Invalid action."}, status=status.HTTP_400_BAD_REQUEST)


# ----------------------------------------
# Security Notifications
# ----------------------------------------

class SecurityNotificationsView(APIView):
    """
    GET  /api/security/notifications/  — Get security notification preferences
    PATCH /api/security/notifications/ — Update preferences
    """
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        prefs = get_section_preferences(request.user.id, "security")
        return Response({
            "new_login_alert": prefs.get("new_login_alert", True),
            "password_change_alert": prefs.get("password_change_alert", True),
            "two_step_change_alert": prefs.get("two_step_change_alert", True),
            "suspicious_activity_alert": prefs.get("suspicious_activity_alert", True),
            "alert_email": prefs.get("alert_email", ""),
            "alert_push": prefs.get("alert_push", True),
        })

    def patch(self, request):
        prefs = get_section_preferences(request.user.id, "security")
        bool_fields = ("new_login_alert", "password_change_alert", "two_step_change_alert",
                       "suspicious_activity_alert", "alert_push")
        for field in bool_fields:
            if field in request.data:
                prefs[field] = bool(request.data[field])

        if "alert_email" in request.data:
            prefs["alert_email"] = (request.data.get("alert_email") or "")[:254]

        update_section_preferences(request.user.id, "security", prefs)
        return Response({"detail": "Security notification settings updated.", **{
            k: prefs.get(k) for k in (*bool_fields, "alert_email")
        }})

    put = patch


# ----------------------------------------
# Unknown Caller Protection
# ----------------------------------------

class UnknownCallerProtectionView(APIView):
    """
    GET  /api/security/unknown-caller/  — Get setting
    POST /api/security/unknown-caller/  — Toggle
    """
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        prefs = get_section_preferences(request.user.id, "security")
        return Response({
            "silence_unknown_callers": prefs.get("silence_unknown_callers", False),
            "block_unknown_callers": prefs.get("block_unknown_callers", False),
        })

    def post(self, request):
        prefs = get_section_preferences(request.user.id, "security")
        if "silence_unknown_callers" in request.data:
            prefs["silence_unknown_callers"] = bool(request.data["silence_unknown_callers"])
        if "block_unknown_callers" in request.data:
            prefs["block_unknown_callers"] = bool(request.data["block_unknown_callers"])
        update_section_preferences(request.user.id, "security", prefs)
        return Response({
            "detail": "Unknown caller settings updated.",
            "silence_unknown_callers": prefs.get("silence_unknown_callers", False),
            "block_unknown_callers": prefs.get("block_unknown_callers", False),
        })


# ============================================================
# CALL SETTINGS SECTION (Notifications & Low Data Mode)
# ============================================================

class CallSettingsView(APIView):
    """
    Call Settings flow:
    - Call Settings: Everyone / My Contacts / Nobody (Who can call me)
    - Video Call Settings: Video Quality (Auto, 1080p, 720p, 480p, 360p), Low Data Mode, Mirror My Video
    - Call Management: Call Waiting, Silence Unknown Callers
    - Plus legacy call notification settings (ringtone, alerts, vibration).

    GET   /api/settings/calls/  — Full settings flow
    PATCH /api/settings/calls/  — Update settings
    PUT   /api/settings/calls/  — Update settings
    """
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        from .user_preferences import get_call_settings_flow
        return Response(get_call_settings_flow(request.user))

    def patch(self, request):
        from .user_preferences import update_call_settings_flow
        updated = update_call_settings_flow(request.user, request.data)
        return Response({
            "detail": "Call settings updated successfully.",
            **updated,
        })

    put = patch


CallNotificationsView = CallSettingsView


class VideoCallSettingsView(APIView):
    """
    Video Call Settings:
    - Video Quality: Auto, 1080p, 720p, 480p, 360p
    - Low Data Mode: true / false
    - Mirror My Video: true / false

    GET   /api/settings/calls/video/
    PATCH /api/settings/calls/video/
    """
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        from .user_preferences import get_call_settings_flow
        flow = get_call_settings_flow(request.user)
        return Response(flow["video_call_settings"])

    def patch(self, request):
        from .user_preferences import update_call_settings_flow
        updated = update_call_settings_flow(request.user, {
            "video_call_settings": request.data,
            **request.data,
        })
        return Response({
            "detail": "Video call settings updated successfully.",
            **updated["video_call_settings"],
        })

    put = patch


class CallManagementView(APIView):
    """
    Call Management:
    - Call Waiting: true / false
    - Silence Unknown Callers: true / false

    GET   /api/settings/calls/management/
    PATCH /api/settings/calls/management/
    """
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        from .user_preferences import get_call_settings_flow
        flow = get_call_settings_flow(request.user)
        return Response(flow["call_management"])

    def patch(self, request):
        from .user_preferences import update_call_settings_flow
        updated = update_call_settings_flow(request.user, {
            "call_management": request.data,
            **request.data,
        })
        return Response({
            "detail": "Call management settings updated successfully.",
            **updated["call_management"],
        })

    put = patch


# ============================================================
# GROUP SETTINGS FLOW & GROUP PRIVACY SECTION
# ============================================================

class GroupSettingsFlowView(APIView):
    """
    Group settings backend flow:
    - Who can add me to groups: Everyone / My Contacts / Nobody
    - Group Privacy:
      - Group Invitations
      - Member Approval
      - Admin Controls
      - Group Info Visibility
      - Recent Groups

    GET   /api/settings/groups/ or /api/settings/groups/flow/
    PATCH /api/settings/groups/ or /api/settings/groups/flow/
    """
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        from .user_preferences import get_group_settings_flow
        return Response(get_group_settings_flow(request.user, request))

    def patch(self, request):
        from .user_preferences import update_group_settings_flow
        updated = update_group_settings_flow(request.user, request.data, request)
        return Response({
            "detail": "Group settings updated successfully.",
            **updated,
        })

    put = patch


class WhoCanAddMeToGroupsView(APIView):
    """
    Who can add me to groups: Everyone / My Contacts / Nobody

    GET   /api/settings/groups/who-can-add/
    PATCH /api/settings/groups/who-can-add/
    """
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        from .user_preferences import get_group_settings_flow
        flow = get_group_settings_flow(request.user, request)
        return Response(flow["who_can_add_me_to_groups"])

    def patch(self, request):
        from .user_preferences import update_group_settings_flow
        val = request.data.get("value") or request.data.get("who_can_add_to_groups") or request.data.get("who_can_add_me_to_groups") or request.data.get("groups")
        updated = update_group_settings_flow(request.user, {
            "who_can_add_to_groups": val,
            **request.data,
        }, request)
        return Response({
            "detail": "Group addition privacy updated successfully.",
            **updated["who_can_add_me_to_groups"],
        })

    put = patch


class GroupPrivacyView(APIView):
    """
    Group Privacy section:
    - Group Invitations
    - Member Approval
    - Admin Controls
    - Group Info Visibility
    - Recent Groups

    GET   /api/settings/groups/privacy/
    PATCH /api/settings/groups/privacy/
    """
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        from .user_preferences import get_group_settings_flow
        flow = get_group_settings_flow(request.user, request)
        return Response(flow["group_privacy"])

    def patch(self, request):
        from .user_preferences import update_group_settings_flow
        updated = update_group_settings_flow(request.user, {
            "group_privacy": request.data,
            **request.data,
        }, request)
        return Response({
            "detail": "Group privacy settings updated successfully.",
            **updated["group_privacy"],
        })

    put = patch


class GroupInvitationsPreferenceView(APIView):
    """
    Group Invitations: allow / review / block, allow_invite_links

    GET   /api/settings/groups/invitations/
    PATCH /api/settings/groups/invitations/
    """
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        from .user_preferences import get_group_settings_flow
        flow = get_group_settings_flow(request.user, request)
        return Response(flow["group_privacy"]["group_invitations"])

    def patch(self, request):
        from .user_preferences import update_group_settings_flow
        val = request.data.get("value") or request.data.get("group_invitations")
        updated = update_group_settings_flow(request.user, {
            "group_invitations": val,
            "allow_invite_links": request.data.get("allow_invite_links"),
            **request.data,
        }, request)
        return Response({
            "detail": "Group invitations settings updated.",
            **updated["group_privacy"]["group_invitations"],
        })

    put = patch


class GroupMemberApprovalPreferenceView(APIView):
    """
    Member Approval: require_approval_by_default

    GET   /api/settings/groups/member-approval/
    PATCH /api/settings/groups/member-approval/
    """
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        from .user_preferences import get_group_settings_flow
        flow = get_group_settings_flow(request.user, request)
        return Response(flow["group_privacy"]["member_approval"])

    def patch(self, request):
        from .user_preferences import update_group_settings_flow
        val = request.data.get("require_approval_by_default") or request.data.get("member_approval_by_default")
        updated = update_group_settings_flow(request.user, {
            "member_approval_by_default": val,
            **request.data,
        }, request)
        return Response({
            "detail": "Member approval settings updated.",
            **updated["group_privacy"]["member_approval"],
        })

    put = patch


class GroupAdminControlsPreferenceView(APIView):
    """
    Admin Controls: default_preset (admins / all_members)

    GET   /api/settings/groups/admin-controls/
    PATCH /api/settings/groups/admin-controls/
    """
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        from .user_preferences import get_group_settings_flow
        flow = get_group_settings_flow(request.user, request)
        return Response(flow["group_privacy"]["admin_controls"])

    def patch(self, request):
        from .user_preferences import update_group_settings_flow
        val = request.data.get("default_preset") or request.data.get("admin_controls_preset")
        updated = update_group_settings_flow(request.user, {
            "admin_controls_preset": val,
            **request.data,
        }, request)
        return Response({
            "detail": "Admin controls preset updated.",
            **updated["group_privacy"]["admin_controls"],
        })

    put = patch


class GroupInfoVisibilityView(APIView):
    """
    Group Info Visibility: everyone / contacts / nobody, show_groups_on_profile

    GET   /api/settings/groups/visibility/
    PATCH /api/settings/groups/visibility/
    """
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        from .user_preferences import get_group_settings_flow
        flow = get_group_settings_flow(request.user, request)
        return Response(flow["group_privacy"]["group_info_visibility"])

    def patch(self, request):
        from .user_preferences import update_group_settings_flow
        val = request.data.get("value") or request.data.get("group_info_visibility")
        updated = update_group_settings_flow(request.user, {
            "group_info_visibility": val,
            "show_groups_on_profile": request.data.get("show_groups_on_profile"),
            **request.data,
        }, request)
        return Response({
            "detail": "Group info visibility updated.",
            **updated["group_privacy"]["group_info_visibility"],
        })

    put = patch


class RecentGroupsView(APIView):
    """
    Recent Groups: list active/recent groups for the authenticated user

    GET /api/settings/groups/recent/
    """
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        from .user_preferences import get_user_recent_groups
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


# ============================================================
# DISCOVERY SECTION (Nearby Places, Businesses, Events, Offers)
# ============================================================

# NOTE: Real implementations would integrate an external Places/Events API.
# These endpoints return structured responses that mirror what a production
# integration would return, so the client code is stable regardless.

class NearbyPlacesView(APIView):
    """
    GET /api/discovery/nearby/
    Discover nearby places. Query params: lat, lng, radius_km, category.
    """
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        lat = request.query_params.get("lat")
        lng = request.query_params.get("lng")
        radius_km = request.query_params.get("radius_km", "5")
        category = request.query_params.get("category", "all")

        if not lat or not lng:
            return Response(
                {"detail": "lat and lng query parameters are required."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        try:
            lat_f = float(lat)
            lng_f = float(lng)
            radius_f = min(float(radius_km), 50.0)
        except ValueError:
            return Response({"detail": "lat, lng, and radius_km must be numbers."}, status=status.HTTP_400_BAD_REQUEST)

        # Return a structured placeholder — in production connect to Google Places / HERE API
        return Response({
            "location": {"lat": lat_f, "lng": lng_f},
            "radius_km": radius_f,
            "category": category,
            "places": [],
            "total": 0,
            "note": "Connect your Places API key via PLACES_API_KEY setting to enable live results.",
        })


class NearbyBusinessesView(APIView):
    """
    GET /api/discovery/businesses/
    Discover nearby Exi-registered businesses.
    """
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        query = (request.query_params.get("q") or "").strip()
        businesses_qs = User.objects.filter(
            is_business=True, is_active=True
        ).exclude(business_name="")

        if query:
            from django.db.models import Q
            businesses_qs = businesses_qs.filter(
                Q(business_name__icontains=query)
                | Q(display_name__icontains=query)
                | Q(bio__icontains=query)
            )

        businesses = []
        for biz in businesses_qs[:50]:
            businesses.append({
                "user_id": biz.id,
                "exi_id": biz.exi_id,
                "business_name": biz.business_name,
                "display_name": biz.display_name or biz.business_name,
                "bio": biz.bio,
                "business_address": biz.business_address,
                "business_website": biz.business_website,
                "profile_photo": request.build_absolute_uri(biz.profile_photo.url) if biz.profile_photo else None,
            })

        return Response({
            "query": query,
            "count": len(businesses),
            "businesses": businesses,
        })


class DiscoveryEventsView(APIView):
    """
    GET /api/discovery/events/
    List upcoming events. Stored in user_preferences keyed per-user (demo).
    In production, events would be a separate DB table.
    """
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        # In production: query Events model
        # For now: return structured empty response
        category = request.query_params.get("category", "all")
        return Response({
            "category": category,
            "events": [],
            "total": 0,
            "note": "Events feature — integrate with events DB table for live results.",
        })


class DiscoveryOffersView(APIView):
    """
    GET /api/discovery/offers/
    List active offers/promotions from businesses.
    """
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        # In production: query Offer model
        return Response({
            "offers": [],
            "total": 0,
            "note": "Offers feature — integrate with offers DB table for live results.",
        })


class AIRecommendationsView(APIView):
    """
    GET /api/discovery/ai-recommendations/
    AI-powered recommendations based on user activity.
    """
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        # In production: call ML recommendation service
        return Response({
            "recommendations": {
                "suggested_contacts": [],
                "trending_businesses": [],
                "nearby_events": [],
                "curated_offers": [],
            },
            "generated_at": timezone.now().isoformat(),
            "note": "AI Recommendations — connect to recommendation engine for live results.",
        })


# ============================================================
# BUSINESS SECTION
# ============================================================

class BusinessProfileView(APIView):
    """
    GET  /api/business/profile/  — Get business profile
    PATCH /api/business/profile/ — Update business profile
    POST /api/business/setup/    — Set up as business account
    """
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        user = request.user
        prefs = get_section_preferences(user.id, "business")
        return Response({
            "is_business": user.is_business,
            "business_name": user.business_name,
            "business_address": user.business_address,
            "business_website": user.business_website,
            "bio": user.bio,
            "display_name": user.display_name,
            "profile_photo": request.build_absolute_uri(user.profile_photo.url) if user.profile_photo else None,
            "exi_id": user.exi_id,
            "categories": prefs.get("categories", []),
            "working_hours": prefs.get("working_hours", {}),
            "social_links": prefs.get("social_links", {}),
            "verified": prefs.get("verified", False),
            "rating": prefs.get("rating", None),
            "review_count": prefs.get("review_count", 0),
        })

    def patch(self, request):
        user = request.user
        update_fields = []

        if "business_name" in request.data:
            user.business_name = (request.data["business_name"] or "")[:100]
            update_fields.append("business_name")
        if "business_address" in request.data:
            user.business_address = (request.data["business_address"] or "")[:255]
            update_fields.append("business_address")
        if "business_website" in request.data:
            user.business_website = (request.data["business_website"] or "")[:200]
            update_fields.append("business_website")
        if "bio" in request.data:
            user.bio = (request.data["bio"] or "")[:160]
            update_fields.append("bio")
        if "display_name" in request.data:
            user.display_name = (request.data["display_name"] or "")[:50]
            update_fields.append("display_name")

        if update_fields:
            user.save(update_fields=update_fields)

        # Extended prefs
        prefs = get_section_preferences(user.id, "business")
        if "categories" in request.data:
            prefs["categories"] = [str(c)[:50] for c in (request.data["categories"] or [])][:10]
        if "working_hours" in request.data and isinstance(request.data["working_hours"], dict):
            prefs["working_hours"] = request.data["working_hours"]
        if "social_links" in request.data and isinstance(request.data["social_links"], dict):
            prefs["social_links"] = request.data["social_links"]
        update_section_preferences(user.id, "business", prefs)

        return Response({"detail": "Business profile updated."})

    put = patch


class BusinessSetupView(APIView):
    """
    POST /api/business/setup/
    Convert a personal account to a business account.
    """
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request):
        user = request.user
        business_name = (request.data.get("business_name") or "").strip()
        if not business_name:
            return Response({"detail": "business_name is required."}, status=status.HTTP_400_BAD_REQUEST)

        user.is_business = True
        user.business_name = business_name[:100]
        if request.data.get("business_address"):
            user.business_address = str(request.data["business_address"])[:255]
        if request.data.get("business_website"):
            user.business_website = str(request.data["business_website"])[:200]
        user.save(update_fields=["is_business", "business_name", "business_address", "business_website"])

        return Response({
            "detail": "Business account set up successfully.",
            "is_business": True,
            "business_name": user.business_name,
        })


class BusinessProductsView(APIView):
    """
    GET  /api/business/products/  — List products/services
    POST /api/business/products/  — Add product/service
    """
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        prefs = get_section_preferences(request.user.id, "business")
        return Response({
            "products": prefs.get("products", []),
            "total": len(prefs.get("products", [])),
        })

    def post(self, request):
        name = (request.data.get("name") or "").strip()
        if not name:
            return Response({"detail": "Product/service name is required."}, status=status.HTTP_400_BAD_REQUEST)

        prefs = get_section_preferences(request.user.id, "business")
        products = prefs.get("products", [])

        import uuid
        product = {
            "id": str(uuid.uuid4()),
            "name": name[:100],
            "description": (request.data.get("description") or "")[:500],
            "price": str(request.data.get("price") or "")[:50],
            "currency": (request.data.get("currency") or "INR")[:10],
            "image_url": (request.data.get("image_url") or "")[:512],
            "is_available": bool(request.data.get("is_available", True)),
            "created_at": timezone.now().isoformat(),
        }
        products.append(product)
        prefs["products"] = products
        update_section_preferences(request.user.id, "business", prefs)
        return Response(product, status=status.HTTP_201_CREATED)


class BusinessProductDetailView(APIView):
    """
    GET    /api/business/products/<product_id>/  — Get product
    PATCH  /api/business/products/<product_id>/  — Update product
    DELETE /api/business/products/<product_id>/  — Delete product
    """
    permission_classes = [permissions.IsAuthenticated]

    def _find(self, products, product_id):
        for i, p in enumerate(products):
            if p.get("id") == product_id:
                return i, p
        return None, None

    def get(self, request, product_id):
        prefs = get_section_preferences(request.user.id, "business")
        _, product = self._find(prefs.get("products", []), product_id)
        if product is None:
            return Response({"detail": "Product not found."}, status=status.HTTP_404_NOT_FOUND)
        return Response(product)

    def patch(self, request, product_id):
        prefs = get_section_preferences(request.user.id, "business")
        products = prefs.get("products", [])
        idx, product = self._find(products, product_id)
        if product is None:
            return Response({"detail": "Product not found."}, status=status.HTTP_404_NOT_FOUND)

        for field in ("name", "description", "price", "currency", "image_url"):
            if field in request.data:
                product[field] = str(request.data[field] or "")[:{"name": 100, "description": 500, "price": 50, "currency": 10, "image_url": 512}.get(field, 255)]
        if "is_available" in request.data:
            product["is_available"] = bool(request.data["is_available"])

        products[idx] = product
        prefs["products"] = products
        update_section_preferences(request.user.id, "business", prefs)
        return Response(product)

    def delete(self, request, product_id):
        prefs = get_section_preferences(request.user.id, "business")
        products = prefs.get("products", [])
        idx, _ = self._find(products, product_id)
        if idx is None:
            return Response({"detail": "Product not found."}, status=status.HTTP_404_NOT_FOUND)
        products.pop(idx)
        prefs["products"] = products
        update_section_preferences(request.user.id, "business", prefs)
        return Response(status=status.HTTP_204_NO_CONTENT)


class BusinessOffersView(APIView):
    """
    GET  /api/business/offers/  — List offers
    POST /api/business/offers/  — Create offer
    """
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        prefs = get_section_preferences(request.user.id, "business")
        offers = prefs.get("offers", [])
        # Filter expired
        now = timezone.now().isoformat()
        active = [o for o in offers if not o.get("expires_at") or o["expires_at"] > now]
        return Response({"offers": active, "total": len(active)})

    def post(self, request):
        title = (request.data.get("title") or "").strip()
        if not title:
            return Response({"detail": "Offer title is required."}, status=status.HTTP_400_BAD_REQUEST)

        prefs = get_section_preferences(request.user.id, "business")
        offers = prefs.get("offers", [])

        import uuid
        offer = {
            "id": str(uuid.uuid4()),
            "title": title[:100],
            "description": (request.data.get("description") or "")[:500],
            "discount_percent": request.data.get("discount_percent"),
            "discount_amount": request.data.get("discount_amount"),
            "promo_code": (request.data.get("promo_code") or "")[:30],
            "valid_from": (request.data.get("valid_from") or timezone.now().isoformat()),
            "expires_at": request.data.get("expires_at"),
            "created_at": timezone.now().isoformat(),
        }
        offers.append(offer)
        prefs["offers"] = offers
        update_section_preferences(request.user.id, "business", prefs)
        return Response(offer, status=status.HTTP_201_CREATED)


class BusinessCustomerChatView(APIView):
    """
    GET /api/business/customer-chat/
    Get customer chat settings — e.g., greeting message, away message,
    business hours, auto-reply.
    PATCH /api/business/customer-chat/ — Update settings
    """
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        prefs = get_section_preferences(request.user.id, "business")
        return Response({
            "greeting_message": prefs.get("greeting_message", ""),
            "away_message": prefs.get("away_message", ""),
            "auto_reply_enabled": prefs.get("auto_reply_enabled", False),
            "auto_reply_message": prefs.get("auto_reply_message", ""),
            "response_time_label": prefs.get("response_time_label", "Usually responds within a few hours"),
            "allow_messages_from_non_contacts": prefs.get("allow_messages_from_non_contacts", True),
        })

    def patch(self, request):
        prefs = get_section_preferences(request.user.id, "business")
        text_fields = {
            "greeting_message": 500,
            "away_message": 500,
            "auto_reply_message": 500,
            "response_time_label": 100,
        }
        for field, maxlen in text_fields.items():
            if field in request.data:
                prefs[field] = (request.data[field] or "")[:maxlen]
        if "auto_reply_enabled" in request.data:
            prefs["auto_reply_enabled"] = bool(request.data["auto_reply_enabled"])
        if "allow_messages_from_non_contacts" in request.data:
            prefs["allow_messages_from_non_contacts"] = bool(request.data["allow_messages_from_non_contacts"])

        update_section_preferences(request.user.id, "business", prefs)
        return Response({"detail": "Customer chat settings updated."})

    put = patch


# ============================================================
# HELP SECTION
# ============================================================

HELP_ARTICLES = [
    {"id": "getting-started", "title": "Getting Started with EXI", "category": "basics",
     "summary": "Learn how to set up your profile, find contacts, and start chatting."},
    {"id": "privacy-controls", "title": "Privacy Controls", "category": "privacy",
     "summary": "Control who can see your last seen, profile photo, and online status."},
    {"id": "two-step-verification", "title": "Two-Step Verification", "category": "security",
     "summary": "Add an extra layer of security to your account with a PIN."},
    {"id": "group-chats", "title": "Creating and Managing Group Chats", "category": "chats",
     "summary": "Learn how to create groups, add members, and manage admin controls."},
    {"id": "calls", "title": "Voice & Video Calls", "category": "calls",
     "summary": "Make high-quality voice and video calls with your contacts."},
    {"id": "media-storage", "title": "Managing Media & Storage", "category": "storage",
     "summary": "Clear old media, manage auto-downloads, and free up storage."},
    {"id": "business-profile", "title": "Setting Up a Business Profile", "category": "business",
     "summary": "Switch to a business account and manage your products and offers."},
    {"id": "blocked-contacts", "title": "Blocking Contacts", "category": "privacy",
     "summary": "Block unwanted contacts and manage your blocked list."},
    {"id": "backup-restore", "title": "Chat Backup & Restore", "category": "chats",
     "summary": "Back up your chats and restore them on a new device."},
    {"id": "disappearing-messages", "title": "Disappearing Messages", "category": "chats",
     "summary": "Set messages to automatically disappear after a set time."},
]

FAQ_ITEMS = [
    {"question": "How do I change my phone number?",
     "answer": "Go to Account > Phone Number > Request Change and follow the verification steps."},
    {"question": "Can I use EXI on multiple devices?",
     "answer": "Yes! You can link up to 5 devices under Account > Linked Devices."},
    {"question": "How do I delete my account?",
     "answer": "Go to Settings > Security > Delete Account. This action is permanent."},
    {"question": "Why are my messages not delivering?",
     "answer": "Check your internet connection. The recipient may have blocked you or turned off notifications."},
    {"question": "How do I report spam?",
     "answer": "Open the chat, tap the three-dot menu, and select 'Report'. You can also block the sender."},
]

TERMS_TEXT = """TERMS OF SERVICE

Last updated: October 2026

1. ACCEPTANCE — By using EXI, you agree to these Terms.
2. ELIGIBILITY — You must be 13 years or older to use EXI.
3. PRIVACY — We respect your privacy. See our Privacy Policy.
4. PROHIBITED USE — Do not use EXI for illegal, harmful, or abusive activities.
5. CONTENT — You own your content. We do not sell it.
6. TERMINATION — We may suspend accounts that violate these Terms.
7. LIMITATION OF LIABILITY — EXI is provided \"as is\" without warranties.
8. GOVERNING LAW — These Terms are governed by applicable law.

For the full Terms of Service, visit our website.
"""

PRIVACY_POLICY_TEXT = """PRIVACY POLICY

Last updated: October 2026

WHAT WE COLLECT
- Account info (email/phone, name, profile photo)
- Device identifiers for push notifications
- Messages (end-to-end encrypted, we cannot read them)
- Usage data (anonymised analytics)

HOW WE USE IT
- To provide and improve the EXI service
- To send service notifications
- For security and fraud prevention

WHAT WE DON'T DO
- We do not sell your personal data
- We do not read your messages
- We do not share data with advertisers

YOUR RIGHTS
- Access, correct, or delete your data at any time
- Export your chat history under Settings > Chats
- Delete your account under Settings > Security

For the full Privacy Policy, visit our website.
"""


class HelpCenterView(APIView):
    """
    GET /api/help/  — Help Center home (articles + FAQ)
    """
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        query = (request.query_params.get("q") or "").strip().lower()
        category = (request.query_params.get("category") or "").strip().lower()

        articles = HELP_ARTICLES
        if query:
            articles = [a for a in articles if query in a["title"].lower() or query in a["summary"].lower()]
        if category:
            articles = [a for a in articles if a["category"] == category]

        faq = FAQ_ITEMS
        if query:
            faq = [f for f in faq if query in f["question"].lower() or query in f["answer"].lower()]

        categories = list({a["category"] for a in HELP_ARTICLES})

        return Response({
            "categories": sorted(categories),
            "articles": articles,
            "faq": faq if not category else [],
            "total_articles": len(articles),
        })


class HelpArticleView(APIView):
    """
    GET /api/help/articles/<article_id>/  — Get full article
    """
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request, article_id):
        article = next((a for a in HELP_ARTICLES if a["id"] == article_id), None)
        if not article:
            return Response({"detail": "Article not found."}, status=status.HTTP_404_NOT_FOUND)
        # In production load full content from DB / CMS
        return Response({**article, "content": article["summary"] + "\n\nFull content coming soon."})


class ReportProblemView(APIView):
    """
    POST /api/help/report-problem/
    Submit a problem report.
    """
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request):
        category = (request.data.get("category") or "other").strip()
        description = (request.data.get("description") or "").strip()
        if not description:
            return Response({"detail": "description is required."}, status=status.HTTP_400_BAD_REQUEST)

        # Store in cache for demo; production would write to DB or Zendesk
        import uuid
        ticket_id = f"EXI-{str(uuid.uuid4())[:8].upper()}"
        cache.set(f"problem_report:{ticket_id}", {
            "ticket_id": ticket_id,
            "user_id": request.user.id,
            "category": category,
            "description": description[:2000],
            "submitted_at": timezone.now().isoformat(),
            "status": "submitted",
        }, timeout=86400 * 30)

        return Response({
            "detail": "Problem report submitted successfully. Our team will review it.",
            "ticket_id": ticket_id,
            "status": "submitted",
        }, status=status.HTTP_201_CREATED)


class ContactSupportView(APIView):
    """
    GET  /api/help/contact/  — Get support contact options
    POST /api/help/contact/  — Send a support message
    """
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        return Response({
            "support_options": [
                {"type": "chat", "label": "Chat with Support", "available": True,
                 "description": "Get help from our support team in real time."},
                {"type": "email", "label": "Email Support", "available": True,
                 "email": "support@exi.app",
                 "description": "Send us an email and we'll respond within 24 hours."},
                {"type": "faq", "label": "Browse FAQ", "available": True,
                 "url": "/api/help/", "description": "Find answers to common questions."},
            ],
            "business_hours": "Monday–Friday, 9 AM–6 PM IST",
            "average_response_time": "Within 24 hours",
        })

    def post(self, request):
        subject = (request.data.get("subject") or "").strip()
        message = (request.data.get("message") or "").strip()
        if not message:
            return Response({"detail": "message is required."}, status=status.HTTP_400_BAD_REQUEST)

        import uuid
        ticket_id = f"EXI-{str(uuid.uuid4())[:8].upper()}"
        cache.set(f"support_ticket:{ticket_id}", {
            "ticket_id": ticket_id,
            "user_id": request.user.id,
            "subject": subject[:200],
            "message": message[:2000],
            "submitted_at": timezone.now().isoformat(),
            "status": "open",
        }, timeout=86400 * 30)

        return Response({
            "detail": "Support message sent. We'll get back to you within 24 hours.",
            "ticket_id": ticket_id,
            "status": "open",
        }, status=status.HTTP_201_CREATED)


class TermsView(APIView):
    """
    GET /api/help/terms/  — Get Terms of Service
    """
    permission_classes = [permissions.AllowAny]

    def get(self, request):
        return Response({
            "title": "Terms of Service",
            "last_updated": "2026-10-01",
            "version": "1.0",
            "content": TERMS_TEXT,
            "url": "https://exi.app/terms",
        })


class PrivacyPolicyView(APIView):
    """
    GET /api/help/privacy/  — Get Privacy Policy
    """
    permission_classes = [permissions.AllowAny]

    def get(self, request):
        return Response({
            "title": "Privacy Policy",
            "last_updated": "2026-10-01",
            "version": "1.0",
            "content": PRIVACY_POLICY_TEXT,
            "url": "https://exi.app/privacy",
        })


def _get_direct_conversation_id(user_a, user_b):
    """Return direct conversation UUID if a chat already exists between two users."""
    from chats.models import ConversationParticipant

    return (
        ConversationParticipant.objects.filter(
            conversation__type="direct",
            user=user_a,
            conversation__participants=user_b,
        )
        .values_list("conversation_id", flat=True)
        .first()
    )


def _find_user_by_phone(phone_input):
    """
    Search for an active User matching the phone number.
    Handles international prefixes, whitespace, and 10-digit national formats.
    """
    if not phone_input:
        return None

    clean = normalize_phone(phone_input)
    digits = re.sub(r"\D", "", phone_input)

    # 1. Exact match with normalized phone
    user = User.objects.filter(phone=clean, is_active=True).first()
    if user:
        return user

    # 2. Match without '+' if stored without prefix
    if clean.startswith("+"):
        user = User.objects.filter(phone=clean[1:], is_active=True).first()
        if user:
            return user

    # 3. Match last 10 digits for domestic phone numbers
    if len(digits) >= 10:
        last10 = digits[-10:]
        user = User.objects.filter(phone__endswith=last10, is_active=True).first()
        if user:
            return user

    return None


def _find_user_by_exi_id(exi_id_input):
    """
    Search for an active User by EXI ID, handle, or username.
    Handles leading '@' or trailing '.exi' (e.g. '@habhi.exi' -> 'habhi').
    """
    if not exi_id_input:
        return None

    raw = exi_id_input.strip()
    clean = raw.lstrip("@").strip()
    if clean.endswith(".exi"):
        clean = clean[:-4]

    # 1. Exact match by exi_id (e.g. 'EXI-123456')
    user = User.objects.filter(models.Q(exi_id__iexact=raw) | models.Q(exi_id__iexact=clean), is_active=True).first()
    if user:
        return user

    # 2. Match by username (e.g. 'habhi')
    user = User.objects.filter(models.Q(username__iexact=clean) | models.Q(username__iexact=raw), is_active=True).first()
    if user:
        return user

    return None


class ContactsView(APIView):
    """
    Manage user's address book / contacts:
    - GET  /api/contacts/ — List All Contacts
    - POST /api/contacts/ — Save New Contact (First name, Last name, Phone number)
    """

    permission_classes = [permissions.IsAuthenticated]

    def _get_contacts(self, user_id):
        prefs = get_section_preferences(user_id, "contacts")
        return prefs.get("contact_list", [])

    def _save_contacts(self, user_id, contact_list):
        update_section_preferences(user_id, "contacts", {"contact_list": contact_list})

    def get(self, request):
        contacts = self._get_contacts(request.user.id)
        enriched = []

        for c in contacts:
            phone = c.get("phone_number", "")
            matched_user = _find_user_by_phone(phone)

            item = {
                "id": c.get("id"),
                "first_name": c.get("first_name", ""),
                "last_name": c.get("last_name", ""),
                "name": f"{c.get('first_name', '')} {c.get('last_name', '')}".strip(),
                "phone_number": phone,
                "is_registered": bool(matched_user),
                "created_at": c.get("created_at"),
            }

            if matched_user:
                serializer = PublicProfileSerializer(matched_user, context={"request": request})
                item["user"] = serializer.data
                conv_id = _get_direct_conversation_id(request.user, matched_user)
                item["conversation_id"] = str(conv_id) if conv_id else None
            else:
                item["user"] = None
                item["conversation_id"] = None

            enriched.append(item)

        # Sort alphabetically by name
        enriched.sort(key=lambda x: (x["first_name"].lower(), x["last_name"].lower()))

        return Response({
            "count": len(enriched),
            "contacts": enriched,
        })

    def post(self, request):
        first_name = (request.data.get("first_name") or "").strip()
        last_name = (request.data.get("last_name") or "").strip()
        phone_number = (request.data.get("phone_number") or "").strip()

        if not first_name:
            return Response(
                {"detail": "First name is required."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        if not phone_number:
            return Response(
                {"detail": "Phone number is required."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        norm_phone = normalize_phone(phone_number)
        contacts = self._get_contacts(request.user.id)

        # Check if contact with same phone already exists; if so, update name
        existing = next((c for c in contacts if normalize_phone(c.get("phone_number", "")) == norm_phone), None)
        now_iso = timezone.now().isoformat()

        if existing:
            existing["first_name"] = first_name
            existing["last_name"] = last_name
            existing["phone_number"] = phone_number
            existing["updated_at"] = now_iso
            contact_obj = existing
        else:
            contact_obj = {
                "id": str(uuid.uuid4()),
                "first_name": first_name,
                "last_name": last_name,
                "phone_number": phone_number,
                "created_at": now_iso,
                "updated_at": now_iso,
            }
            contacts.append(contact_obj)

        self._save_contacts(request.user.id, contacts)

        # Check live registered user
        matched_user = _find_user_by_phone(phone_number)
        user_data = None
        conv_id = None
        if matched_user:
            user_data = PublicProfileSerializer(matched_user, context={"request": request}).data
            cid = _get_direct_conversation_id(request.user, matched_user)
            conv_id = str(cid) if cid else None

        response_data = {
            "id": contact_obj["id"],
            "first_name": first_name,
            "last_name": last_name,
            "name": f"{first_name} {last_name}".strip(),
            "phone_number": phone_number,
            "is_registered": bool(matched_user),
            "user": user_data,
            "conversation_id": conv_id,
            "created_at": contact_obj["created_at"],
            "detail": "Contact saved successfully.",
        }

        return Response(response_data, status=status.HTTP_201_CREATED if not existing else status.HTTP_200_OK)


class ContactDetailView(APIView):
    """
    GET, PATCH, DELETE for a specific saved contact.
    """

    permission_classes = [permissions.IsAuthenticated]

    def _get_contacts(self, user_id):
        return get_section_preferences(user_id, "contacts").get("contact_list", [])

    def _save_contacts(self, user_id, contact_list):
        update_section_preferences(user_id, "contacts", {"contact_list": contact_list})

    def get(self, request, contact_id):
        contacts = self._get_contacts(request.user.id)
        c = next((item for item in contacts if item.get("id") == contact_id), None)
        if not c:
            return Response({"detail": "Contact not found."}, status=status.HTTP_404_NOT_FOUND)

        matched_user = _find_user_by_phone(c.get("phone_number", ""))
        user_data = PublicProfileSerializer(matched_user, context={"request": request}).data if matched_user else None
        cid = _get_direct_conversation_id(request.user, matched_user) if matched_user else None

        return Response({
            "id": c["id"],
            "first_name": c.get("first_name", ""),
            "last_name": c.get("last_name", ""),
            "name": f"{c.get('first_name', '')} {c.get('last_name', '')}".strip(),
            "phone_number": c.get("phone_number", ""),
            "is_registered": bool(matched_user),
            "user": user_data,
            "conversation_id": str(cid) if cid else None,
            "created_at": c.get("created_at"),
        })

    def patch(self, request, contact_id):
        contacts = self._get_contacts(request.user.id)
        c = next((item for item in contacts if item.get("id") == contact_id), None)
        if not c:
            return Response({"detail": "Contact not found."}, status=status.HTTP_404_NOT_FOUND)

        if "first_name" in request.data:
            val = (request.data["first_name"] or "").strip()
            if not val:
                return Response({"detail": "First name cannot be empty."}, status=status.HTTP_400_BAD_REQUEST)
            c["first_name"] = val

        if "last_name" in request.data:
            c["last_name"] = (request.data["last_name"] or "").strip()

        if "phone_number" in request.data:
            phone_val = (request.data["phone_number"] or "").strip()
            if not phone_val:
                return Response({"detail": "Phone number cannot be empty."}, status=status.HTTP_400_BAD_REQUEST)
            c["phone_number"] = phone_val

        c["updated_at"] = timezone.now().isoformat()
        self._save_contacts(request.user.id, contacts)

        return Response({"detail": "Contact updated successfully.", "contact": c})

    def delete(self, request, contact_id):
        contacts = self._get_contacts(request.user.id)
        initial_len = len(contacts)
        contacts = [c for c in contacts if c.get("id") != contact_id]
        if len(contacts) == initial_len:
            return Response({"detail": "Contact not found."}, status=status.HTTP_404_NOT_FOUND)

        self._save_contacts(request.user.id, contacts)
        return Response(status=status.HTTP_204_NO_CONTENT)


class FindOnExiView(APIView):
    """
    Powers the 'Find on exi' screen:
    - Tab 1: Find by @ exi ID (?exi_id=...)
    - Tab 2: Find by Phone (?phone=...)
    """

    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        exi_id_query = request.query_params.get("exi_id") or request.query_params.get("q")
        phone_query = request.query_params.get("phone")

        if not exi_id_query and not phone_query:
            return Response(
                {"detail": "Please provide 'exi_id' or 'phone' query parameter."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        target_user = None
        search_mode = "exi_id" if exi_id_query else "phone"

        if exi_id_query:
            target_user = _find_user_by_exi_id(exi_id_query)
        elif phone_query:
            target_user = _find_user_by_phone(phone_query)

        if not target_user:
            return Response(
                {
                    "found": False,
                    "search_mode": search_mode,
                    "detail": f"No user found on EXI with this {'EXI ID' if search_mode == 'exi_id' else 'phone number'}.",
                },
                status=status.HTTP_404_NOT_FOUND,
            )

        # Check privacy permissions: who can find me by phone / ID
        if target_user.id != request.user.id:
            target_prefs = get_section_preferences(target_user.id, "privacy")
            if search_mode == "phone":
                by_phone_perm = target_prefs.get("find_by_phone", "everyone")
                if by_phone_perm == "nobody":
                    return Response(
                        {"found": False, "detail": "User not found."},
                        status=status.HTTP_404_NOT_FOUND,
                    )
                if by_phone_perm == "contacts" and not are_contacts(target_user, request.user):
                    return Response(
                        {"found": False, "detail": "User not found."},
                        status=status.HTTP_404_NOT_FOUND,
                    )
            else:
                by_id_perm = target_prefs.get("find_by_exi_id", target_prefs.get("find_by_id", "everyone"))
                if by_id_perm == "nobody":
                    return Response(
                        {"found": False, "detail": "User not found."},
                        status=status.HTTP_404_NOT_FOUND,
                    )
                if by_id_perm == "contacts" and not are_contacts(target_user, request.user):
                    return Response(
                        {"found": False, "detail": "User not found."},
                        status=status.HTTP_404_NOT_FOUND,
                    )

        serializer = PublicProfileSerializer(target_user, context={"request": request})
        conv_id = _get_direct_conversation_id(request.user, target_user)
        is_contact = are_contacts(request.user, target_user)

        return Response({
            "found": True,
            "search_mode": search_mode,
            "user": serializer.data,
            "conversation_id": str(conv_id) if conv_id else None,
            "is_contact": is_contact,
        })


class UserQRCodeView(APIView):
    """
    Powers 'Scan an exi code' and 'Show my code':
    - GET  /api/users/qr/       — Get my QR code data & invite link
    - POST /api/users/qr/scan/  — Scan / resolve someone's EXI code
    """

    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        user = request.user
        prefs = get_section_preferences(user.id, "privacy")
        invite_link = request.build_absolute_uri(f"/invite/{user.exi_id}")

        return Response({
            "exi_id": user.exi_id,
            "username": user.username,
            "handle": f"@{user.username}.exi" if user.username else f"@{user.exi_id}",
            "display_name": user.display_name or user.full_name or user.username,
            "qr_data": f"exi://user/{user.exi_id}",
            "invite_link": invite_link,
            "privacy": {
                "find_by_id": prefs.get("find_by_id", "everyone"),
                "find_by_phone": prefs.get("find_by_phone", "contacts"),
            },
        })

    def post(self, request):
        qr_data = (request.data.get("qr_data") or request.data.get("code") or "").strip()
        if not qr_data:
            return Response({"detail": "qr_data is required."}, status=status.HTTP_400_BAD_REQUEST)

        # Parse exi://user/<exi_id> or raw EXI ID
        exi_id = qr_data
        if "exi://user/" in qr_data:
            exi_id = qr_data.split("exi://user/")[-1]
        elif "/invite/" in qr_data:
            exi_id = qr_data.split("/invite/")[-1]

        target = _find_user_by_exi_id(exi_id)
        if not target:
            return Response({"detail": "Invalid or expired EXI code."}, status=status.HTTP_404_NOT_FOUND)

        serializer = PublicProfileSerializer(target, context={"request": request})
        conv_id = _get_direct_conversation_id(request.user, target)

        return Response({
            "resolved": True,
            "user": serializer.data,
            "conversation_id": str(conv_id) if conv_id else None,
            "is_contact": are_contacts(request.user, target),
        })


# ===========================================================================
# Live Notes (Status Notes: in movie, busy, custom note, etc.)
# ===========================================================================

class UserLiveNoteView(APIView):
    """
    Manage user's personal Live Note:
    - GET    /api/profile/note/ - View my current active live note + presets
    - POST   /api/profile/note/ - Set / update live note (preset or custom text)
    - PATCH  /api/profile/note/ - Update live note
    - DELETE /api/profile/note/ - Clear / delete live note
    """
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        from .live_notes import get_live_note, LIVE_NOTE_PRESETS
        note = get_live_note(request.user.id)
        return Response({
            "live_note": note,
            "has_active_note": note is not None,
            "presets": LIVE_NOTE_PRESETS,
        })

    def post(self, request):
        from .live_notes import set_live_note
        note, err = set_live_note(request.user, request.data)
        if err:
            return Response({"detail": err}, status=status.HTTP_400_BAD_REQUEST)
        return Response({
            "detail": "Live note set successfully.",
            "live_note": note,
        }, status=status.HTTP_201_CREATED)

    def patch(self, request):
        return self.post(request)

    def put(self, request):
        return self.post(request)

    def delete(self, request):
        from .live_notes import clear_live_note
        res = clear_live_note(request.user)
        return Response(res, status=status.HTTP_200_OK)


class LiveNotePresetsView(APIView):
    """
    GET /api/profile/note/presets/ - List all available status note presets
    """
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        from .live_notes import LIVE_NOTE_PRESETS
        return Response({
            "count": len(LIVE_NOTE_PRESETS),
            "presets": LIVE_NOTE_PRESETS,
        })


class LiveNotesFeedView(APIView):
    """
    Feed of active live notes from user's contacts & chat partners:
    - GET /api/users/notes/ (or /api/contacts/notes/)
    """
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        from .live_notes import get_live_note, get_contacts_live_notes_feed
        my_note = get_live_note(request.user.id)
        feed = get_contacts_live_notes_feed(request.user)
        return Response({
            "my_note": my_note,
            "count": len(feed),
            "feed": feed,
        })


# ============================================================
# STATUS (STORIES) WORKFLOW SECTION
# ============================================================

class StatusCreateView(APIView):
    """
    Status workflow:
    - User clicks Add Status
    - Selects photo, video or text
    - Adds optional caption
    - Selects visibility
    - Uploads media to storage
    - Backend creates status (expires automatically after 24 hours)
    - Followers/contacts can view it

    POST /api/status/ or /api/status/create/ — Create new status
    GET  /api/status/ or /api/status/feed/   — Get status feed
    """
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request):
        from .user_preferences import create_status
        data = request.data
        status_type = data.get("status_type") or data.get("type") or "photo"
        caption = data.get("caption", "")
        text_content = data.get("text_content") or data.get("text", "")
        background_color = data.get("background_color", "#1E293B")
        font_style = data.get("font_style", "default")
        visibility = data.get("visibility")

        selected_contacts = data.get("selected_contacts")
        if isinstance(selected_contacts, str):
            try:
                selected_contacts = json.loads(selected_contacts)
            except Exception:
                selected_contacts = [int(x.strip()) for x in selected_contacts.split(",") if x.strip().isdigit()]

        excluded_contacts = data.get("excluded_contacts")
        if isinstance(excluded_contacts, str):
            try:
                excluded_contacts = json.loads(excluded_contacts)
            except Exception:
                excluded_contacts = [int(x.strip()) for x in excluded_contacts.split(",") if x.strip().isdigit()]

        media_file = request.FILES.get("file") or request.FILES.get("media") or request.FILES.get("image") or request.FILES.get("video")
        media_url = data.get("media_url", "")
        media_type = data.get("media_type", "")

        status_type_lower = str(status_type).lower().strip()
        if status_type_lower in ("photo", "video", "image") and not media_file and not media_url:
            if text_content or caption:
                status_type = "text"
                text_content = text_content or caption
            else:
                return Response(
                    {"detail": f"Media file or media_url is required for {status_type} status."},
                    status=status.HTTP_400_BAD_REQUEST
                )

        new_status = create_status(
            user=request.user,
            status_type=status_type,
            caption=caption,
            text_content=text_content,
            background_color=background_color,
            font_style=font_style,
            media_file=media_file,
            media_url=media_url,
            media_type=media_type,
            visibility=visibility,
            selected_contacts=selected_contacts,
            excluded_contacts=excluded_contacts,
            request=request,
        )

        return Response(new_status, status=status.HTTP_201_CREATED)

    def get(self, request):
        from .user_preferences import get_status_feed
        feed = get_status_feed(request.user, request=request)
        return Response(feed, status=status.HTTP_200_OK)


class StatusDetailView(APIView):
    """
    GET    /api/status/<status_id>/ - View single status (records view automatically)
    DELETE /api/status/<status_id>/ - Delete status (owner only)
    """
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request, status_id):
        from .user_preferences import get_status_by_id, can_view_status, record_status_view
        status_data, owner_uid = get_status_by_id(status_id)
        if not status_data:
            return Response({"detail": "Status not found or expired."}, status=status.HTTP_404_NOT_FOUND)

        try:
            from django.contrib.auth import get_user_model
            User = get_user_model()
            creator = User.objects.get(id=int(owner_uid), is_active=True)
        except Exception:
            return Response({"detail": "Creator user not found."}, status=status.HTTP_404_NOT_FOUND)

        if not can_view_status(creator, request.user, status_data):
            return Response({"detail": "You do not have permission to view this status."}, status=status.HTTP_403_FORBIDDEN)

        record_status_view(status_id, request.user, request=request)

        if request.user.id == creator.id:
            return Response(status_data, status=status.HTTP_200_OK)
        else:
            safe_copy = dict(status_data)
            safe_copy.pop("viewers", None)
            safe_copy["is_viewed"] = True
            return Response(safe_copy, status=status.HTTP_200_OK)

    def delete(self, request, status_id):
        from .user_preferences import delete_status
        success, detail = delete_status(status_id, request.user)
        if not success:
            return Response({"detail": detail}, status=status.HTTP_400_BAD_REQUEST if "creator" not in detail else status.HTTP_403_FORBIDDEN)
        return Response({"detail": detail}, status=status.HTTP_200_OK)


class StatusViewRecordView(APIView):
    """
    POST /api/status/<status_id>/view/
    Explicitly record that user viewed the status.
    """
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request, status_id):
        from .user_preferences import record_status_view
        success, is_new, status_data, err = record_status_view(status_id, request.user, request=request)
        if not success:
            status_code = status.HTTP_403_FORBIDDEN if "permission" in (err or "") else status.HTTP_404_NOT_FOUND
            return Response({"detail": err}, status=status_code)

        return Response({
            "status_id": str(status_id),
            "viewed": True,
            "is_new_view": is_new,
            "view_count": status_data.get("view_count", 0),
            "viewed_at": timezone.now().isoformat(),
        }, status=status.HTTP_200_OK)


class StatusViewersListView(APIView):
    """
    GET /api/status/<status_id>/viewers/
    Backend records viewers and allows the creator to view who viewed it.
    """
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request, status_id):
        from .user_preferences import get_status_viewers
        success, err, data = get_status_viewers(status_id, request.user)
        if not success:
            status_code = status.HTTP_403_FORBIDDEN if "creator" in (err or "") else status.HTTP_404_NOT_FOUND
            return Response({"detail": err}, status=status_code)

        return Response(data, status=status.HTTP_200_OK)


class UserStatusListView(APIView):
    """
    GET /api/status/user/<user_id>/
    View all active statuses posted by a specific user.
    """
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request, user_id):
        from django.contrib.auth import get_user_model
        from .user_preferences import get_user_statuses, can_view_status
        User = get_user_model()
        try:
            target_user = User.objects.get(id=user_id, is_active=True)
        except User.DoesNotExist:
            return Response({"detail": "User not found."}, status=status.HTTP_404_NOT_FOUND)

        active = get_user_statuses(user_id, active_only=True)
        visible = []
        is_owner = request.user.id == target_user.id

        for s in active:
            if can_view_status(target_user, request.user, s):
                if is_owner:
                    visible.append(s)
                else:
                    s_copy = dict(s)
                    viewers = s_copy.pop("viewers", [])
                    s_copy["is_viewed"] = any(v.get("user_id") == request.user.id for v in viewers)
                    visible.append(s_copy)

        return Response({
            "user_id": target_user.id,
            "username": target_user.username,
            "display_name": getattr(target_user, "display_name", None) or getattr(target_user, "full_name", None) or target_user.username,
            "status_count": len(visible),
            "statuses": visible,
        }, status=status.HTTP_200_OK)


class StatusMuteView(APIView):
    """
    POST /api/status/mute/<user_id>/
    POST /api/status/unmute/<user_id>/
    """
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request, user_id, action="mute"):
        from .user_preferences import mute_status_user, unmute_status_user
        if action == "unmute":
            success, detail = unmute_status_user(request.user, user_id)
        else:
            success, detail = mute_status_user(request.user, user_id)
        return Response({"detail": detail, "muted": (action == "mute")}, status=status.HTTP_200_OK)


class StatusPrivacySettingsView(APIView):
    """
    GET   /api/status/settings/ - Get default status visibility preference
    PATCH /api/status/settings/ - Update default status visibility preference
    """
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        from .user_preferences import get_section_preferences
        prefs = get_section_preferences(request.user.id, "privacy") or {}
        val = prefs.get("status", "contacts")
        selected = prefs.get("status_selected_contacts", [])
        return Response({
            "value": val,
            "options": ["everyone", "contacts", "selected", "nobody"],
            "labels": {
                "everyone": "Everyone",
                "contacts": "My Contacts",
                "selected": "Selected Contacts",
                "nobody": "Nobody",
            },
            "selected_contacts": selected,
        })

    def patch(self, request):
        from .user_preferences import get_section_preferences, update_section_preferences
        prefs = get_section_preferences(request.user.id, "privacy") or {}
        val = request.data.get("value") or request.data.get("status")
        if val is not None:
            val = str(val).lower().strip()
            if val in ("everyone", "contacts", "selected", "nobody"):
                prefs["status"] = val
        if "selected_contacts" in request.data:
            sel = request.data["selected_contacts"]
            if isinstance(sel, list):
                prefs["status_selected_contacts"] = [int(x) for x in sel if str(x).isdigit()]

        update_section_preferences(request.user.id, "privacy", prefs)
        return Response({
            "detail": "Status privacy updated successfully.",
            "value": prefs.get("status", "contacts"),
            "selected_contacts": prefs.get("status_selected_contacts", []),
        })

    put = patch


# ============================================================
# PROFILE & EXI CARD SECTION
# ============================================================

class EXICardView(APIView):
    """
    Digital EXI Card:
    - GET   /api/profile/card/ or /api/profile/exi-card/ — Get my Digital EXI Card
    - PATCH /api/profile/card/ — Update card theme and styling
    """
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        from .user_preferences import get_user_exi_card
        card = get_user_exi_card(request.user, request=request, viewer=request.user)
        return Response(card, status=status.HTTP_200_OK)

    def patch(self, request):
        from .user_preferences import update_user_exi_card
        card = update_user_exi_card(request.user, request.data)
        return Response({
            "detail": "Digital EXI Card updated.",
            **card,
        }, status=status.HTTP_200_OK)

    put = patch


class PublicEXICardView(APIView):
    """
    GET /api/profile/card/<exi_id>/
    Public digital card resolution by EXI ID.
    """
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request, exi_id):
        from .user_preferences import get_user_exi_card
        try:
            target_user = User.objects.get(exi_id=exi_id, is_active=True)
        except User.DoesNotExist:
            return Response({"detail": "User with this EXI ID not found."}, status=status.HTTP_404_NOT_FOUND)

        card = get_user_exi_card(target_user, request=request, viewer=request.user)
        return Response(card, status=status.HTTP_200_OK)


class EditProfileView(APIView):
    """
    PATCH /api/profile/edit/
    Edit profile photo, name, username, bio, and status message.
    """
    permission_classes = [permissions.IsAuthenticated]

    def patch(self, request):
        user = request.user
        data = request.data

        if "display_name" in data:
            user.display_name = str(data["display_name"]).strip()[:50]
        if "full_name" in data:
            user.full_name = str(data["full_name"]).strip()[:100]
        if "bio" in data:
            user.bio = str(data["bio"]).strip()[:160]
        if "status_message" in data:
            user.status_message = str(data["status_message"]).strip()[:100]

        if "username" in data:
            new_u = str(data["username"]).strip().lower()
            if new_u and new_u != user.username:
                if User.objects.filter(username=new_u).exclude(id=user.id).exists():
                    return Response({"detail": "This username is already taken."}, status=status.HTTP_400_BAD_REQUEST)
                user.username = new_u

        photo_file = request.FILES.get("profile_photo") or request.FILES.get("photo") or request.FILES.get("avatar")
        if photo_file:
            user.profile_photo = photo_file

        user.save()

        from .serializers import ProfileSerializer
        return Response({
            "detail": "Profile updated successfully.",
            "profile": ProfileSerializer(user, context={"request": request}).data,
        }, status=status.HTTP_200_OK)

    put = patch


# ============================================================
# SHARE PROFILE HEALTH CARD SECTION
# ============================================================

class HealthCardView(APIView):
    """
    Personal Health Identity Card & Emergency information:
    - GET   /api/health/card/ — Get Health Card
    - PATCH /api/health/card/ — Update Health Card details
    """
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        from .user_preferences import get_user_health_card
        card = get_user_health_card(request.user, request=request)
        return Response(card, status=status.HTTP_200_OK)

    def patch(self, request):
        from .user_preferences import update_user_health_card
        card = update_user_health_card(request.user, request.data, request=request)
        return Response({
            "detail": "Health Card updated successfully.",
            **card,
        }, status=status.HTTP_200_OK)

    put = patch


class HealthEmergencyContactView(APIView):
    """
    POST   /api/health/card/emergency-contacts/ — Add emergency contact
    DELETE /api/health/card/emergency-contacts/<contact_id>/ — Delete emergency contact
    """
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request):
        from .user_preferences import add_emergency_contact
        name = request.data.get("name")
        phone = request.data.get("phone_number") or request.data.get("phone")
        if not name or not phone:
            return Response({"detail": "name and phone_number are required."}, status=status.HTTP_400_BAD_REQUEST)

        contact = add_emergency_contact(request.user, request.data)
        return Response({
            "detail": "Emergency contact added.",
            "contact": contact,
        }, status=status.HTTP_201_CREATED)

    def delete(self, request, contact_id):
        from .user_preferences import delete_emergency_contact
        delete_emergency_contact(request.user, contact_id)
        return Response({"detail": "Emergency contact removed."}, status=status.HTTP_200_OK)


class HealthFamilyProfilesView(APIView):
    """
    GET  /api/health/family/ — List family health profiles
    POST /api/health/family/ — Add a family health profile
    """
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        from .user_preferences import get_family_profiles
        profiles = get_family_profiles(request.user)
        return Response({"count": len(profiles), "family_profiles": profiles}, status=status.HTTP_200_OK)

    def post(self, request):
        from .user_preferences import add_family_profile
        name = request.data.get("full_name") or request.data.get("name")
        if not name:
            return Response({"detail": "full_name is required."}, status=status.HTTP_400_BAD_REQUEST)

        profile = add_family_profile(request.user, request.data)
        return Response({
            "detail": "Family profile added successfully.",
            "family_profile": profile,
        }, status=status.HTTP_201_CREATED)


class HealthFamilyProfileDetailView(APIView):
    """
    PATCH  /api/health/family/<family_id>/ — Edit family profile
    DELETE /api/health/family/<family_id>/ — Delete family profile
    """
    permission_classes = [permissions.IsAuthenticated]

    def patch(self, request, family_id):
        from .user_preferences import update_family_profile
        updated = update_family_profile(request.user, family_id, request.data)
        if not updated:
            return Response({"detail": "Family profile not found."}, status=status.HTTP_404_NOT_FOUND)
        return Response({
            "detail": "Family profile updated.",
            "family_profile": updated,
        }, status=status.HTTP_200_OK)

    put = patch

    def delete(self, request, family_id):
        from .user_preferences import delete_family_profile
        delete_family_profile(request.user, family_id)
        return Response({"detail": "Family profile deleted."}, status=status.HTTP_200_OK)


class PublicHealthCardShareView(APIView):
    """
    GET /api/health/card/share/<token>/
    Public emergency health card view for first responders and emergency contacts.
    """
    permission_classes = [permissions.AllowAny]

    def get(self, request, token):
        from .user_preferences import get_health_card_by_share_token
        card = get_health_card_by_share_token(token, request=request)
        if not card:
            return Response({"detail": "Emergency health card not found or invalid token."}, status=status.HTTP_404_NOT_FOUND)
        return Response(card, status=status.HTTP_200_OK)


# ============================================================
# MY VAULT - SECURE PERSONAL DOCUMENT STORAGE SECTION
# ============================================================

class VaultOverviewView(APIView):
    """
    GET /api/vault/ or /api/vault/overview/
    Vault summary, categories, stats, storage usage, and recent history.
    """
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        from .user_preferences import get_user_vault_overview
        category = request.query_params.get("category")
        search = request.query_params.get("search")
        file_type = request.query_params.get("file_type")
        overview = get_user_vault_overview(request.user, request=request, category=category, search=search, file_type=file_type)
        return Response(overview, status=status.HTTP_200_OK)


class VaultPinSetView(APIView):
    """
    POST /api/vault/pin/set/
    Set or change PIN for My Vault.
    """
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request):
        from .user_preferences import set_vault_pin
        pin = request.data.get("pin")
        if not pin:
            return Response({"detail": "pin is required (4-8 digits)."}, status=status.HTTP_400_BAD_REQUEST)

        success, detail = set_vault_pin(request.user, pin)
        if not success:
            return Response({"detail": detail}, status=status.HTTP_400_BAD_REQUEST)
        return Response({"detail": detail, "vault_lock_enabled": True}, status=status.HTTP_200_OK)


class VaultUnlockView(APIView):
    """
    POST /api/vault/unlock/ — Unlock vault with PIN or biometrics
    POST /api/vault/lock/   — Lock vault session
    """
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request, action="unlock"):
        from .user_preferences import verify_vault_unlock
        if action == "lock":
            return Response({"detail": "Vault locked.", "is_locked": True}, status=status.HTTP_200_OK)

        pin = request.data.get("pin")
        biometric_token = request.data.get("biometric_token")
        success, detail, session_token = verify_vault_unlock(request.user, pin=pin, biometric_token=biometric_token, request=request)
        if not success:
            return Response({"detail": detail}, status=status.HTTP_403_FORBIDDEN)

        return Response({
            "detail": detail,
            "vault_token": session_token,
            "is_unlocked": True,
            "expires_in_seconds": 900,
        }, status=status.HTTP_200_OK)


class VaultDocumentsView(APIView):
    """
    GET  /api/vault/documents/ — List documents (filter by category, file_type, search)
    POST /api/vault/documents/ — Upload document (PDFs, Images, Docs)
    """
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        from .user_preferences import get_user_vault_overview
        category = request.query_params.get("category")
        search = request.query_params.get("search")
        file_type = request.query_params.get("file_type")
        overview = get_user_vault_overview(request.user, request=request, category=category, search=search, file_type=file_type)
        return Response({
            "count": len(overview["documents"]),
            "documents": overview["documents"],
            "stats": overview["stats"],
        }, status=status.HTTP_200_OK)

    def post(self, request):
        from .user_preferences import add_vault_document
        title = request.data.get("title")
        category = request.data.get("category", "personal")
        description = request.data.get("description", "")
        tags = request.data.get("tags")
        file_obj = request.FILES.get("file") or request.FILES.get("document")
        file_url = request.data.get("file_url", "")

        if not file_obj and not file_url:
            return Response({"detail": "File or file_url is required."}, status=status.HTTP_400_BAD_REQUEST)
        if not title and file_obj:
            title = file_obj.name

        doc = add_vault_document(
            user=request.user,
            title=title,
            category=category,
            file=file_obj,
            file_url=file_url,
            description=description,
            tags=tags,
            request=request,
        )
        return Response(doc, status=status.HTTP_201_CREATED)


class VaultDocumentDetailView(APIView):
    """
    GET    /api/vault/documents/<doc_id>/ — View document details
    PATCH  /api/vault/documents/<doc_id>/ — Update document metadata
    DELETE /api/vault/documents/<doc_id>/ — Delete document
    """
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request, doc_id):
        from .user_preferences import get_vault_document
        doc = get_vault_document(request.user, doc_id, request=request)
        if not doc:
            return Response({"detail": "Document not found."}, status=status.HTTP_404_NOT_FOUND)
        return Response(doc, status=status.HTTP_200_OK)

    def patch(self, request, doc_id):
        from .user_preferences import update_vault_document
        updated = update_vault_document(request.user, doc_id, request.data, request=request)
        if not updated:
            return Response({"detail": "Document not found."}, status=status.HTTP_404_NOT_FOUND)
        return Response({
            "detail": "Document updated.",
            "document": updated,
        }, status=status.HTTP_200_OK)

    put = patch

    def delete(self, request, doc_id):
        from .user_preferences import delete_vault_document
        success, detail = delete_vault_document(request.user, doc_id, request=request)
        if not success:
            return Response({"detail": detail}, status=status.HTTP_404_NOT_FOUND)
        return Response({"detail": detail}, status=status.HTTP_200_OK)


class VaultDocumentShareView(APIView):
    """
    POST /api/vault/documents/<doc_id>/share/ — Share document with another user
    GET  /api/vault/shared-with-me/           — Documents shared with current user
    """
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request, doc_id):
        from .user_preferences import share_vault_document
        recipient_id = request.data.get("recipient_id") or request.data.get("user_id")
        recipient_exi = request.data.get("recipient_exi_id") or request.data.get("exi_id")
        permissions_level = request.data.get("permissions", "view")
        expires_hours = request.data.get("expires_in_hours", 24)
        try:
            expires_hours = int(expires_hours)
        except (ValueError, TypeError):
            expires_hours = 24

        try:
            if recipient_id:
                rec_user = User.objects.get(id=int(recipient_id), is_active=True)
            elif recipient_exi:
                rec_user = User.objects.get(exi_id=str(recipient_exi).strip(), is_active=True)
            else:
                return Response({"detail": "recipient_id or recipient_exi_id is required."}, status=status.HTTP_400_BAD_REQUEST)
        except (User.DoesNotExist, ValueError):
            return Response({"detail": "Recipient user not found."}, status=status.HTTP_404_NOT_FOUND)

        success, detail, record = share_vault_document(
            user=request.user,
            doc_id=doc_id,
            recipient_user=rec_user,
            permissions=permissions_level,
            expires_in_hours=expires_hours,
            request=request,
        )
        if not success:
            return Response({"detail": detail}, status=status.HTTP_404_NOT_FOUND)
        return Response({"detail": detail, "share": record}, status=status.HTTP_200_OK)

    def get(self, request):
        from .user_preferences import get_shared_with_me_documents
        docs = get_shared_with_me_documents(request.user)
        return Response({
            "count": len(docs),
            "shared_documents": docs,
        }, status=status.HTTP_200_OK)


class VaultHistoryView(APIView):
    """
    GET /api/vault/history/
    Audit log / access history of vault activities.
    """
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        from .user_preferences import get_vault_history
        limit = request.query_params.get("limit", 50)
        try:
            limit = int(limit)
        except (ValueError, TypeError):
            limit = 50

        history = get_vault_history(request.user, limit=limit)
        return Response({
            "count": len(history),
            "history": history,
        }, status=status.HTTP_200_OK)



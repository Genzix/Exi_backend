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
    DeleteAccountSerializer,
    DeviceKeyUploadSerializer,
    DeviceRegisterSerializer,
    DeviceSerializer,
    PrivacySettingsSerializer,
)
from .serializers import get_tokens, ProfileSerializer

User = get_user_model()


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


class PrivacySettingsView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        settings = get_or_create_privacy_settings(request.user)
        return Response(PrivacySettingsSerializer(settings).data)

    def patch(self, request):
        settings = get_or_create_privacy_settings(request.user)
        serializer = PrivacySettingsSerializer(settings, data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        serializer.save()
        return Response(serializer.data)


class BlockUserView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request, user_id):
        try:
            blocked = User.objects.get(id=user_id)
        except User.DoesNotExist:
            return Response({'detail': 'User not found.'}, status=status.HTTP_404_NOT_FOUND)
            
        if blocked.id == request.user.id:
            return Response({'detail': 'Cannot block yourself.'}, status=status.HTTP_400_BAD_REQUEST)

        reason = request.data.get('reason', '')
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

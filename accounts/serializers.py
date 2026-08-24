import secrets
from datetime import timedelta

from django.conf import settings as django_settings
from django.contrib.auth import get_user_model
from django.contrib.auth.hashers import check_password as check_hash, make_password
from django.contrib.auth.password_validation import validate_password
from django.contrib.auth.tokens import PasswordResetTokenGenerator
from django.utils import timezone
from django.utils.encoding import force_bytes, force_str
from django.utils.http import urlsafe_base64_decode, urlsafe_base64_encode
from rest_framework import serializers
from rest_framework_simplejwt.tokens import RefreshToken

from .models import OTPVerification, normalize_phone

User = get_user_model()
reset_tokens = PasswordResetTokenGenerator()

MAX_RESET_CODE_ATTEMPTS = 5


def get_tokens(user):
    refresh = RefreshToken.for_user(user)
    refresh["exi_id"] = user.exi_id
    refresh["display_name"] = user.display_name
    refresh["token_version"] = user.token_version
    return {
        "access": str(refresh.access_token),
        "refresh": str(refresh),
    }


def split_email_or_phone(value):
    """Return (email, phone) — exactly one is set."""
    value = value.strip()
    if "@" in value:
        return value.lower(), None
    phone = normalize_phone(value)
    if len(phone) < 8:
        raise serializers.ValidationError({"email_or_phone": "Enter a valid email or phone number."})
    return None, phone


# ---------- Profile responses ----------

class ProfileSerializer(serializers.ModelSerializer):
    class Meta:
        model = User
        fields = [
            "id",
            "email",
            "phone",
            "full_name",
            "display_name",
            "profile_photo",
            "exi_id",
            "is_online",
            "last_seen",
            "bio",
            "status_message",
            "is_business",
            "business_name",
            "business_address",
            "business_website",
            "profile_setup_complete",
            "date_joined",
        ]
        read_only_fields = fields


class PrivacyMaskedProfileMixin:
    """Hides online status, last seen, and photo per the profile owner's settings.

    Declare the masked fields as SerializerMethodField on the serializer using it.
    """

    profile_photo = serializers.SerializerMethodField()
    is_online = serializers.SerializerMethodField()
    last_seen = serializers.SerializerMethodField()

    def _visible(self, obj):
        from .utils import visible_profile_fields

        cache = self.context.setdefault("_privacy_cache", {})
        if obj.id not in cache:
            request = self.context.get("request")
            viewer = getattr(request, "user", None) if request else None
            if viewer is not None and not viewer.is_authenticated:
                viewer = None
            cache[obj.id] = visible_profile_fields(obj, viewer)
        return cache[obj.id]

    def get_profile_photo(self, obj):
        if not obj.profile_photo or not self._visible(obj)["profile_photo"]:
            return None
        request = self.context.get("request")
        url = obj.profile_photo.url
        return request.build_absolute_uri(url) if request else url

    def get_is_online(self, obj):
        return obj.is_online if self._visible(obj)["online_status"] else None

    def get_last_seen(self, obj):
        return obj.last_seen if self._visible(obj)["last_seen"] else None


class PublicProfileSerializer(PrivacyMaskedProfileMixin, serializers.ModelSerializer):
    """Public view of a profile, masked according to the owner's privacy settings."""

    class Meta:
        model = User
        fields = [
            "display_name",
            "full_name",
            "profile_photo",
            "exi_id",
            "is_online",
            "last_seen",
            "bio",
            "status_message",
            "is_business",
            "business_name",
            "business_address",
            "business_website",
        ]


# ---------- Screen 1: Login ----------

class LoginSerializer(serializers.Serializer):
    email_or_phone = serializers.CharField()
    password = serializers.CharField(write_only=True)

    def validate(self, data):
        user = User.find_by_login(data["email_or_phone"])
        if not user or not user.check_password(data["password"]):
            raise serializers.ValidationError("Invalid email/phone or password.")
        if not user.is_active:
            raise serializers.ValidationError("This account is disabled.")
        data["user"] = user
        return data


class ForgotPasswordSerializer(serializers.Serializer):
    email_or_phone = serializers.CharField()

    def save(self):
        user = User.find_by_login(self.validated_data["email_or_phone"])
        # Same response whether or not the account exists, so the endpoint
        # cannot be used to enumerate registered users.
        message = {"detail": "If an account exists, reset instructions were sent."}

        if not user or not user.is_active:
            return message

        identifier = self.validated_data["email_or_phone"].strip()

        # Email → uid + token
        if "@" in identifier and user.email:
            uid = urlsafe_base64_encode(force_bytes(user.pk))
            token = reset_tokens.make_token(user)
            print(f"[reset email] uid={uid} token={token}")
            if django_settings.DEBUG:
                message["uid"] = uid
                message["token"] = token
            return message

        # Phone → 6-digit OTP (15 min), stored hashed
        code = f"{secrets.randbelow(1_000_000):06d}"
        user.reset_code = make_password(code)
        user.reset_code_expires = timezone.now() + timedelta(minutes=15)
        user.reset_code_attempts = 0
        user.save(
            update_fields=["reset_code", "reset_code_expires", "reset_code_attempts"]
        )
        print(f"[reset phone] code={code}")
        if django_settings.DEBUG:
            message["code"] = code
        return message


class ResetPasswordSerializer(serializers.Serializer):
    password = serializers.CharField(write_only=True, min_length=8)
    password_confirm = serializers.CharField(write_only=True)

    # email reset
    uid = serializers.CharField(required=False, allow_blank=True)
    token = serializers.CharField(required=False, allow_blank=True)

    # phone reset
    email_or_phone = serializers.CharField(required=False, allow_blank=True)
    code = serializers.CharField(required=False, allow_blank=True)

    def validate(self, data):
        if data["password"] != data["password_confirm"]:
            raise serializers.ValidationError({"password_confirm": "Passwords do not match."})
        validate_password(data["password"])

        uid = data.get("uid") or ""
        token = data.get("token") or ""
        code = data.get("code") or ""
        login = (data.get("email_or_phone") or "").strip()

        if uid and token:
            data["user"] = self._user_from_email_token(uid, token)
        elif login and code:
            data["user"] = self._user_from_phone_code(login, code)
        else:
            raise serializers.ValidationError("Provide uid+token or email_or_phone+code.")

        return data

    def _user_from_email_token(self, uid, token):
        try:
            user = User.objects.get(pk=force_str(urlsafe_base64_decode(uid)))
        except (User.DoesNotExist, ValueError, TypeError, OverflowError):
            raise serializers.ValidationError("Invalid or expired reset link.")
        if not reset_tokens.check_token(user, token):
            raise serializers.ValidationError("Invalid or expired reset link.")
        return user

    def _user_from_phone_code(self, login, code):
        invalid = serializers.ValidationError("Invalid or expired reset code.")

        user = User.find_by_login(login)
        if not user or not user.reset_code or not user.reset_code_expires:
            raise invalid
        if user.reset_code_expires < timezone.now():
            raise invalid
        if user.reset_code_attempts >= MAX_RESET_CODE_ATTEMPTS:
            raise serializers.ValidationError(
                "Too many incorrect codes. Request a new reset code."
            )

        if not check_hash(code, user.reset_code):
            user.reset_code_attempts += 1
            user.save(update_fields=["reset_code_attempts"])
            raise invalid

        return user

    def save(self):
        user = self.validated_data["user"]
        user.set_password(self.validated_data["password"])
        user.reset_code = ""
        user.reset_code_expires = None
        user.reset_code_attempts = 0
        # Force every existing session to re-authenticate with the new password.
        user.token_version += 1
        user.save(
            update_fields=[
                "password",
                "reset_code",
                "reset_code_expires",
                "reset_code_attempts",
                "token_version",
            ]
        )
        return user


# ---------- Screen 2: Registration ----------

class RegisterSerializer(serializers.Serializer):
    full_name = serializers.CharField(max_length=100)
    email_or_phone = serializers.CharField(max_length=255)
    password = serializers.CharField(write_only=True, min_length=8)
    password_confirm = serializers.CharField(write_only=True)

    def validate_full_name(self, value):
        value = value.strip()
        if len(value) < 2:
            raise serializers.ValidationError("Enter your full name.")
        return value

    def validate(self, data):
        if data["password"] != data["password_confirm"]:
            raise serializers.ValidationError({"password_confirm": "Passwords do not match."})
        validate_password(data["password"])

        email, phone = split_email_or_phone(data["email_or_phone"])
        identifier = email or phone

        if email and User.objects.filter(email__iexact=email).exists():
            raise serializers.ValidationError({"email_or_phone": "Email already registered."})
        if phone and User.objects.filter(phone=phone).exists():
            raise serializers.ValidationError({"email_or_phone": "Phone already registered."})

        try:
            otp_obj = OTPVerification.objects.get(email_or_phone=identifier)
        except OTPVerification.DoesNotExist:
            raise serializers.ValidationError(
                {"email_or_phone": "Please verify OTP before registering."}
            )
        if not otp_obj.is_verified:
            raise serializers.ValidationError(
                {"email_or_phone": "Please verify OTP before registering."}
            )
        if otp_obj.expires_at < timezone.now():
            raise serializers.ValidationError(
                {"email_or_phone": "OTP verification expired. Request a new OTP."}
            )

        data["email"] = email
        data["phone"] = phone
        data["otp_obj"] = otp_obj
        return data

    def create(self, data):
        user = User(
            email=data["email"],
            phone=data["phone"],
            full_name=data["full_name"],
            username=data["email"] or data["phone"],
        )
        user.set_password(data["password"])
        user.save()
        data["otp_obj"].delete()
        return user


# ---------- Screen 3: Profile Setup ----------

class ProfileSetupSerializer(serializers.ModelSerializer):
    is_online = serializers.BooleanField(required=False, default=True)

    class Meta:
        model = User
        fields = ["display_name", "profile_photo", "is_online"]

    def validate_display_name(self, value):
        value = value.strip()
        if not value:
            raise serializers.ValidationError("Display name is required.")
        return value

    def update(self, user, data):
        user.display_name = data["display_name"]
        if "profile_photo" in data:
            user.profile_photo = data["profile_photo"]
        user.is_online = data.get("is_online", True)
        user.last_seen = timezone.now()
        user.profile_setup_complete = True
        user.save()
        return user


# ---------- Edit profile later ----------

class ProfileUpdateSerializer(serializers.ModelSerializer):
    class Meta:
        model = User
        fields = ["full_name", "display_name", "profile_photo", "bio", "status_message", "is_business", "business_name", "business_address", "business_website"]

    def validate_display_name(self, value):
        value = value.strip()
        if not value:
            raise serializers.ValidationError("Display name cannot be empty.")
        return value


class OnlineStatusSerializer(serializers.Serializer):
    is_online = serializers.BooleanField()


# ---------- OTP Verification ----------

class SendOTPSerializer(serializers.Serializer):
    email_or_phone = serializers.CharField(max_length=255)

    def validate(self, data):
        email, phone = split_email_or_phone(data["email_or_phone"])
        data["email_or_phone"] = email or phone
        
        # Check if already registered
        if email and User.objects.filter(email__iexact=email).exists():
            raise serializers.ValidationError({"email_or_phone": "Email already registered."})
        if phone and User.objects.filter(phone=phone).exists():
            raise serializers.ValidationError({"email_or_phone": "Phone already registered."})
            
        return data

    def save(self):
        identifier = self.validated_data["email_or_phone"]
        code = f"{secrets.randbelow(1_000_000):06d}"
        expires = timezone.now() + timedelta(minutes=10)

        OTPVerification.objects.update_or_create(
            email_or_phone=identifier,
            defaults={
                "otp_code": make_password(code),
                "expires_at": expires,
                "is_verified": False,
                "attempts": 0,
            },
        )

        # In production, send via SMS/Email here
        print(f"[Send OTP] To {identifier}: {code}")

        response = {"detail": "OTP sent successfully."}
        if django_settings.DEBUG:
            response["otp_code"] = code
        return response


class VerifyOTPSerializer(serializers.Serializer):
    email_or_phone = serializers.CharField(max_length=255)
    otp_code = serializers.CharField(max_length=6)

    def validate(self, data):
        email, phone = split_email_or_phone(data["email_or_phone"])
        identifier = email or phone

        try:
            otp_obj = OTPVerification.objects.get(email_or_phone=identifier)
        except OTPVerification.DoesNotExist:
            raise serializers.ValidationError("No OTP found for this email/phone.")

        if otp_obj.expires_at < timezone.now():
            raise serializers.ValidationError("OTP has expired.")

        if otp_obj.is_locked:
            raise serializers.ValidationError(
                "Too many incorrect attempts. Request a new OTP."
            )

        if not check_hash(data["otp_code"], otp_obj.otp_code):
            otp_obj.attempts += 1
            otp_obj.save(update_fields=["attempts"])
            raise serializers.ValidationError("Invalid OTP.")

        data["otp_obj"] = otp_obj
        return data

    def save(self):
        otp_obj = self.validated_data["otp_obj"]
        otp_obj.is_verified = True
        otp_obj.attempts = 0
        otp_obj.save(update_fields=["is_verified", "attempts"])
        return {"detail": "OTP verified successfully."}


# --- Appended from security_serializers.py ---

from django.contrib.auth import get_user_model
from django.contrib.auth.password_validation import validate_password
from rest_framework import serializers

from .models import AdminActivityLog, BlockedUser, UserDevice, UserPrivacySettings

User = get_user_model()


class DeviceRegisterSerializer(serializers.Serializer):
    device_id = serializers.CharField(max_length=64)
    device_name = serializers.CharField(max_length=100, required=False, allow_blank=True)
    platform = serializers.ChoiceField(
        choices=['ios', 'android', 'web', 'desktop', 'unknown'],
        default='unknown',
    )
    push_token = serializers.CharField(max_length=512, required=False, allow_blank=True)
    app_version = serializers.CharField(max_length=32, required=False, allow_blank=True)


class DeviceSerializer(serializers.ModelSerializer):
    is_current = serializers.SerializerMethodField()
    has_encryption_keys = serializers.BooleanField(source='supports_e2ee', read_only=True)

    class Meta:
        model = UserDevice
        fields = [
            'id', 'device_id', 'device_name', 'platform', 'app_version',
            'ip_address', 'is_active', 'is_current', 'has_encryption_keys',
            'keys_updated_at', 'last_active', 'created_at',
        ]
        read_only_fields = fields

    def get_is_current(self, obj):
        current_id = self.context.get('current_device_id')
        return bool(current_id and obj.device_id == current_id)


class ChangePasswordSerializer(serializers.Serializer):
    current_password = serializers.CharField(write_only=True)
    new_password = serializers.CharField(write_only=True, min_length=8)
    new_password_confirm = serializers.CharField(write_only=True)

    def validate(self, data):
        user = self.context['request'].user
        if not user.check_password(data['current_password']):
            raise serializers.ValidationError({'current_password': 'Current password is incorrect.'})
        if data['new_password'] != data['new_password_confirm']:
            raise serializers.ValidationError({'new_password_confirm': 'Passwords do not match.'})
        validate_password(data['new_password'], user=user)
        return data

    def save(self):
        user = self.context['request'].user
        user.set_password(self.validated_data['new_password'])
        user.revoke_all_sessions()
        user.save(update_fields=['password', 'token_version'])
        return user


class PrivacySettingsSerializer(serializers.ModelSerializer):
    class Meta:
        model = UserPrivacySettings
        fields = [
            'show_last_seen',
            'show_online_status',
            'show_profile_photo',
            'allow_calls_from',
            'allow_messages_from',
            'read_receipts_enabled',
            'searchable',
            'push_notifications_enabled',
            'in_app_notifications_enabled',
            'updated_at',
        ]
        read_only_fields = ['updated_at']


# ---------- End-to-end encryption key bundles ----------

class OneTimePreKeySerializer(serializers.Serializer):
    key_id = serializers.IntegerField(min_value=0)
    public_key = serializers.CharField(max_length=1024)


class DeviceKeyUploadSerializer(serializers.Serializer):
    device_id = serializers.CharField(max_length=64)
    registration_id = serializers.IntegerField(min_value=0)
    identity_key = serializers.CharField(max_length=1024)
    signed_prekey_id = serializers.IntegerField(min_value=0)
    signed_prekey = serializers.CharField(max_length=1024)
    signed_prekey_signature = serializers.CharField(max_length=1024)
    one_time_prekeys = OneTimePreKeySerializer(many=True, required=False)

    def validate_one_time_prekeys(self, value):
        if len(value) > 100:
            raise serializers.ValidationError('Upload at most 100 one-time prekeys.')
        key_ids = [item['key_id'] for item in value]
        if len(key_ids) != len(set(key_ids)):
            raise serializers.ValidationError('Duplicate key_id values.')
        return value

    def validate_device_id(self, value):
        request = self.context['request']
        try:
            device = UserDevice.objects.get(user=request.user, device_id=value)
        except UserDevice.DoesNotExist:
            raise serializers.ValidationError('Register this device before uploading keys.')
        self.context['device'] = device
        return value


class BlockUserSerializer(serializers.Serializer):
    exi_id = serializers.CharField()
    reason = serializers.CharField(required=False, allow_blank=True, max_length=255)

    def validate_exi_id(self, value):
        request = self.context['request']
        try:
            blocked = User.objects.get(exi_id=value)
        except User.DoesNotExist:
            raise serializers.ValidationError('User not found.')
        if blocked.id == request.user.id:
            raise serializers.ValidationError('You cannot block yourself.')
        self.context['blocked_user'] = blocked
        return value


class BlockedUserSerializer(serializers.ModelSerializer):
    blocked = serializers.SerializerMethodField()

    class Meta:
        model = BlockedUser
        fields = ['id', 'blocked', 'reason', 'created_at']

    def get_blocked(self, obj):
        from .serializers import PublicProfileSerializer
        return PublicProfileSerializer(obj.blocked).data


class DeleteAccountSerializer(serializers.Serializer):
    password = serializers.CharField(write_only=True)
    confirmation = serializers.CharField()

    def validate(self, data):
        if data['confirmation'].strip().upper() != 'DELETE':
            raise serializers.ValidationError({'confirmation': 'Type DELETE to confirm.'})
        user = self.context['request'].user
        if not user.check_password(data['password']):
            raise serializers.ValidationError({'password': 'Password is incorrect.'})
        return data


# --- Appended: Module 10 Admin panel serializers ---


class AdminUserListSerializer(serializers.ModelSerializer):
    reports_received_count = serializers.IntegerField(read_only=True, required=False)
    reports_filed_count = serializers.IntegerField(read_only=True, required=False)

    class Meta:
        model = User
        fields = [
            'id',
            'exi_id',
            'email',
            'phone',
            'username',
            'full_name',
            'display_name',
            'is_active',
            'is_staff',
            'is_superuser',
            'is_online',
            'is_business',
            'profile_setup_complete',
            'last_seen',
            'last_login',
            'date_joined',
            'reports_received_count',
            'reports_filed_count',
        ]
        read_only_fields = fields


class AdminUserDetailSerializer(AdminUserListSerializer):
    active_devices = serializers.SerializerMethodField()
    message_count = serializers.SerializerMethodField()
    call_count = serializers.SerializerMethodField()

    class Meta(AdminUserListSerializer.Meta):
        fields = AdminUserListSerializer.Meta.fields + [
            'bio',
            'status_message',
            'business_name',
            'business_address',
            'business_website',
            'active_devices',
            'message_count',
            'call_count',
            'token_version',
        ]

    def get_active_devices(self, obj):
        return obj.devices.filter(is_active=True).count()

    def get_message_count(self, obj):
        return obj.messages_sent.count()

    def get_call_count(self, obj):
        from django.db.models import Q
        from chats.models import Call

        return Call.objects.filter(Q(caller=obj) | Q(callee=obj)).count()


class AdminUserUpdateSerializer(serializers.Serializer):
    is_active = serializers.BooleanField(required=False)
    is_staff = serializers.BooleanField(required=False)
    display_name = serializers.CharField(required=False, allow_blank=True, max_length=50)
    full_name = serializers.CharField(required=False, allow_blank=True, max_length=100)


class AdminBanSerializer(serializers.Serializer):
    reason = serializers.CharField(required=False, allow_blank=True, max_length=255)


class AdminReportUpdateSerializer(serializers.Serializer):
    status = serializers.ChoiceField(choices=['pending', 'reviewed', 'resolved'])
    resolution_notes = serializers.CharField(required=False, allow_blank=True, max_length=2000)
    ban_user = serializers.BooleanField(required=False, default=False)


class AdminActivityLogSerializer(serializers.ModelSerializer):
    actor_exi_id = serializers.CharField(source='actor.exi_id', read_only=True, allow_null=True)
    actor_display_name = serializers.CharField(
        source='actor.display_name', read_only=True, allow_null=True
    )
    target_user_exi_id = serializers.CharField(
        source='target_user.exi_id', read_only=True, allow_null=True
    )

    class Meta:
        model = AdminActivityLog
        fields = [
            'id',
            'actor',
            'actor_exi_id',
            'actor_display_name',
            'action',
            'target_user',
            'target_user_exi_id',
            'target_type',
            'target_id',
            'summary',
            'metadata',
            'ip_address',
            'created_at',
        ]
        read_only_fields = fields


class AdminReportSerializer(serializers.Serializer):
    """Read-only report payload for staff review."""

    id = serializers.UUIDField()
    reason = serializers.CharField()
    notes = serializers.CharField()
    status = serializers.CharField()
    created_at = serializers.DateTimeField()
    conversation = serializers.UUIDField(source='conversation_id', allow_null=True)
    reporter = serializers.SerializerMethodField()
    reported_user = serializers.SerializerMethodField()

    def get_reporter(self, obj):
        return {
            'id': obj.reporter_id,
            'exi_id': obj.reporter.exi_id,
            'display_name': obj.reporter.display_name,
            'email': obj.reporter.email,
        }

    def get_reported_user(self, obj):
        return {
            'id': obj.reported_user_id,
            'exi_id': obj.reported_user.exi_id,
            'display_name': obj.reported_user.display_name,
            'email': obj.reported_user.email,
            'is_active': obj.reported_user.is_active,
        }

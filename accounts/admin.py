from django.contrib import admin
from django.contrib.auth.admin import UserAdmin as BaseUserAdmin

from .models import (
    AdminActivityLog,
    BlockedUser,
    DevicePreKey,
    OTPVerification,
    User,
    UserDevice,
    UserPrivacySettings,
)


@admin.register(User)
class UserAdmin(BaseUserAdmin):
    list_display = [
        "email",
        "phone",
        "full_name",
        "display_name",
        "exi_id",
        "is_online",
        "profile_setup_complete",
        "date_joined",
    ]
    list_filter = ["is_online", "profile_setup_complete", "is_staff", "is_active"]
    search_fields = ["email", "phone", "full_name", "display_name", "exi_id"]
    ordering = ["-date_joined"]
    readonly_fields = ["exi_id", "last_seen", "date_joined", "last_login"]

    fieldsets = [
        (None, {"fields": ["username", "password"]}),
        ("Contact", {"fields": ["email", "phone", "full_name"]}),
        ("Profile", {
            "fields": [
                "display_name",
                "profile_photo",
                "exi_id",
                "bio",
                "profile_setup_complete",
                "is_online",
                "last_seen",
            ]
        }),
        ("Permissions", {
            "fields": ["is_active", "is_staff", "is_superuser", "groups", "user_permissions"]
        }),
        ("Dates", {"fields": ["last_login", "date_joined"]}),
    ]

    add_fieldsets = [
        (None, {
            "classes": ["wide"],
            "fields": ["email", "phone", "full_name", "username", "password1", "password2"],
        }),
    ]


@admin.register(UserDevice)
class UserDeviceAdmin(admin.ModelAdmin):
    list_display = ["user", "device_name", "platform", "is_active", "supports_e2ee", "last_active"]
    list_filter = ["platform", "is_active"]
    search_fields = ["user__email", "user__exi_id", "device_id", "device_name"]
    # Key material and push tokens are credentials — never editable from admin.
    readonly_fields = [
        "push_token",
        "identity_key",
        "signed_prekey",
        "signed_prekey_signature",
        "registration_id",
        "keys_updated_at",
    ]


@admin.register(DevicePreKey)
class DevicePreKeyAdmin(admin.ModelAdmin):
    list_display = ["device", "key_id", "consumed_at", "created_at"]
    list_filter = ["consumed_at"]
    readonly_fields = ["public_key"]


@admin.register(UserPrivacySettings)
class UserPrivacySettingsAdmin(admin.ModelAdmin):
    list_display = ["user", "allow_messages_from", "allow_calls_from", "searchable", "updated_at"]
    list_filter = ["allow_messages_from", "allow_calls_from", "searchable"]


@admin.register(BlockedUser)
class BlockedUserAdmin(admin.ModelAdmin):
    list_display = ["blocker", "blocked", "reason", "created_at"]
    search_fields = ["blocker__exi_id", "blocked__exi_id"]


@admin.register(OTPVerification)
class OTPVerificationAdmin(admin.ModelAdmin):
    list_display = ["email_or_phone", "is_verified", "attempts", "expires_at", "created_at"]
    list_filter = ["is_verified"]
    # Codes are stored hashed; showing the field read-only avoids accidental edits.
    readonly_fields = ["otp_code"]


@admin.register(AdminActivityLog)
class AdminActivityLogAdmin(admin.ModelAdmin):
    list_display = ["id", "actor", "action", "target_user", "summary", "created_at"]
    list_filter = ["action", "created_at"]
    search_fields = ["summary", "actor__exi_id", "target_user__exi_id", "target_id"]
    readonly_fields = [
        "actor",
        "action",
        "target_user",
        "target_type",
        "target_id",
        "summary",
        "metadata",
        "ip_address",
        "created_at",
    ]
    raw_id_fields = ["actor", "target_user"]

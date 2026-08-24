import re
import secrets
import string

from django.contrib.auth.models import AbstractUser
from django.db import models
from django.utils import timezone


def generate_exi_id():
    chars = string.ascii_uppercase + string.digits
    return "EXI-" + "".join(secrets.choice(chars) for _ in range(6))


def normalize_phone(value):
    value = value.strip()
    if value.startswith("+"):
        return "+" + re.sub(r"\D", "", value[1:])
    return re.sub(r"\D", "", value)


class User(AbstractUser):
    email = models.EmailField(unique=True, null=True, blank=True)
    phone = models.CharField(max_length=20, unique=True, null=True, blank=True)

    full_name = models.CharField(max_length=100, blank=True)
    display_name = models.CharField(max_length=50, blank=True)
    profile_photo = models.ImageField(upload_to="profile_photos/", blank=True, null=True)
    bio = models.CharField(max_length=160, blank=True)
    status_message = models.CharField(max_length=100, blank=True)

    # Business Information
    is_business = models.BooleanField(default=False)
    business_name = models.CharField(max_length=100, blank=True)
    business_address = models.CharField(max_length=255, blank=True)
    business_website = models.URLField(blank=True)

    exi_id = models.CharField(max_length=16, unique=True, editable=False)
    is_online = models.BooleanField(default=False)
    last_seen = models.DateTimeField(null=True, blank=True)
    profile_setup_complete = models.BooleanField(default=False)

    # Hashed reset code — never store the plaintext code
    reset_code = models.CharField(max_length=128, blank=True)
    reset_code_expires = models.DateTimeField(null=True, blank=True)
    reset_code_attempts = models.PositiveSmallIntegerField(default=0)
    token_version = models.PositiveIntegerField(default=0)

    USERNAME_FIELD = "username"
    REQUIRED_FIELDS = []

    class Meta:
        ordering = ["-date_joined"]

    def __str__(self):
        return self.display_name or self.full_name or self.email or self.phone or self.username

    def save(self, *args, **kwargs):
        if not self.exi_id:
            self.exi_id = self._make_exi_id()
        if not self.username:
            self.username = str(self.email or self.phone or self.exi_id)[:150]
        super().save(*args, **kwargs)

    def _make_exi_id(self):
        for _ in range(20):
            exi_id = generate_exi_id()
            if not User.objects.filter(exi_id=exi_id).exists():
                return exi_id
        raise RuntimeError("Could not generate unique EXI ID")

    def set_online(self):
        self.is_online = True
        self.last_seen = timezone.now()
        self.save(update_fields=["is_online", "last_seen"])

    def set_offline(self):
        self.is_online = False
        self.last_seen = timezone.now()
        self.save(update_fields=["is_online", "last_seen"])

    def revoke_all_sessions(self):
        self.token_version += 1
        self.save(update_fields=["token_version"])

    @classmethod
    def find_by_login(cls, email_or_phone):
        value = email_or_phone.strip()
        if "@" in value:
            return cls.objects.filter(email__iexact=value.lower()).first()
        return cls.objects.filter(phone=normalize_phone(value)).first()


class OTPVerification(models.Model):
    MAX_ATTEMPTS = 5

    email_or_phone = models.CharField(max_length=255, unique=True)
    # Hashed OTP — never store the plaintext code
    otp_code = models.CharField(max_length=128)
    expires_at = models.DateTimeField()
    is_verified = models.BooleanField(default=False)
    attempts = models.PositiveSmallIntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"{self.email_or_phone} - {'Verified' if self.is_verified else 'Pending'}"

    @property
    def is_locked(self):
        return self.attempts >= self.MAX_ATTEMPTS


class UserPrivacySettings(models.Model):
    VISIBILITY_CHOICES = (
        ('everyone', 'Everyone'),
        ('contacts', 'Contacts Only'),
        ('nobody', 'Nobody'),
    )

    user = models.OneToOneField(
        User,
        on_delete=models.CASCADE,
        related_name='privacy_settings',
    )
    show_last_seen = models.BooleanField(default=True)
    show_online_status = models.BooleanField(default=True)
    show_profile_photo = models.BooleanField(default=True)
    allow_calls_from = models.CharField(
        max_length=10,
        choices=VISIBILITY_CHOICES,
        default='everyone',
    )
    allow_messages_from = models.CharField(
        max_length=10,
        choices=VISIBILITY_CHOICES,
        default='everyone',
    )
    read_receipts_enabled = models.BooleanField(default=True)
    searchable = models.BooleanField(
        default=True,
        help_text="Allow other users to find this account through search.",
    )
    push_notifications_enabled = models.BooleanField(default=True)
    in_app_notifications_enabled = models.BooleanField(default=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"Privacy settings for {self.user}"


class UserDevice(models.Model):
    PLATFORM_CHOICES = (
        ('ios', 'iOS'),
        ('android', 'Android'),
        ('web', 'Web'),
        ('desktop', 'Desktop'),
        ('unknown', 'Unknown'),
    )

    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name='devices')
    device_id = models.CharField(max_length=64)
    device_name = models.CharField(max_length=100, blank=True)
    platform = models.CharField(max_length=10, choices=PLATFORM_CHOICES, default='unknown')
    push_token = models.CharField(max_length=512, blank=True)
    app_version = models.CharField(max_length=32, blank=True)
    ip_address = models.GenericIPAddressField(null=True, blank=True)
    user_agent = models.CharField(max_length=512, blank=True)
    is_active = models.BooleanField(default=True)
    last_active = models.DateTimeField(auto_now=True)
    created_at = models.DateTimeField(auto_now_add=True)

    # End-to-end encryption key material (public keys only — the server never
    # sees private keys or plaintext for encrypted messages).
    registration_id = models.PositiveIntegerField(null=True, blank=True)
    identity_key = models.TextField(blank=True)
    signed_prekey_id = models.PositiveIntegerField(null=True, blank=True)
    signed_prekey = models.TextField(blank=True)
    signed_prekey_signature = models.TextField(blank=True)
    keys_updated_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        unique_together = ('user', 'device_id')
        ordering = ['-last_active']

    def __str__(self):
        return f"{self.user} — {self.device_name or self.device_id}"

    @property
    def supports_e2ee(self):
        return bool(self.identity_key and self.signed_prekey and self.signed_prekey_signature)


class DevicePreKey(models.Model):
    """One-time prekey published by a device for asynchronous E2EE session setup."""

    device = models.ForeignKey(
        UserDevice,
        on_delete=models.CASCADE,
        related_name='prekeys',
    )
    key_id = models.PositiveIntegerField()
    public_key = models.TextField()
    consumed_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = ('device', 'key_id')
        ordering = ['key_id']

    def __str__(self):
        return f"PreKey {self.key_id} for {self.device_id}"


class BlockedUser(models.Model):
    blocker = models.ForeignKey(
        User,
        on_delete=models.CASCADE,
        related_name='blocked_users',
    )
    blocked = models.ForeignKey(
        User,
        on_delete=models.CASCADE,
        related_name='blocked_by',
    )
    reason = models.CharField(max_length=255, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = ('blocker', 'blocked')
        ordering = ['-created_at']

    def __str__(self):
        return f"{self.blocker} blocked {self.blocked}"


class AdminActivityLog(models.Model):
    """Append-only audit trail for staff actions in the admin API."""

    ACTION_CHOICES = (
        ('user_view', 'Viewed user'),
        ('user_update', 'Updated user'),
        ('user_ban', 'Banned user'),
        ('user_unban', 'Unbanned user'),
        ('user_force_logout', 'Forced logout'),
        ('report_review', 'Reviewed report'),
        ('report_resolve', 'Resolved report'),
        ('report_dismiss', 'Dismissed report'),
        ('moderation_action', 'Moderation action'),
        ('analytics_view', 'Viewed analytics'),
        ('other', 'Other'),
    )

    id = models.BigAutoField(primary_key=True)
    actor = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        null=True,
        related_name='admin_actions',
    )
    action = models.CharField(max_length=32, choices=ACTION_CHOICES)
    target_user = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='admin_actions_received',
    )
    target_type = models.CharField(max_length=50, blank=True)
    target_id = models.CharField(max_length=64, blank=True)
    summary = models.CharField(max_length=255)
    metadata = models.JSONField(default=dict, blank=True)
    ip_address = models.GenericIPAddressField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['-created_at']),
            models.Index(fields=['action', '-created_at']),
        ]

    def __str__(self):
        return f"{self.action} by {self.actor_id} at {self.created_at}"

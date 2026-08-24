import time

from django.core.cache import cache
from django.utils import timezone

from .models import DevicePreKey, UserDevice, UserPrivacySettings


def get_client_ip(request):
    forwarded = request.META.get('HTTP_X_FORWARDED_FOR')
    if forwarded:
        return forwarded.split(',')[0].strip()
    return request.META.get('REMOTE_ADDR')


def log_admin_action(
    request,
    *,
    action,
    summary,
    target_user=None,
    target_type='',
    target_id='',
    metadata=None,
):
    """Persist a staff action for the activity-log feed."""
    from .models import AdminActivityLog

    return AdminActivityLog.objects.create(
        actor=request.user if request.user.is_authenticated else None,
        action=action,
        target_user=target_user,
        target_type=target_type or '',
        target_id=str(target_id) if target_id else '',
        summary=summary[:255],
        metadata=metadata or {},
        ip_address=get_client_ip(request),
    )

def register_or_update_device(user, request, data=None):
    data = data or {}
    device_id = data.get('device_id')
    if not device_id:
        return None

    defaults = {
        'device_name': data.get('device_name', ''),
        'platform': data.get('platform', 'unknown'),
        'push_token': data.get('push_token', ''),
        'app_version': data.get('app_version', ''),
        'ip_address': get_client_ip(request),
        'user_agent': request.META.get('HTTP_USER_AGENT', '')[:512],
        'is_active': True,
        'last_active': timezone.now(),
    }

    device, _ = UserDevice.objects.update_or_create(
        user=user,
        device_id=device_id,
        defaults=defaults,
    )
    return device


def get_or_create_privacy_settings(user):
    settings, _ = UserPrivacySettings.objects.get_or_create(user=user)
    return settings


# ---------- Rate limiting / abuse protection ----------

LOGIN_MAX_ATTEMPTS = 5
LOGIN_LOCKOUT_SECONDS = 15 * 60


def rate_limit(key, limit, window_seconds):
    """Sliding-window limiter. Returns (allowed, retry_after_seconds)."""
    now = time.time()
    hits = [hit for hit in cache.get(key, []) if now - hit < window_seconds]

    if len(hits) >= limit:
        return False, int(window_seconds - (now - hits[0])) + 1

    hits.append(now)
    cache.set(key, hits, timeout=window_seconds)
    return True, 0


def throttle_request(request, scope, limit, window_seconds, identifier=None):
    """Rate limit by authenticated user when available, otherwise by client IP."""
    if identifier is None:
        user = getattr(request, 'user', None)
        if user is not None and user.is_authenticated:
            identifier = f'user:{user.id}'
        else:
            identifier = f'ip:{get_client_ip(request)}'
    return rate_limit(f'ratelimit:{scope}:{identifier}', limit, window_seconds)


def _login_failure_key(identifier):
    return f'login_failures:{identifier.strip().lower()}'


def get_login_lockout(identifier):
    """Remaining lockout seconds for an identifier, or 0 when not locked."""
    now = time.time()
    failures = [
        hit
        for hit in cache.get(_login_failure_key(identifier), [])
        if now - hit < LOGIN_LOCKOUT_SECONDS
    ]
    if len(failures) < LOGIN_MAX_ATTEMPTS:
        return 0
    return int(LOGIN_LOCKOUT_SECONDS - (now - failures[0])) + 1


def record_login_failure(identifier):
    key = _login_failure_key(identifier)
    now = time.time()
    failures = [hit for hit in cache.get(key, []) if now - hit < LOGIN_LOCKOUT_SECONDS]
    failures.append(now)
    cache.set(key, failures, timeout=LOGIN_LOCKOUT_SECONDS)


def clear_login_failures(identifier):
    cache.delete(_login_failure_key(identifier))


# ---------- Privacy controls ----------

def are_contacts(user_a, user_b):
    """Two users are contacts when they already share a direct conversation."""
    if not user_a or not user_b or user_a.id == user_b.id:
        return False

    from chats.models import Conversation

    return (
        Conversation.objects.filter(type='direct', participants=user_a)
        .filter(participants=user_b)
        .exists()
    )


def privacy_settings_or_default(user):
    """Read-only privacy lookup that never writes a row for the user."""
    return UserPrivacySettings.objects.filter(user=user).first() or UserPrivacySettings(
        user=user
    )


def visibility_allows(owner, viewer, setting_value):
    """Resolve an 'everyone' / 'contacts' / 'nobody' privacy choice."""
    if viewer is not None and owner.id == getattr(viewer, 'id', None):
        return True
    if setting_value == 'everyone':
        return True
    if setting_value == 'nobody':
        return False
    return are_contacts(owner, viewer)


def can_message(sender, recipient):
    """Whether `sender` is allowed to start/continue a chat with `recipient`."""
    prefs = privacy_settings_or_default(recipient)
    return visibility_allows(recipient, sender, prefs.allow_messages_from)


def can_call(caller, callee):
    prefs = privacy_settings_or_default(callee)
    return visibility_allows(callee, caller, prefs.allow_calls_from)


def visible_profile_fields(owner, viewer):
    """Which optional profile fields `viewer` may see on `owner`'s profile."""
    prefs = privacy_settings_or_default(owner)
    return {
        'online_status': visibility_allows(
            owner, viewer, 'everyone' if prefs.show_online_status else 'nobody'
        ),
        'last_seen': visibility_allows(
            owner, viewer, 'everyone' if prefs.show_last_seen else 'nobody'
        ),
        'profile_photo': visibility_allows(
            owner, viewer, 'everyone' if prefs.show_profile_photo else 'nobody'
        ),
    }


# ---------- End-to-end encryption key management ----------

MIN_PREKEY_COUNT = 10


def store_device_keys(device, data):
    """Publish a device's public identity/signed prekey bundle and one-time prekeys."""
    device.registration_id = data['registration_id']
    device.identity_key = data['identity_key']
    device.signed_prekey_id = data['signed_prekey_id']
    device.signed_prekey = data['signed_prekey']
    device.signed_prekey_signature = data['signed_prekey_signature']
    device.keys_updated_at = timezone.now()
    device.save(
        update_fields=[
            'registration_id',
            'identity_key',
            'signed_prekey_id',
            'signed_prekey',
            'signed_prekey_signature',
            'keys_updated_at',
        ]
    )

    prekeys = data.get('one_time_prekeys') or []
    if prekeys:
        DevicePreKey.objects.filter(
            device=device,
            key_id__in=[item['key_id'] for item in prekeys],
        ).delete()
        DevicePreKey.objects.bulk_create(
            [
                DevicePreKey(
                    device=device,
                    key_id=item['key_id'],
                    public_key=item['public_key'],
                )
                for item in prekeys
            ]
        )
    return device


def available_prekey_count(device):
    return DevicePreKey.objects.filter(device=device, consumed_at__isnull=True).count()


def take_device_key_bundle(device):
    """Build a key bundle for a peer, consuming one one-time prekey if available."""
    if not device.supports_e2ee:
        return None

    prekey = (
        DevicePreKey.objects.filter(device=device, consumed_at__isnull=True)
        .order_by('key_id')
        .first()
    )
    if prekey:
        DevicePreKey.objects.filter(pk=prekey.pk, consumed_at__isnull=True).update(
            consumed_at=timezone.now()
        )

    return {
        'device_id': device.device_id,
        'platform': device.platform,
        'registration_id': device.registration_id,
        'identity_key': device.identity_key,
        'signed_prekey_id': device.signed_prekey_id,
        'signed_prekey': device.signed_prekey,
        'signed_prekey_signature': device.signed_prekey_signature,
        'one_time_prekey': (
            {'key_id': prekey.key_id, 'public_key': prekey.public_key} if prekey else None
        ),
    }


def get_user_key_bundles(user):
    """Key bundles for every active E2EE-capable device belonging to a user."""
    devices = UserDevice.objects.filter(user=user, is_active=True).exclude(identity_key='')
    bundles = [take_device_key_bundle(device) for device in devices]
    return [bundle for bundle in bundles if bundle]

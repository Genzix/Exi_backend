from rest_framework import serializers
from django.contrib.auth import get_user_model
from django.db import models
from django.urls import reverse
from django.utils import timezone
from accounts.serializers import PrivacyMaskedProfileMixin, PublicProfileSerializer
from .models import Conversation, ConversationParticipant, Media, Message, Call, Report, Notification

User = get_user_model()

MEDIA_MAX_FILE_SIZES = {
    'image': 10 * 1024 * 1024,
    'video': 100 * 1024 * 1024,
    'document': 25 * 1024 * 1024,
    'audio': 25 * 1024 * 1024,
    'voice': 25 * 1024 * 1024,
}

class ParticipantUserSerializer(PrivacyMaskedProfileMixin, serializers.ModelSerializer):
    live_note = serializers.SerializerMethodField()

    class Meta:
        model = User
        fields = ['exi_id', 'full_name', 'display_name', 'profile_photo', 'is_online', 'last_seen', 'live_note']

    def get_live_note(self, obj):
        from accounts.live_notes import get_live_note
        return get_live_note(obj.id)


class MessagePreviewSerializer(serializers.ModelSerializer):
    class Meta:
        model = Message
        fields = [
            'id', 'content', 'message_type', 'attachment_name',
            'status', 'created_at', 'sender_id'
        ]


class MessageSearchResultSerializer(serializers.ModelSerializer):
    conversation_id = serializers.UUIDField(source='conversation.id', read_only=True)
    sender_name = serializers.CharField(source='sender.display_name', read_only=True)
    sender_exi_id = serializers.CharField(source='sender.exi_id', read_only=True)
    chat_participants = serializers.SerializerMethodField()

    class Meta:
        model = Message
        fields = [
            'id', 'conversation_id', 'content', 'message_type',
            'attachment_name', 'status', 'created_at', 'sender_id',
            'sender_name', 'sender_exi_id', 'chat_participants',
        ]

    def get_chat_participants(self, obj):
        request = self.context.get('request')
        if not request:
            return []
        users = obj.conversation.participants.exclude(id=request.user.id)
        return ParticipantUserSerializer(users, many=True).data


class RecentConversationSerializer(serializers.ModelSerializer):
    conversation_id = serializers.UUIDField(source='conversation.id', read_only=True)
    type = serializers.CharField(source='conversation.type', read_only=True)
    updated_at = serializers.DateTimeField(source='conversation.updated_at', read_only=True)
    disappearing_duration = serializers.IntegerField(source='conversation.disappearing_duration', read_only=True, allow_null=True)
    
    participants = serializers.SerializerMethodField()
    last_message = serializers.SerializerMethodField()
    permissions = serializers.SerializerMethodField()

    class Meta:
        model = ConversationParticipant
        fields = [
            'conversation_id', 
            'type', 
            'is_pinned', 
            'is_archived', 
            'is_muted',
            'unread_count', 
            'updated_at', 
            'disappearing_duration',
            'participants', 
            'last_message',
            'permissions',
        ]

    def get_permissions(self, obj):
        from .chat_permissions import get_chat_permissions_display
        cid = getattr(obj, 'conversation_id', getattr(obj, 'id', None))
        uid = getattr(obj, 'user_id', None)
        return get_chat_permissions_display(cid, uid)

    def get_participants(self, obj):
        # In a real app, you'd want to use prefetch_related and avoid queries in loop.
        # This gets all participants EXCEPT the current user
        users = obj.conversation.participants.exclude(id=obj.user_id)
        return ParticipantUserSerializer(users, many=True).data

    def get_last_message(self, obj):
        from django.utils import timezone
        from datetime import timedelta
        qs = obj.conversation.messages.all()
        if obj.cleared_at:
            qs = qs.filter(created_at__gt=obj.cleared_at)
        if obj.conversation.disappearing_duration:
            expired_time = timezone.now() - timedelta(seconds=obj.conversation.disappearing_duration)
            qs = qs.filter(created_at__gte=expired_time)
        msg = qs.order_by('-created_at').first()
        if msg:
            return MessagePreviewSerializer(msg).data
        return None


from .models import Conversation, ConversationParticipant, Message, Media, Call, Report, Notification, MessageReaction, StarredMessage, MessageDraft

class MessageReactionSerializer(serializers.ModelSerializer):
    username = serializers.CharField(source='user.username', read_only=True)

    class Meta:
        model = MessageReaction
        fields = ['user', 'username', 'reaction', 'created_at']


class MessageSerializer(serializers.ModelSerializer):
    download_url = serializers.SerializerMethodField()
    media_id = serializers.UUIDField(source='media.id', read_only=True, allow_null=True)
    reactions = MessageReactionSerializer(many=True, read_only=True)
    is_starred = serializers.SerializerMethodField()
    expires_at = serializers.SerializerMethodField()
    is_disappearing = serializers.SerializerMethodField()
    attachment_data = serializers.SerializerMethodField()

    class Meta:
        model = Message
        fields = [
            'id', 'sender', 'content', 'message_type', 'media_id',
            'attachment_name', 'attachment_mime_type', 'attachment_size',
            'download_url', 'attachment_data', 'status', 'created_at',
            'is_encrypted', 'ciphertext', 'encryption_version', 'sender_device_id',
            'reactions', 'is_starred', 'forwarded_from', 'scheduled_for', 'is_scheduled',
            'expires_at', 'is_disappearing'
        ]

    def get_attachment_data(self, obj):
        import json
        mtype = obj.message_type
        if mtype in ('contact', 'location', 'capsule', 'exi_pop'):
            try:
                data = json.loads(obj.content)
                if isinstance(data, dict):
                    if mtype == 'capsule' and obj.scheduled_for:
                        from django.utils import timezone
                        now = timezone.now()
                        request = self.context.get('request')
                        is_sender = request and request.user.id == obj.sender_id
                        is_unlocked = now >= obj.scheduled_for
                        data['is_unlocked'] = is_unlocked
                        if not is_unlocked and not is_sender:
                            data['secret_content'] = None
                            data['content'] = None
                            data['message'] = None
                    return data
            except (ValueError, TypeError):
                pass
        return None

    def get_download_url(self, obj):
        request = self.context.get('request')
        if obj.media_id and obj.media and obj.media.status == 'ready':
            path = reverse('media-download', kwargs={'media_id': obj.media_id})
            return request.build_absolute_uri(path) if request else path
        if not obj.attachment:
            return None
        path = reverse('message-attachment-download', kwargs={'message_id': obj.id})
        return request.build_absolute_uri(path) if request else path

    def get_is_starred(self, obj):
        request = self.context.get('request')
        if request and request.user.is_authenticated:
            return StarredMessage.objects.filter(message=obj, user=request.user).exists()
        return False

    def get_expires_at(self, obj):
        conv = getattr(obj, 'conversation', None)
        if conv and conv.disappearing_duration and obj.created_at:
            from datetime import timedelta
            return obj.created_at + timedelta(seconds=conv.disappearing_duration)
        return None

    def get_is_disappearing(self, obj):
        conv = getattr(obj, 'conversation', None)
        return bool(conv and conv.disappearing_duration)


class SendMessageSerializer(serializers.Serializer):
    """Accepts either a plaintext message or a client-encrypted payload."""

    content = serializers.CharField(required=False, allow_blank=True, max_length=10000)
    ciphertext = serializers.CharField(required=False, allow_blank=True, max_length=65535)
    encryption_version = serializers.IntegerField(required=False, min_value=1)
    sender_device_id = serializers.CharField(required=False, allow_blank=True, max_length=64)
    forwarded_from_id = serializers.UUIDField(required=False, allow_null=True)
    scheduled_for = serializers.DateTimeField(required=False, allow_null=True)

    def validate(self, data):
        content = (data.get('content') or '').strip()
        ciphertext = (data.get('ciphertext') or '').strip()

        if not content and not ciphertext and not data.get('forwarded_from_id'):
            raise serializers.ValidationError(
                {'content': 'Provide content, ciphertext, or a forwarded message ID.'}
            )

        if ciphertext:
            if not (data.get('sender_device_id') or '').strip():
                raise serializers.ValidationError(
                    {'sender_device_id': 'Required for encrypted messages.'}
                )
            # The server must never retain plaintext for an encrypted message.
            data['content'] = ''
            data['ciphertext'] = ciphertext
            data['is_encrypted'] = True
            data['encryption_version'] = data.get('encryption_version') or 1
        else:
            data['content'] = content
            data['ciphertext'] = ''
            data['is_encrypted'] = False
            data['encryption_version'] = 0
            data['sender_device_id'] = ''

        return data


class MediaSerializer(serializers.ModelSerializer):
    download_url = serializers.SerializerMethodField()

    class Meta:
        model = Media
        fields = [
            'id', 'media_type', 'file_name', 'mime_type', 'file_size',
            'status', 'download_url', 'created_at', 'completed_at',
        ]
        read_only_fields = fields

    def get_download_url(self, obj):
        if obj.status != 'ready' or not obj.file:
            return None
        path = reverse('media-download', kwargs={'media_id': obj.id})
        request = self.context.get('request')
        return request.build_absolute_uri(path) if request else path


class MediaUploadUrlSerializer(serializers.Serializer):
    media_type = serializers.ChoiceField(
        choices=['image', 'video', 'document', 'audio', 'voice']
    )
    file_name = serializers.CharField(max_length=255)
    mime_type = serializers.CharField(max_length=100)
    file_size = serializers.IntegerField(min_value=1)

    def validate(self, data):
        media_type = data['media_type']
        if data['file_size'] > MEDIA_MAX_FILE_SIZES[media_type]:
            limit_mb = MEDIA_MAX_FILE_SIZES[media_type] // (1024 * 1024)
            raise serializers.ValidationError(
                {'file_size': f'{media_type.title()} files must be {limit_mb} MB or smaller.'}
            )
        return data


class MediaCompleteSerializer(serializers.Serializer):
    media_id = serializers.UUIDField()
    chat_id = serializers.UUIDField(required=False)
    caption = serializers.CharField(required=False, allow_blank=True, max_length=2000)

    def validate(self, data):
        request = self.context['request']
        try:
            media = Media.objects.get(
                id=data['media_id'],
                owner=request.user,
                status='pending',
            )
        except Media.DoesNotExist:
            raise serializers.ValidationError({'media_id': 'Media not found or already completed.'})

        if media.expires_at < timezone.now():
            media.status = 'failed'
            media.save(update_fields=['status'])
            raise serializers.ValidationError({'media_id': 'Upload has expired.'})

        chat_id = data.get('chat_id')
        if chat_id:
            if not ConversationParticipant.objects.filter(
                user=request.user,
                conversation_id=chat_id,
                deleted_at__isnull=True,
            ).exists():
                raise serializers.ValidationError({'chat_id': 'Not a participant in this chat.'})

        data['media'] = media
        return data


class MediaMessageUploadSerializer(serializers.Serializer):
    ALLOWED_MIME_PREFIXES = {
        'image': ('image/',),
        'video': ('video/',),
        'audio': ('audio/',),
        'voice': ('audio/',),
    }
    DOCUMENT_MIME_TYPES = {
        'application/pdf',
        'application/msword',
        'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
        'application/vnd.ms-excel',
        'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
        'application/vnd.ms-powerpoint',
        'application/vnd.openxmlformats-officedocument.presentationml.presentation',
        'text/plain',
        'text/csv',
        'application/zip',
    }
    MAX_FILE_SIZES = MEDIA_MAX_FILE_SIZES

    media_type = serializers.ChoiceField(
        choices=['image', 'video', 'document', 'audio', 'voice']
    )
    file = serializers.FileField()
    caption = serializers.CharField(required=False, allow_blank=True, max_length=2000)

    def validate(self, data):
        uploaded_file = data['file']
        media_type = data['media_type']
        mime_type = (uploaded_file.content_type or '').lower()

        if uploaded_file.size > self.MAX_FILE_SIZES[media_type]:
            limit_mb = self.MAX_FILE_SIZES[media_type] // (1024 * 1024)
            raise serializers.ValidationError(
                {'file': f'{media_type.title()} files must be {limit_mb} MB or smaller.'}
            )

        if media_type == 'document':
            valid_type = mime_type in self.DOCUMENT_MIME_TYPES
        else:
            valid_type = mime_type.startswith(self.ALLOWED_MIME_PREFIXES[media_type])
        if not valid_type:
            raise serializers.ValidationError(
                {'file': f'The uploaded file is not a valid {media_type} file.'}
            )
        return data


class ContactAttachmentSerializer(serializers.Serializer):
    """Attachment: Share contact card data (matching Contact Card & Bottom Sheet)."""
    name = serializers.CharField(max_length=150, required=True)
    phone = serializers.CharField(max_length=30, required=False, allow_blank=True, default="")
    phone_type = serializers.CharField(max_length=50, required=False, allow_blank=True, default="Mobile")
    country = serializers.CharField(max_length=50, required=False, allow_blank=True, default="India")
    role = serializers.CharField(max_length=150, required=False, allow_blank=True, default="")
    exi_id = serializers.CharField(max_length=32, required=False, allow_blank=True, default="")
    email = serializers.EmailField(required=False, allow_blank=True, default="")
    organization = serializers.CharField(max_length=150, required=False, allow_blank=True, default="")
    avatar_url = serializers.CharField(required=False, allow_blank=True, default="")
    is_verified = serializers.BooleanField(required=False, default=True)
    is_on_exi = serializers.BooleanField(required=False, default=True)
    notes = serializers.CharField(max_length=500, required=False, allow_blank=True, default="")
    vcard_raw = serializers.CharField(required=False, allow_blank=True, default="")

    def validate(self, data):
        if not data.get("name", "").strip():
            raise serializers.ValidationError({"name": "Contact name is required."})
        if not any([data.get("phone"), data.get("exi_id"), data.get("email")]):
            raise serializers.ValidationError("At least one contact method (phone, exi_id, or email) is required.")
        return data


class LocationAttachmentSerializer(serializers.Serializer):
    """Attachment: Share GPS location coordinates."""
    latitude = serializers.FloatField(required=True)
    longitude = serializers.FloatField(required=True)
    name = serializers.CharField(max_length=200, required=False, allow_blank=True, default="")
    address = serializers.CharField(max_length=500, required=False, allow_blank=True, default="")
    is_live = serializers.BooleanField(required=False, default=False)
    live_duration_minutes = serializers.IntegerField(required=False, default=0, min_value=0, max_value=1440)

    def validate(self, data):
        lat = data["latitude"]
        lng = data["longitude"]
        if not (-90.0 <= lat <= 90.0):
            raise serializers.ValidationError({"latitude": "Latitude must be between -90 and 90."})
        if not (-180.0 <= lng <= 180.0):
            raise serializers.ValidationError({"longitude": "Longitude must be between -180 and 180."})
        return data


class TimeCapsuleAttachmentSerializer(serializers.Serializer):
    """Attachment: Time Capsule scheduled message with lock/unlock rules."""
    content = serializers.CharField(max_length=5000, required=True)
    unlock_at = serializers.DateTimeField(required=True)
    title = serializers.CharField(max_length=150, required=False, allow_blank=True, default="")
    lock_hint = serializers.CharField(max_length=300, required=False, allow_blank=True, default="")
    theme = serializers.CharField(max_length=50, required=False, default="golden")

    def validate_unlock_at(self, value):
        from django.utils import timezone
        if value <= timezone.now():
            raise serializers.ValidationError("Time Capsule unlock date must be in the future.")
        return value


class ExiPopAttachmentSerializer(serializers.Serializer):
    """Attachment: EXI Pop interactive ephemeral surprise."""
    POP_TYPES = (
        ("confetti", "Confetti Burst"),
        ("buzz", "Nudge & Buzz"),
        ("scratch_card", "Scratch Card Reveal"),
        ("flash", "Flash Pop"),
        ("fireworks", "Fireworks Display"),
        ("heartbeat", "Heartbeat Pulse"),
        ("gift_box", "Gift Box Open"),
    )
    pop_type = serializers.ChoiceField(choices=POP_TYPES, default="confetti")
    content = serializers.CharField(max_length=1000, required=False, allow_blank=True, default="")
    effect = serializers.CharField(max_length=50, required=False, default="burst")
    sound = serializers.CharField(max_length=50, required=False, default="pop")
    auto_dismiss_seconds = serializers.IntegerField(required=False, default=5, min_value=0, max_value=60)
    interactive_data = serializers.DictField(required=False, default=dict)


class UnifiedAttachmentSerializer(serializers.Serializer):
    """
    Unified attachment intake serializer for the 7 Attachment Menu actions:
    Camera, Photos, Documents, Contacts, Location, Time Capsule, EXI Pop.
    """
    ATTACHMENT_TYPES = (
        ("camera", "Camera"),
        ("photos", "Photos"),
        ("documents", "Documents"),
        ("contacts", "Contacts"),
        ("contact", "Contact Card"),
        ("location", "Location"),
        ("time_capsule", "Time Capsule"),
        ("capsule", "Time Capsule"),
        ("exi_pop", "EXI Pop"),
        ("pop", "EXI Pop"),
    )
    attachment_type = serializers.ChoiceField(choices=ATTACHMENT_TYPES)
    # Media inputs (Camera, Photos, Documents)
    file = serializers.FileField(required=False, allow_null=True)
    caption = serializers.CharField(required=False, allow_blank=True, max_length=2000, default="")
    media_type = serializers.CharField(required=False, allow_blank=True, default="")
    # Structured inputs
    contact = ContactAttachmentSerializer(required=False)
    location = LocationAttachmentSerializer(required=False)
    time_capsule = TimeCapsuleAttachmentSerializer(required=False)
    exi_pop = ExiPopAttachmentSerializer(required=False)


class StartConversationSerializer(serializers.Serializer):
    user_id = serializers.IntegerField(required=False)
    exi_id = serializers.CharField(required=False)

    def validate(self, data):
        user_id = data.get("user_id")
        exi_id = data.get("exi_id")
        if not user_id and not exi_id:
            raise serializers.ValidationError("Provide user_id or exi_id.")
        if user_id and exi_id:
            raise serializers.ValidationError("Provide only one of user_id or exi_id.")

        try:
            if user_id:
                other = User.objects.get(id=user_id)
            else:
                other = User.objects.get(exi_id=exi_id)
        except User.DoesNotExist:
            raise serializers.ValidationError("User not found.")

        data["other_user"] = other
        return data

class ConversationDetailSerializer(RecentConversationSerializer):
    messages = serializers.SerializerMethodField()
    
    class Meta(RecentConversationSerializer.Meta):
        fields = RecentConversationSerializer.Meta.fields + ['messages']
        
    def get_messages(self, obj):
        messages = obj.conversation.messages.all()[:50]
        return MessageSerializer(messages, many=True).data


class InitiateCallSerializer(serializers.Serializer):
    chat_id = serializers.UUIDField()
    call_type = serializers.ChoiceField(choices=['voice', 'video'])

    def validate(self, data):
        request = self.context['request']
        chat_id = data['chat_id']

        try:
            participant = ConversationParticipant.objects.select_related('conversation').get(
                user=request.user,
                conversation_id=chat_id,
                deleted_at__isnull=True,
            )
        except ConversationParticipant.DoesNotExist:
            raise serializers.ValidationError({'chat_id': 'Conversation not found.'})

        if participant.conversation.type != 'direct':
            raise serializers.ValidationError({'chat_id': 'Calls are only supported in direct chats.'})

        callee = participant.conversation.participants.exclude(id=request.user.id).first()
        if not callee:
            raise serializers.ValidationError({'chat_id': 'No recipient found for this chat.'})

        active_call = Call.objects.filter(
            status__in=['ringing', 'active'],
        ).filter(
            models.Q(caller=request.user) | models.Q(callee=request.user)
        ).exists()
        if active_call:
            raise serializers.ValidationError('You already have an active or ringing call.')

        # Check Callee Call Waiting setting:
        from accounts.user_preferences import get_section_preferences
        callee_call_prefs = get_section_preferences(callee.id, "calls") or {}
        call_waiting_enabled = callee_call_prefs.get("call_waiting", True)

        callee_in_call = Call.objects.filter(
            status__in=['ringing', 'active'],
        ).filter(
            models.Q(caller=callee) | models.Q(callee=callee)
        ).exists()

        if callee_in_call and not call_waiting_enabled:
            raise serializers.ValidationError({'chat_id': 'User is currently on another call.'})

        from accounts.utils import can_call
        from .utils import is_blocked
        from .chat_permissions import check_chat_call_permission

        call_allowed, call_err = check_chat_call_permission(participant.conversation_id, request.user.id, callee.id)
        if not call_allowed:
            raise serializers.ValidationError({'chat_id': call_err})

        if is_blocked(request.user, callee):
            raise serializers.ValidationError({'chat_id': 'Cannot call this user.'})
        if not can_call(request.user, callee):
            raise serializers.ValidationError(
                {'chat_id': 'This user does not accept calls from you.'}
            )

        data['conversation'] = participant.conversation
        data['callee'] = callee
        return data


class CallSignalSerializer(serializers.Serializer):
    signal_type = serializers.ChoiceField(choices=['offer', 'answer', 'ice_candidate'])
    sdp = serializers.JSONField(required=False)
    candidate = serializers.JSONField(required=False)

    def validate(self, data):
        signal_type = data['signal_type']
        if signal_type in ('offer', 'answer') and not data.get('sdp'):
            raise serializers.ValidationError({'sdp': 'SDP is required for offer/answer.'})
        if signal_type == 'ice_candidate' and not data.get('candidate'):
            raise serializers.ValidationError({'candidate': 'ICE candidate is required.'})
        return data


class CallSerializer(serializers.ModelSerializer):
    caller = PublicProfileSerializer(read_only=True)
    callee = PublicProfileSerializer(read_only=True)
    conversation_id = serializers.UUIDField(source='conversation.id', read_only=True)
    signaling_url = serializers.SerializerMethodField()
    is_silenced = serializers.SerializerMethodField()
    video_quality = serializers.SerializerMethodField()
    low_data_mode = serializers.SerializerMethodField()

    class Meta:
        model = Call
        fields = [
            'id', 'conversation_id', 'call_type', 'status',
            'caller', 'callee', 'started_at', 'answered_at',
            'ended_at', 'duration_seconds', 'signaling_url',
            'is_silenced', 'video_quality', 'low_data_mode',
        ]
        read_only_fields = fields

    def get_signaling_url(self, obj):
        if obj.is_terminal:
            return None
        request = self.context.get('request')
        if not request:
            return f'/ws/calls/{obj.id}/'
        ws_scheme = 'wss' if request.is_secure() else 'ws'
        host = request.get_host()
        return f'{ws_scheme}://{host}/ws/calls/{obj.id}/'

    def get_is_silenced(self, obj):
        from accounts.user_preferences import get_section_preferences
        from accounts.utils import are_contacts
        callee_call_prefs = get_section_preferences(obj.callee_id, "calls") or {}
        if callee_call_prefs.get("silence_unknown_callers", False):
            return not are_contacts(obj.caller, obj.callee)
        return False

    def get_video_quality(self, obj):
        from accounts.user_preferences import get_section_preferences
        callee_call_prefs = get_section_preferences(obj.callee_id, "calls") or {}
        return callee_call_prefs.get("video_quality", "auto")

    def get_low_data_mode(self, obj):
        from accounts.user_preferences import get_section_preferences
        callee_call_prefs = get_section_preferences(obj.callee_id, "calls") or {}
        return bool(callee_call_prefs.get("low_data_mode", False))


class ReportSerializer(serializers.ModelSerializer):
    reporter_id = serializers.UUIDField(source='reporter.id', read_only=True)
    
    class Meta:
        model = Report
        fields = [
            'id', 'reporter_id', 'reported_user', 'conversation', 
            'reason', 'notes', 'status', 'created_at'
        ]
        read_only_fields = ['id', 'status', 'created_at']


class NotificationSerializer(serializers.ModelSerializer):
    sender = ParticipantUserSerializer(read_only=True)
    sender_id = serializers.IntegerField(source='sender.id', read_only=True, allow_null=True)
    conversation_id = serializers.UUIDField(source='conversation.id', read_only=True, allow_null=True)
    message_id = serializers.UUIDField(source='message.id', read_only=True, allow_null=True)

    class Meta:
        model = Notification
        fields = [
            'id',
            'notification_type',
            'title',
            'body',
            'sender',
            'sender_id',
            'conversation_id',
            'message_id',
            'data',
            'is_read',
            'created_at',
        ]
        read_only_fields = fields


class StarredMessageSerializer(serializers.ModelSerializer):
    message = MessageSerializer()

    class Meta:
        model = StarredMessage
        fields = ['id', 'message', 'created_at']


class MessageDraftSerializer(serializers.ModelSerializer):
    class Meta:
        model = MessageDraft
        fields = [
            'id', 'conversation', 'content', 'is_encrypted', 'ciphertext', 
            'encryption_version', 'sender_device_id', 'created_at', 'updated_at'
        ]
        read_only_fields = ['id', 'created_at', 'updated_at']

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
    class Meta:
        model = User
        fields = ['exi_id', 'full_name', 'display_name', 'profile_photo', 'is_online', 'last_seen']


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
    
    participants = serializers.SerializerMethodField()
    last_message = serializers.SerializerMethodField()

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
            'participants', 
            'last_message'
        ]

    def get_participants(self, obj):
        # In a real app, you'd want to use prefetch_related and avoid queries in loop.
        # This gets all participants EXCEPT the current user
        users = obj.conversation.participants.exclude(id=obj.user_id)
        return ParticipantUserSerializer(users, many=True).data

    def get_last_message(self, obj):
        msg = obj.conversation.messages.order_by('-created_at').first()
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

    class Meta:
        model = Message
        fields = [
            'id', 'sender', 'content', 'message_type', 'media_id',
            'attachment_name', 'attachment_mime_type', 'attachment_size',
            'download_url', 'status', 'created_at',
            'is_encrypted', 'ciphertext', 'encryption_version', 'sender_device_id',
            'reactions', 'is_starred', 'forwarded_from', 'scheduled_for', 'is_scheduled'
        ]

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

        return data


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

        from accounts.utils import can_call
        from .utils import is_blocked

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

    class Meta:
        model = Call
        fields = [
            'id', 'conversation_id', 'call_type', 'status',
            'caller', 'callee', 'started_at', 'answered_at',
            'ended_at', 'duration_seconds', 'signaling_url',
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

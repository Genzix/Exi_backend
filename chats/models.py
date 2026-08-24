import uuid
from django.db import models
from django.conf import settings

class Conversation(models.Model):
    CONVERSATION_TYPES = (
        ('direct', 'Direct Message'),
        ('group', 'Group Chat'),
    )
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    type = models.CharField(max_length=10, choices=CONVERSATION_TYPES, default='direct')
    participants = models.ManyToManyField(settings.AUTH_USER_MODEL, through='ConversationParticipant', related_name='conversations')
    disappearing_duration = models.PositiveIntegerField(null=True, blank=True, help_text="Duration in seconds")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"Conversation {self.id} ({self.type})"


class ConversationParticipant(models.Model):
    conversation = models.ForeignKey(Conversation, on_delete=models.CASCADE)
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)
    is_pinned = models.BooleanField(default=False)
    is_archived = models.BooleanField(default=False)
    is_muted = models.BooleanField(default=False)
    unread_count = models.IntegerField(default=0)
    cleared_at = models.DateTimeField(null=True, blank=True)
    deleted_at = models.DateTimeField(null=True, blank=True)
    joined_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = ('conversation', 'user')

    def __str__(self):
        return f"{self.user.username} in {self.conversation.id}"


class Message(models.Model):
    STATUS_CHOICES = (
        ('sent', 'Sent'),
        ('delivered', 'Delivered'),
        ('read', 'Read'),
    )
    MESSAGE_TYPES = (
        ('text', 'Text'),
        ('image', 'Image'),
        ('video', 'Video'),
        ('document', 'Document'),
        ('audio', 'Audio'),
        ('voice', 'Voice Note'),
    )
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    conversation = models.ForeignKey(Conversation, on_delete=models.CASCADE, related_name='messages')
    sender = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='messages_sent')
    content = models.TextField(blank=True)
    message_type = models.CharField(max_length=10, choices=MESSAGE_TYPES, default='text')
    attachment = models.FileField(upload_to='chat_attachments/%Y/%m/%d/', blank=True, null=True)
    attachment_name = models.CharField(max_length=255, blank=True)
    attachment_mime_type = models.CharField(max_length=100, blank=True)
    attachment_size = models.PositiveBigIntegerField(null=True, blank=True)
    status = models.CharField(max_length=10, choices=STATUS_CHOICES, default='sent')
    created_at = models.DateTimeField(auto_now_add=True)

    forwarded_from = models.ForeignKey('self', on_delete=models.SET_NULL, null=True, blank=True, related_name='forwarded_messages')
    scheduled_for = models.DateTimeField(null=True, blank=True)
    is_scheduled = models.BooleanField(default=False)

    # End-to-end encryption. When is_encrypted is set, `content` stays empty and
    # only the client-encrypted payload is stored.
    is_encrypted = models.BooleanField(default=False)
    ciphertext = models.TextField(blank=True)
    encryption_version = models.PositiveSmallIntegerField(default=0)
    sender_device_id = models.CharField(max_length=64, blank=True)

    class Meta:
        ordering = ['-created_at']

    media = models.ForeignKey(
        'Media',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='messages',
    )

    def __str__(self):
        return f"Msg {self.id} by {self.sender.username}"


class Media(models.Model):
    MEDIA_TYPES = (
        ('image', 'Image'),
        ('video', 'Video'),
        ('document', 'Document'),
        ('audio', 'Audio'),
        ('voice', 'Voice Note'),
    )
    STATUS_CHOICES = (
        ('pending', 'Pending Upload'),
        ('ready', 'Ready'),
        ('failed', 'Failed'),
    )

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    owner = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='media_uploads',
    )
    media_type = models.CharField(max_length=10, choices=MEDIA_TYPES)
    file_name = models.CharField(max_length=255)
    mime_type = models.CharField(max_length=100)
    file_size = models.PositiveBigIntegerField()
    file = models.FileField(upload_to='chat_media/%Y/%m/%d/', blank=True, null=True)
    status = models.CharField(max_length=10, choices=STATUS_CHOICES, default='pending')
    upload_token = models.CharField(max_length=64, unique=True)
    expires_at = models.DateTimeField()
    created_at = models.DateTimeField(auto_now_add=True)
    completed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return f"Media {self.id} ({self.media_type})"


class Call(models.Model):
    CALL_TYPES = (
        ('voice', 'Voice'),
        ('video', 'Video'),
    )
    STATUS_CHOICES = (
        ('ringing', 'Ringing'),
        ('active', 'Active'),
        ('ended', 'Ended'),
        ('missed', 'Missed'),
        ('rejected', 'Rejected'),
        ('cancelled', 'Cancelled'),
    )

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    conversation = models.ForeignKey(
        Conversation,
        on_delete=models.CASCADE,
        related_name='calls',
    )
    caller = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='calls_made',
    )
    callee = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='calls_received',
    )
    call_type = models.CharField(max_length=10, choices=CALL_TYPES)
    status = models.CharField(max_length=10, choices=STATUS_CHOICES, default='ringing')
    started_at = models.DateTimeField(auto_now_add=True)
    answered_at = models.DateTimeField(null=True, blank=True)
    ended_at = models.DateTimeField(null=True, blank=True)
    duration_seconds = models.PositiveIntegerField(default=0)

    class Meta:
        ordering = ['-started_at']

    def __str__(self):
        return f"Call {self.id} ({self.call_type}) — {self.status}"

    @property
    def is_terminal(self):
        return self.status in ('ended', 'missed', 'rejected', 'cancelled')

    def other_user(self, user):
        return self.callee if self.caller_id == user.id else self.caller

    def finalize(self, status, user=None):
        from django.utils import timezone

        now = timezone.now()
        self.status = status
        self.ended_at = now
        if self.answered_at:
            self.duration_seconds = int((now - self.answered_at).total_seconds())
        self.save(update_fields=['status', 'ended_at', 'duration_seconds'])


class Report(models.Model):
    REPORT_REASONS = (
        ('spam', 'Spam'),
        ('abuse', 'Abuse/Harassment'),
        ('inappropriate', 'Inappropriate Content'),
        ('other', 'Other'),
    )
    STATUS_CHOICES = (
        ('pending', 'Pending'),
        ('reviewed', 'Reviewed'),
        ('resolved', 'Resolved'),
    )

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    reporter = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='reports_filed')
    reported_user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='reports_received')
    conversation = models.ForeignKey(Conversation, on_delete=models.SET_NULL, null=True, blank=True)
    reason = models.CharField(max_length=20, choices=REPORT_REASONS)
    notes = models.TextField(blank=True)
    status = models.CharField(max_length=10, choices=STATUS_CHOICES, default='pending')
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return f"Report {self.id} by {self.reporter.username}"


class Notification(models.Model):
    NOTIFICATION_TYPES = (
        ('new_message', 'New Message'),
        ('missed_call', 'Missed Call'),
        ('system', 'System'),
    )

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    recipient = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='notifications',
    )
    sender = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='notifications_triggered',
    )
    notification_type = models.CharField(
        max_length=20,
        choices=NOTIFICATION_TYPES,
        default='new_message',
    )
    title = models.CharField(max_length=120)
    body = models.CharField(max_length=255)
    conversation = models.ForeignKey(
        Conversation,
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name='notifications',
    )
    message = models.ForeignKey(
        Message,
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name='notifications',
    )
    data = models.JSONField(default=dict, blank=True)
    is_read = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['recipient', 'is_read', '-created_at']),
        ]

    def __str__(self):
        return f"{self.notification_type} → {self.recipient_id}"


class MessageReaction(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    message = models.ForeignKey(Message, on_delete=models.CASCADE, related_name='reactions')
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='message_reactions')
    reaction = models.CharField(max_length=50)  # The emoji or string representation
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = ('message', 'user', 'reaction')
        ordering = ['created_at']

    def __str__(self):
        return f"{self.user.username} reacted {self.reaction} to Msg {self.message_id}"


class StarredMessage(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    message = models.ForeignKey(Message, on_delete=models.CASCADE, related_name='starred_by')
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='starred_messages')
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = ('message', 'user')
        ordering = ['-created_at']

    def __str__(self):
        return f"{self.user.username} starred Msg {self.message_id}"


class MessageDraft(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    conversation = models.ForeignKey(Conversation, on_delete=models.CASCADE, related_name='drafts')
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='drafts')
    content = models.TextField(blank=True)
    is_encrypted = models.BooleanField(default=False)
    ciphertext = models.TextField(blank=True)
    encryption_version = models.PositiveSmallIntegerField(default=0)
    sender_device_id = models.CharField(max_length=64, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        unique_together = ('conversation', 'user')
        ordering = ['-updated_at']

    def __str__(self):
        return f"Draft by {self.user.username} in Conv {self.conversation_id}"

from django.contrib import admin

from .models import (
    Call,
    Conversation,
    ConversationParticipant,
    Media,
    Message,
    Notification,
    Report,
)


@admin.register(Notification)
class NotificationAdmin(admin.ModelAdmin):
    list_display = (
        'id',
        'recipient',
        'notification_type',
        'title',
        'is_read',
        'created_at',
    )
    list_filter = ('notification_type', 'is_read', 'created_at')
    search_fields = ('title', 'body', 'recipient__username', 'recipient__exi_id')
    raw_id_fields = ('recipient', 'sender', 'conversation', 'message')


@admin.register(Report)
class ReportAdmin(admin.ModelAdmin):
    list_display = (
        'id',
        'reporter',
        'reported_user',
        'reason',
        'status',
        'created_at',
    )
    list_filter = ('status', 'reason', 'created_at')
    search_fields = (
        'notes',
        'reporter__exi_id',
        'reporter__email',
        'reported_user__exi_id',
        'reported_user__email',
    )
    raw_id_fields = ('reporter', 'reported_user', 'conversation')
    readonly_fields = ('created_at',)


@admin.register(Call)
class CallAdmin(admin.ModelAdmin):
    list_display = ('id', 'call_type', 'status', 'caller', 'callee', 'started_at', 'duration_seconds')
    list_filter = ('call_type', 'status')
    raw_id_fields = ('conversation', 'caller', 'callee')


@admin.register(Message)
class MessageAdmin(admin.ModelAdmin):
    list_display = ('id', 'sender', 'message_type', 'status', 'is_encrypted', 'created_at')
    list_filter = ('message_type', 'status', 'is_encrypted')
    search_fields = ('content', 'sender__exi_id')
    raw_id_fields = ('conversation', 'sender', 'media')


admin.site.register(Conversation)
admin.site.register(ConversationParticipant)
admin.site.register(Media)

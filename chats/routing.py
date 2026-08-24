from django.urls import path

from . import consumers

websocket_urlpatterns = [
    path('ws/chat/<uuid:chat_id>/', consumers.ChatConsumer.as_asgi()),
    path('ws/calls/', consumers.UserCallConsumer.as_asgi()),
    path('ws/calls/<uuid:call_id>/', consumers.CallSignalingConsumer.as_asgi()),
]

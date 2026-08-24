import os
import django

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'exi_chat_backend.settings')
django.setup()

from django.core.asgi import get_asgi_application
from channels.routing import ProtocolTypeRouter, URLRouter
from chats.middleware import JWTAuthMiddleware
import chats.routing

application = ProtocolTypeRouter({
    "http": get_asgi_application(),
    "websocket": JWTAuthMiddleware(
        URLRouter(
            chats.routing.websocket_urlpatterns
        )
    ),
})

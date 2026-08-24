from urllib.parse import parse_qs

from channels.db import database_sync_to_async
from channels.middleware import BaseMiddleware
from django.contrib.auth import get_user_model
from django.contrib.auth.models import AnonymousUser
from rest_framework_simplejwt.exceptions import TokenError
from rest_framework_simplejwt.tokens import AccessToken

User = get_user_model()


@database_sync_to_async
def get_user(user_id, token_version):
    """Resolve the socket user, rejecting disabled accounts and revoked tokens."""
    try:
        user = User.objects.get(id=user_id)
    except User.DoesNotExist:
        return AnonymousUser()

    if not user.is_active or user.token_version != token_version:
        return AnonymousUser()
    return user


def get_token_from_scope(scope):
    query_params = parse_qs(scope.get("query_string", b"").decode("utf-8"))
    token = query_params.get("token", [None])[0]
    if token:
        return token

    headers = dict(scope.get("headers", []))
    auth_header = headers.get(b"authorization", b"").decode("utf-8")
    if auth_header.startswith("Bearer "):
        return auth_header.split(" ", 1)[1]
    return None


class JWTAuthMiddleware(BaseMiddleware):
    async def __call__(self, scope, receive, send):
        scope["user"] = AnonymousUser()
        token = get_token_from_scope(scope)

        if token:
            try:
                # Validates signature, expiry, and token type (access, not refresh).
                access_token = AccessToken(token)
                scope["user"] = await get_user(
                    access_token["user_id"],
                    access_token.get("token_version", 0),
                )
            except (TokenError, KeyError):
                scope["user"] = AnonymousUser()

        return await super().__call__(scope, receive, send)

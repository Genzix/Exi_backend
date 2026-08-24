from rest_framework_simplejwt.authentication import JWTAuthentication
from rest_framework_simplejwt.exceptions import InvalidToken


class VersionedJWTAuthentication(JWTAuthentication):
    """Reject JWTs issued before the user's latest token_version."""

    def get_user(self, validated_token):
        user = super().get_user(validated_token)
        token_version = validated_token.get('token_version', 0)
        if token_version != user.token_version:
            raise InvalidToken('Token has been revoked.')
        return user

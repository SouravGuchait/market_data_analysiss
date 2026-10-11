"""Serializers for registration, login, the current user, and logout."""

from django.contrib.auth import get_user_model, password_validation
from rest_framework import serializers
from rest_framework_simplejwt.serializers import TokenObtainPairSerializer
from rest_framework_simplejwt.tokens import RefreshToken

from .models import Role


class UserSerializer(serializers.ModelSerializer):
    """The shape the API returns for a user. Never includes the password."""

    roles = serializers.SerializerMethodField()

    class Meta:
        model = get_user_model()
        fields = ["id", "email", "first_name", "last_name", "roles"]

    def get_roles(self, obj):
        return [obj.role]


class RegisterSerializer(serializers.ModelSerializer):
    password = serializers.CharField(
        write_only=True,
        validators=[password_validation.validate_password],
    )

    class Meta:
        model = get_user_model()
        fields = ["id", "email", "password", "first_name", "last_name"]

    def validate(self, attrs):
        # Registration always produces an analyst. Silently dropping a role
        # field would look like the API accepted it, and quietly self-assigning
        # one would be privilege escalation, so refuse it explicitly.
        attempted = {
            key: self.initial_data[key]
            for key in ("role", "roles")
            if key in self.initial_data
        }
        if attempted:
            raise serializers.ValidationError(
                {
                    "role": (
                        "Roles cannot be set during registration. New accounts "
                        "always start as analyst; ask an administrator to change "
                        "your role."
                    )
                }
            )
        return attrs

    def create(self, validated_data):
        user = get_user_model().objects.create_user(**validated_data)
        return user


class LoginSerializer(TokenObtainPairSerializer):
    """Authenticates on email and password, and carries the role into the token.

    ``TokenObtainPairSerializer`` reads its username field from
    ``User.USERNAME_FIELD``, which is now ``email``, so the request body uses
    ``email`` rather than ``username`` without any change here.
    """

    @classmethod
    def get_token(cls, user):
        token = super().get_token(user)
        token["email"] = user.email
        token["role"] = user.role
        return token

    def validate(self, attrs):
        data = super().validate(attrs)
        data["user"] = UserSerializer(self.user).data
        return data


class LogoutSerializer(serializers.Serializer):
    """Blacklists the refresh token it is handed."""

    refresh = serializers.CharField()

    def save(self, **kwargs) -> None:
        RefreshToken(self.validated_data["refresh"]).blacklist()

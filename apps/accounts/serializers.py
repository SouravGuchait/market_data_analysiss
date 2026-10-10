"""Serializers for registration, the current-user payload, and login tokens."""

from django.contrib.auth import get_user_model, password_validation
from django.contrib.auth.models import Group
from rest_framework import serializers
from rest_framework_simplejwt.serializers import TokenObtainPairSerializer
from rest_framework_simplejwt.tokens import RefreshToken

from .permissions import ANALYST_GROUP


class UserSerializer(serializers.ModelSerializer):
    roles = serializers.SerializerMethodField()

    class Meta:
        model = get_user_model()
        fields = ["id", "username", "email", "roles"]

    def get_roles(self, obj):
        return sorted(obj.groups.values_list("name", flat=True))


class RegisterSerializer(serializers.ModelSerializer):
    password = serializers.CharField(
        write_only=True,
        validators=[password_validation.validate_password],
    )

    class Meta:
        model = get_user_model()
        fields = ["id", "username", "email", "password"]

    def create(self, validated_data):
        user = get_user_model().objects.create_user(**validated_data)
        user.groups.add(Group.objects.get(name=ANALYST_GROUP))
        return user


class LoginSerializer(TokenObtainPairSerializer):
    """Adds the user profile to the token response and role claims to the token."""

    @classmethod
    def get_token(cls, user):
        token = super().get_token(user)
        token["username"] = user.username
        token["roles"] = sorted(user.groups.values_list("name", flat=True))
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

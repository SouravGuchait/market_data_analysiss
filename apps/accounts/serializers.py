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

    def get_roles(self, obj) -> list[str]:
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


# --------------------------------------------------------------------------- #
# Output shapes for the interactive API docs (/docs/)
#
# Registration and login return a token pair alongside the user, and the usage
# endpoint returns a computed aggregate rather than a model row. None of them has
# a request serializer that also describes the response, so they are named here
# and referenced from the views' ``@extend_schema``. They are documentation
# schemas: the views render the same dictionaries they always did.
# --------------------------------------------------------------------------- #


class RegistrationResponseSerializer(serializers.Serializer):
    """`POST /auth/register/`: the created user and a fresh token pair."""

    user = UserSerializer(read_only=True)
    access = serializers.CharField(read_only=True)
    refresh = serializers.CharField(read_only=True)


class LoginResponseSerializer(serializers.Serializer):
    """`POST /auth/login/`: the token pair plus the user it belongs to."""

    access = serializers.CharField(read_only=True)
    refresh = serializers.CharField(read_only=True)
    user = UserSerializer(read_only=True)


class UsageJobsSerializer(serializers.Serializer):
    total = serializers.IntegerField(read_only=True)
    by_status = serializers.DictField(child=serializers.IntegerField(), read_only=True)
    rows_received = serializers.IntegerField(read_only=True)
    rows_accepted = serializers.IntegerField(read_only=True)
    rows_rejected = serializers.IntegerField(read_only=True)


class UsagePricesSerializer(serializers.Serializer):
    records_imported = serializers.IntegerField(read_only=True)
    instruments_covered = serializers.IntegerField(read_only=True)
    earliest_date = serializers.DateField(read_only=True, allow_null=True)
    latest_date = serializers.DateField(read_only=True, allow_null=True)


class UserUsageSerializer(serializers.Serializer):
    """`GET /auth/usage/` and `GET /me/analytics/`: the caller's activity summary."""

    jobs = UsageJobsSerializer(read_only=True)
    prices = UsagePricesSerializer(read_only=True)

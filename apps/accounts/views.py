"""Authentication endpoints: registration, login, logout, and the current user."""

from rest_framework import status
from rest_framework.generics import CreateAPIView
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework.throttling import ScopedRateThrottle
from rest_framework.views import APIView
from rest_framework_simplejwt.tokens import RefreshToken
from rest_framework_simplejwt.views import TokenObtainPairView

from apps.common.views import CachedReadMixin

from . import services
from .serializers import (
    LogoutSerializer,
    LoginSerializer,
    RegisterSerializer,
    UserSerializer,
)


class RegisterView(CreateAPIView):
    permission_classes = [AllowAny]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "register"
    serializer_class = RegisterSerializer

    def create(self, request, *args, **kwargs):
        # get_serializer() honours get_serializer_class() and passes context.
        serializer = RegisterSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        user = serializer.save()

        refresh = RefreshToken.for_user(user)
        return Response(
            {
                "user": UserSerializer(user).data,
                "access": str(refresh.access_token),
                "refresh": str(refresh),
            },
            status=status.HTTP_201_CREATED,
        )


class LoginView(TokenObtainPairView):
    permission_classes = [AllowAny]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "login"
    serializer_class = LoginSerializer


class LogoutView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request):
        serializer = LogoutSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        serializer.save()
        return Response(status=status.HTTP_205_RESET_CONTENT)


class MeView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        return Response(UserSerializer(request.user).data)


class UserUsageView(CachedReadMixin, APIView):
    """The authenticated user's own activity summary.

    An ``APIView`` rather than a generic view because the response is not a model
    representation: it is an aggregate built by a service function, so there is
    no queryset, serializer, or pagination to configure.

    It is the most expensive single read in the API -- the summary scans every
    price row this user owns -- and it is read far more often than it changes, so
    it is served through the cache, keyed by owner.
    """

    permission_classes = [IsAuthenticated]
    cache_namespace = services.USAGE_CACHE_NAMESPACE

    def get(self, request, *args, **kwargs):
        return self.cached_response(request, *args, **kwargs)

    def get_cache_key(self):
        return services.usage_cache_key(self.request.user)

    def get_cache_ttl(self):
        return services.usage_cache_ttl()

    def produce_payload(self, request, *args, **kwargs):
        return services.get_user_usage_statistics(request.user)

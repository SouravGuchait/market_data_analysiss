"""URL routing for instruments and exchanges."""

from django.urls import path

from .views import ExchangeListAPIView, InstrumentListAPIView, InstrumentRetrieveAPIView


app_name = "instruments"

urlpatterns = [
    path("", InstrumentListAPIView.as_view(), name="list"),
    path("exchanges/", ExchangeListAPIView.as_view(), name="exchange-list"),
    path("<int:pk>/", InstrumentRetrieveAPIView.as_view(), name="detail"),
]

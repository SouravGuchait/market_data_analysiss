"""URL routing for market data."""

from django.urls import path

from .views import PriceHistoryAPIView


app_name = "marketdata"

urlpatterns = [
    path(
        "instruments/<int:instrument_pk>/prices/",
        PriceHistoryAPIView.as_view(),
        name="price-history",
    ),
]

"""Query-parameter filters for instruments."""

import django_filters as filters
from django.db.models import Q

from .models import AssetType, Instrument


class InstrumentFilter(filters.FilterSet):
    """Maps query parameters onto the lookups ``services.list_instruments`` uses.

    ``symbol`` and ``exchange`` are exact and case-insensitive, because a symbol
    is an identifier rather than free text. Partial matching is available
    separately through ``?search=``.
    """

    symbol = filters.CharFilter(field_name="symbol", lookup_expr="iexact")
    exchange = filters.CharFilter(field_name="exchange__code", lookup_expr="iexact")
    asset_type = filters.ChoiceFilter(choices=AssetType.choices)
    is_active = filters.BooleanFilter()
    search = filters.CharFilter(method="filter_search")

    class Meta:
        model = Instrument
        fields = ["symbol", "exchange", "asset_type", "is_active", "search"]

    def filter_search(self, queryset, name, value):
        """Matches a substring of either the symbol or the full name."""
        return queryset.filter(Q(symbol__icontains=value) | Q(name__icontains=value))

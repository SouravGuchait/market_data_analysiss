"""Pagination shared by every list endpoint."""

from rest_framework.pagination import PageNumberPagination


class StandardResultsPagination(PageNumberPagination):
    """Offset pagination with a client-adjustable page size.

    ``page_size`` is the default, ``?page_size=`` lets a client ask for fewer or
    more (up to ``max_page_size``), and ``?page=`` selects the page. Chosen over
    cursor pagination because every list here is bounded and clients need
    "jump to page 3", which cursors cannot do.
    """

    page_size = 25
    page_size_query_param = "page_size"
    max_page_size = 100

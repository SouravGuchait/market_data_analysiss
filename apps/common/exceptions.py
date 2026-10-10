"""One error shape for every API response.

DRF renders validation failures as ``{"field": ["message"]}`` and auth failures
as ``{"detail": "message"}``. Clients should not have to branch on both, so the
handler below rewrites both into a single ``{"error": {...}`` envelope.
"""

from rest_framework.views import exception_handler


STATUS_CODES = {
    400: "bad_request",
    401: "not_authenticated",
    403: "permission_denied",
    404: "not_found",
    405: "method_not_allowed",
    429: "throttled",
}


def market_data_exception_handler(exc, context):
    response = exception_handler(exc, context)
    if response is None:
        return None

    detail = response.data
    if isinstance(detail, list):
        detail = {"detail": detail}

    messages = detail.get("detail")
    if isinstance(messages, list):
        messages = messages[0] if messages else None

    response.data = {
        "error": {
            "code": STATUS_CODES.get(response.status_code, "error"),
            "message": messages or "Request rejected.",
            "fields": {k: v for k, v in detail.items() if k != "detail"},
        }
    }
    return response

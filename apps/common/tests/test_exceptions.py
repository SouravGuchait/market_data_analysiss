from django.test import SimpleTestCase

from rest_framework.exceptions import AuthenticationFailed, ValidationError

from apps.common.exceptions import market_data_exception_handler


class ExceptionHandlerTests(SimpleTestCase):
    def test_authentication_failures_become_a_401_envelope(self):
        response = market_data_exception_handler(
            AuthenticationFailed("No active account found."), {}
        )

        self.assertEqual(response.status_code, 401)
        self.assertEqual(
            response.data,
            {
                "error": {
                    "code": "not_authenticated",
                    "message": "No active account found.",
                    "fields": {},
                }
            },
        )

    def test_validation_failures_expose_their_field_messages(self):
        response = market_data_exception_handler(
            ValidationError({"password": ["This password is too short."]}), {}
        )

        self.assertEqual(response.status_code, 400)
        self.assertEqual(
            response.data,
            {
                "error": {
                    "code": "bad_request",
                    "message": "Request rejected.",
                    "fields": {"password": ["This password is too short."]},
                }
            },
        )

    def test_list_details_are_flattened_into_the_message(self):
        response = market_data_exception_handler(
            AuthenticationFailed("Token is blacklisted"), {}
        )

        self.assertEqual(response.data["error"]["message"], "Token is blacklisted")

    def test_unhandled_exceptions_pass_through_untouched(self):
        self.assertIsNone(market_data_exception_handler(ValueError("boom"), {}))

"""Tests for the audit-log report (administrator only).

The endpoint is the one report that returns stored rows rather than aggregates,
so the suite checks the row shape, the pagination and filters that DRF provides,
and the two access rules: administrators read every actor's events, and nobody
else reads any.
"""

from datetime import datetime, timezone

from django.test import TestCase

from apps.audit import services
from apps.audit.events import AuditAction
from apps.audit.models import AuditEvent
from apps.common.tests.mixins import AuthenticatedAPITestCase
from apps.reports.services import list_audit_events

AUDIT_URL = "/api/v1/reports/audit-events/"


def at(minutes: int):
    return datetime(2024, 3, 12, 0, minutes, tzinfo=timezone.utc)


class AuditLogMixin:
    def make_event(self, user=None, *, action, object_type="IngestionJob", object_id="", request_id="req-1", minutes=0, metadata=None):
        event = AuditEvent.objects.create(
            user=user,
            action=action,
            object_type=object_type,
            object_id=object_id,
            request_id=request_id,
            ip_address="203.0.113.7",
            metadata=metadata,
        )
        # ``created_at`` is ``auto_now_add``, so a chosen time is written by an
        # UPDATE that bypasses the auto field.
        AuditEvent.objects.filter(pk=event.pk).update(created_at=at(minutes))
        event.refresh_from_db()
        return event


class AuditLogServiceTests(AuditLogMixin, TestCase):
    def test_the_queryset_is_newest_first_with_the_actor_joined(self):
        from django.contrib.auth import get_user_model

        alice = get_user_model().objects.create_user(
            email="alice@example.com", password="drf-portfolio-42!"
        )
        old = self.make_event(alice, action="a.old", minutes=0)
        new = self.make_event(alice, action="a.new", minutes=30)

        rows = list(list_audit_events())

        self.assertEqual([row.pk for row in rows], [new.pk, old.pk])

    def test_a_page_does_not_query_once_per_row(self):
        from django.contrib.auth import get_user_model

        alice = get_user_model().objects.create_user(
            email="alice@example.com", password="drf-portfolio-42!"
        )
        for minute in range(10):
            self.make_event(alice, action="a.event", minutes=minute)

        with self.assertNumQueries(1):
            emails = [row.user.email for row in services.audit_event_queryset()[:5]]

        self.assertEqual(len(emails), 5)


class AuditLogAPITests(AuditLogMixin, AuthenticatedAPITestCase):
    def setUp(self):
        super().setUp()
        self.admin = self.create_administrator("root@example.com")
        self.alice = self.create_user("alice@example.com")
        self.bob = self.create_user("bob@example.com")

        self.first = self.make_event(
            self.alice, action=AuditAction.JOB_CREATED, object_id="1", request_id="req-1", minutes=0
        )
        self.second = self.make_event(
            self.bob,
            action=AuditAction.UPLOAD_LOADED,
            object_id="2",
            request_id="req-2",
            minutes=10,
            metadata={"status": "completed", "rows_accepted": 5},
        )
        self.third = self.make_event(
            None, action="auth.denied", object_type="", object_id="", request_id="req-3", minutes=20
        )

    def get(self, **params):
        # ``data=`` is the query string; bare keyword arguments would land in the
        # WSGI environ instead and be silently ignored by the filter backend.
        return self.get_as(self.admin, AUDIT_URL, data=params or None)

    # --- shape ----------------------------------------------------------- #

    def test_an_administrator_reads_the_log_newest_first(self):
        response = self.get()

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["count"], 3)
        self.assertEqual(
            [row["id"] for row in response.data["results"]],
            [self.third.pk, self.second.pk, self.first.pk],
        )

    def test_a_row_carries_its_actor_target_and_metadata(self):
        row = self.get().data["results"][1]  # the second event, by bob

        self.assertEqual(row["user_email"], "bob@example.com")
        self.assertEqual(row["action"], "ingestion.upload_loaded")
        self.assertEqual(row["object_type"], "IngestionJob")
        self.assertEqual(row["object_id"], "2")
        self.assertEqual(row["request_id"], "req-2")
        self.assertEqual(row["metadata"], {"status": "completed", "rows_accepted": 5})
        self.assertEqual(row["created_at"], "2024-03-12T00:10:00Z")

    def test_an_event_whose_actor_is_gone_still_reports(self):
        # ``user`` is SET_NULL, so the row outlives the account; the email is null
        # and the rest of the row is intact.
        row = self.get().data["results"][0]

        self.assertIsNone(row["user_email"])
        self.assertEqual(row["action"], "auth.denied")

    # --- pagination ------------------------------------------------------ #

    def test_the_log_is_paginated(self):
        for minute in range(30, 60):
            self.make_event(self.alice, action="a.event", minutes=minute)

        response = self.get()

        self.assertEqual(response.data["count"], 33)
        self.assertEqual(len(response.data["results"]), 25)
        self.assertIsNotNone(response.data["next"])
        self.assertIsNone(response.data["previous"])

    def test_a_later_page_carries_the_remainder(self):
        for minute in range(30, 60):
            self.make_event(self.alice, action="a.event", minutes=minute)

        response = self.get(page=2)

        self.assertEqual(len(response.data["results"]), 8)
        self.assertIsNone(response.data["next"])
        self.assertIsNotNone(response.data["previous"])

    def test_page_size_is_honoured(self):
        response = self.get(page_size=2)

        self.assertEqual(len(response.data["results"]), 2)

    # --- filters --------------------------------------------------------- #

    def test_the_log_filters_by_action(self):
        response = self.get(action=AuditAction.JOB_CREATED)

        self.assertEqual(response.data["count"], 1)
        self.assertEqual(response.data["results"][0]["object_id"], "1")

    def test_the_log_filters_by_object(self):
        response = self.get(object_type="IngestionJob", object_id="2")

        self.assertEqual(response.data["count"], 1)
        self.assertEqual(response.data["results"][0]["action"], "ingestion.upload_loaded")

    def test_the_log_filters_by_actor_email(self):
        response = self.get(user="ALICE@example.com")  # case-insensitive

        self.assertEqual(response.data["count"], 1)
        self.assertEqual(response.data["results"][0]["request_id"], "req-1")

    def test_the_log_filters_by_request_id(self):
        response = self.get(request_id="req-3")

        self.assertEqual(response.data["count"], 1)

    def test_the_log_filters_by_date_range(self):
        response = self.get(since="2024-03-12T00:05:00Z", until="2024-03-12T00:15:00Z")

        self.assertEqual(response.data["count"], 1)
        self.assertEqual(response.data["results"][0]["request_id"], "req-2")

    def test_the_log_can_be_searched(self):
        response = self.get(search="upload")

        self.assertEqual(response.data["count"], 1)
        self.assertEqual(response.data["results"][0]["action"], "ingestion.upload_loaded")

    def test_the_log_can_be_ordered_oldest_first(self):
        response = self.get(ordering="created_at")

        self.assertEqual(response.data["results"][0]["id"], self.first.pk)

    def test_an_empty_log_is_an_empty_page_not_a_404(self):
        AuditEvent.objects.all().delete()

        response = self.get()

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["count"], 0)
        self.assertEqual(response.data["results"], [])

    # --- access ---------------------------------------------------------- #

    def test_an_analyst_is_forbidden(self):
        self.assertEqual(self.get_as(self.user, AUDIT_URL).status_code, 403)

    def test_the_log_requires_authentication(self):
        self.assertEqual(self.client.get(AUDIT_URL).status_code, 401)

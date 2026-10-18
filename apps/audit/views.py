"""The audit app exposes no HTTP endpoints of its own.

Writing events is a service (:func:`apps.audit.services.record_event`) that other
apps call; reading them is a report (``GET /api/v1/reports/audit-events/``) that
lives in ``apps.reports`` with the other reports. Keeping the app view-free is the
same separation the reporting app keeps in the other direction: the audit domain
owns the rows, and the reporting surface owns the read.
"""

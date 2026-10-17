"""The service layer: job lifecycle, report shape, and the upload path."""

import pytest
from django.core.exceptions import ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import override_settings

from apps.ingestion.etl.errors import ROW_RULES
from apps.ingestion.models import IngestionJob, JobStatus, SourceType
from apps.ingestion.services import (
    create_job,
    get_job_for_user,
    infer_source_type,
    ingest_file,
    ingest_upload,
    list_jobs_for_user,
    report_for,
)
from apps.marketdata.models import PriceRecord

from .helpers import make_instrument, make_user, write_csv

CLEAN_ROW = ["AAPL", "2024-03-12", "100.00", "102.00", "99.00", "101.00", "101.00", 1000, "clean"]


def test_infer_source_type_maps_every_supported_extension():
    assert infer_source_type("a.csv") == SourceType.CSV
    assert infer_source_type("a.JSON") == SourceType.JSON
    assert infer_source_type("a.jsonl") == SourceType.JSON
    assert infer_source_type("a.xlsx") == SourceType.XLSX


def test_infer_source_type_rejects_an_unknown_extension():
    with pytest.raises(ValueError):
        infer_source_type("a.parquet")


@pytest.mark.django_db
def test_create_job_starts_pending_and_owned():
    user = make_user()

    job = create_job(created_by=user, source_type=SourceType.CSV)

    assert job.status == JobStatus.PENDING
    assert job.created_by_id == user.pk


@pytest.mark.django_db
def test_create_job_rejects_an_unknown_source_type():
    with pytest.raises(ValidationError):
        create_job(created_by=make_user(), source_type="parquet")


@pytest.mark.django_db
def test_list_jobs_for_user_only_returns_that_users_jobs():
    owner = make_user("owner@example.test")
    other = make_user("other@example.test")
    create_job(created_by=owner, source_type=SourceType.CSV)
    create_job(created_by=other, source_type=SourceType.CSV)

    assert list_jobs_for_user(user=owner).count() == 1


@pytest.mark.django_db
def test_get_job_for_user_refuses_another_users_job():
    owner = make_user("owner@example.test")
    other = make_user("other@example.test")
    job = create_job(created_by=owner, source_type=SourceType.CSV)

    with pytest.raises(IngestionJob.DoesNotExist):
        get_job_for_user(other, job.pk)


@pytest.mark.django_db
def test_report_has_a_rule_table_and_a_failure_sample(tmp_path):
    make_instrument("AAPL")
    user = make_user()
    path = write_csv(tmp_path / "clean.csv", [CLEAN_ROW])

    job = ingest_file(user=user, path=path).job
    report = report_for(job)

    assert report["status"] == JobStatus.SUCCEEDED
    assert report["rows_accepted"] == 1
    assert {rule["rule"] for rule in report["rules"]} == set(ROW_RULES)
    assert report["sample_failures"] == []


@pytest.mark.django_db
def test_ingest_upload_stores_the_file_and_loads_it(tmp_path):
    make_instrument("AAPL")
    user = make_user()
    body = ",".join(str(value) for value in ["symbol", "trading_date", "open", "high", "low", "close", "adjusted_close", "volume", "source"])
    body += "\n" + ",".join(str(value) for value in CLEAN_ROW) + "\n"
    upload = SimpleUploadedFile("clean.csv", body.encode("utf-8"), content_type="text/csv")

    with override_settings(MEDIA_ROOT=tmp_path):
        result = ingest_upload(user=user, uploaded_file=upload)

    assert result.job.status == JobStatus.SUCCEEDED
    assert PriceRecord.objects.count() == 1
    assert result.job.files.count() == 1

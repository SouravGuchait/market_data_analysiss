"""Safe file handling: names, extensions, size limits, and checksums.

These checks run before a file is parsed, so a failure here means the bytes were
never read at all. The traversal and oversize cases are the ones worth pinning:
they are the difference between a bounded ingest and a hostile one.
"""

import hashlib

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import IntegrityError
from django.test import override_settings

from apps.ingestion.etl.errors import UnsafeFileError
from apps.ingestion.etl.storage import (
    assert_within_size,
    checksum_of,
    content_type_for,
    register_local_file,
    safe_filename,
    store_upload,
    validate_extension,
    validate_local_file,
)
from apps.ingestion.models import IngestionJob, SourceType

from .helpers import make_user


def make_job(user):
    return IngestionJob.objects.create(created_by=user, source_type=SourceType.CSV)


# --------------------------------------------------------------------------- #
# Names, extensions, sizes (no database needed)
# --------------------------------------------------------------------------- #

def test_safe_filename_reduces_a_traversal_path_to_its_base_name():
    assert safe_filename("../../etc/passwd") == "passwd"
    assert safe_filename(r"..\..\windows\system32\cmd.exe") == "cmd.exe"


def test_safe_filename_rejects_an_unusable_name():
    with pytest.raises(UnsafeFileError):
        safe_filename("")
    with pytest.raises(UnsafeFileError):
        safe_filename("..")


def test_safe_filename_rejects_forbidden_characters():
    with pytest.raises(UnsafeFileError):
        safe_filename('weird"name.csv')


def test_validate_extension_accepts_the_supported_formats():
    for name in ("a.csv", "a.CSV", "a.json", "a.jsonl", "a.xlsx"):
        assert validate_extension(name)


def test_validate_extension_rejects_anything_else():
    with pytest.raises(UnsafeFileError):
        validate_extension("prices.parquet")


@override_settings(INGESTION_MAX_UPLOAD_BYTES=10)
def test_assert_within_size_raises_over_the_ceiling():
    assert_within_size(10)
    with pytest.raises(UnsafeFileError):
        assert_within_size(11)


def test_checksum_matches_a_plain_sha256(tmp_path):
    path = tmp_path / "a.csv"
    path.write_bytes(b"symbol,trading_date\n")

    assert checksum_of(path) == hashlib.sha256(b"symbol,trading_date\n").hexdigest()


def test_content_type_prefers_the_declared_value():
    assert content_type_for("a.csv", "text/csv") == "text/csv"
    assert content_type_for("a.xlsx").endswith("spreadsheetml.sheet")


def test_validate_local_file_rejects_a_missing_file(tmp_path):
    with pytest.raises(UnsafeFileError):
        validate_local_file(tmp_path / "nope.csv")


def test_validate_local_file_rejects_a_bad_extension(tmp_path):
    path = tmp_path / "a.parquet"
    path.write_text("x", encoding="utf-8")

    with pytest.raises(UnsafeFileError):
        validate_local_file(path)


# --------------------------------------------------------------------------- #
# Registering files (needs the database)
# --------------------------------------------------------------------------- #

@pytest.mark.django_db
def test_register_local_file_records_a_checksum(tmp_path):
    user = make_user()
    job = make_job(user)
    path = tmp_path / "prices.csv"
    path.write_bytes(b"symbol\nAAPL\n")

    stored = register_local_file(job=job, path=path)

    assert stored.checksum == hashlib.sha256(b"symbol\nAAPL\n").hexdigest()
    assert stored.original_filename == "prices.csv"
    assert stored.content_type == "text/csv"


@pytest.mark.django_db
def test_store_upload_stores_the_bytes_and_hashes_them(tmp_path):
    user = make_user()
    job = make_job(user)
    upload = SimpleUploadedFile("prices.csv", b"symbol\nAAPL\n", content_type="text/csv")

    with override_settings(MEDIA_ROOT=tmp_path):
        stored = store_upload(job=job, uploaded_file=upload)

        assert stored.checksum == hashlib.sha256(b"symbol\nAAPL\n").hexdigest()
        assert tmp_path in (tmp_path / stored.storage_path).parents
        assert (tmp_path / stored.storage_path).is_file()


@pytest.mark.django_db
def test_re_uploading_the_same_bytes_to_one_job_is_a_duplicate(tmp_path):
    user = make_user()
    job = make_job(user)

    with override_settings(MEDIA_ROOT=tmp_path):
        store_upload(job=job, uploaded_file=SimpleUploadedFile("a.csv", b"x"))
        with pytest.raises(IntegrityError):
            store_upload(job=job, uploaded_file=SimpleUploadedFile("a.csv", b"x"))


@pytest.mark.django_db
def test_store_upload_refuses_an_unsupported_extension():
    user = make_user()
    job = make_job(user)

    with pytest.raises(UnsafeFileError):
        store_upload(job=job, uploaded_file=SimpleUploadedFile("a.parquet", b"x"))

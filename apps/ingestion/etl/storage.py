"""Safe handling of uploaded and on-disk source files.

Three things are checked before a file is ever read:

* **Name.** Only the base name is trusted, so ``../../etc/passwd`` becomes
  ``passwd`` and cannot escape the storage root. The extension must be one we
  can parse.
* **Size.** A hard byte ceiling stops a hostile or accidental upload from
  exhausting memory or disk. ``django.core.files`` already streams a large
  ``UploadedFile`` to a temp file, but the ceiling is still enforced explicitly.
* **Content.** A checksum is computed while the bytes are written, giving the
  ``IngestionFile`` its idempotency key.

Nothing here parses the file; that is :mod:`apps.ingestion.etl.sources`.
"""

from __future__ import annotations

import hashlib
import mimetypes
from pathlib import Path

from django.conf import settings
from django.core.files.storage import default_storage

from apps.ingestion.models import IngestionFile

from .errors import UnsafeFileError

ALLOWED_EXTENSIONS = {".csv", ".json", ".jsonl", ".xlsx"}

# 25 MiB is comfortable for a daily price file and small enough that a runaway
# upload cannot fill the disk.
DEFAULT_MAX_UPLOAD_BYTES = 25 * 1024 * 1024

# Characters that must never appear in a stored name, on top of the base-name
# reduction: a NUL or a path separator inside a name is always hostile.
_FORBIDDEN = set('<>:"/\\|?*\0')

_CONTENT_TYPES = {
    ".csv": "text/csv",
    ".json": "application/json",
    ".jsonl": "application/x-ndjson",
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
}


def max_upload_bytes() -> int:
    """Return the configured upload ceiling, in bytes."""
    return int(getattr(settings, "INGESTION_MAX_UPLOAD_BYTES", DEFAULT_MAX_UPLOAD_BYTES))


def safe_filename(name: str) -> str:
    """Reduce an arbitrary client-supplied name to a safe base name.

    Contract
    --------
    in : any filename, trusted or not.
    out: a bare file name with no directory component and no forbidden
         characters.

    Raises :class:`UnsafeFileError` when nothing usable remains, because silently
    inventing a name would hide that the input was hostile.
    """
    base = Path(str(name).replace("\\", "/")).name.strip()
    if not base or base in {".", ".."}:
        raise UnsafeFileError(f"unusable file name: {name!r}")
    if any(char in _FORBIDDEN for char in base):
        raise UnsafeFileError(f"file name contains forbidden characters: {name!r}")
    return base


def content_type_for(name: str, declared: str | None = None) -> str:
    """Return a deterministic content type for a known extension.

    The declared value wins when it is meaningful. Otherwise the explicit map is
    used before ``mimetypes``, because ``mimetypes`` reads the host registry on
    Windows and there ``.csv`` maps to ``application/vnd.ms-excel`` — a result
    that would differ between a developer's laptop and a server.
    """
    if declared and declared != "application/octet-stream":
        return declared[:128]
    suffix = Path(name).suffix.lower()
    if suffix in _CONTENT_TYPES:
        return _CONTENT_TYPES[suffix]
    guessed, _ = mimetypes.guess_type(name)
    return guessed or "application/octet-stream"


def validate_extension(name: str) -> str:
    """Return the lower-cased extension, or raise if it is not parseable."""
    suffix = Path(name).suffix.lower()
    if suffix not in ALLOWED_EXTENSIONS:
        allowed = ", ".join(sorted(ALLOWED_EXTENSIONS))
        raise UnsafeFileError(f"unsupported file type {suffix or '(none)'}; allowed: {allowed}")
    return suffix


def assert_within_size(size: int) -> None:
    """Raise :class:`UnsafeFileError` if ``size`` exceeds the ceiling."""
    ceiling = max_upload_bytes()
    if size > ceiling:
        raise UnsafeFileError(
            f"file is {size} bytes, over the {ceiling}-byte limit"
        )


def checksum_of(path: str | Path) -> str:
    """Return the SHA-256 of a file, read in chunks so it never loads whole."""
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def checksum_of_upload(uploaded_file) -> str:
    """Return the SHA-256 of an ``UploadedFile``, left rewound for the write.

    Contract
    --------
    in : a Django ``UploadedFile`` (in memory or on a temp file).
    out: the hex digest, with the stream seeked back to 0.

    Business purpose: the digest is the submission's idempotency key. Computing
    it *before* the job row exists is what lets "the same file uploaded twice"
    be recognised without first creating something to deduplicate against — and
    it is safe to hash an upload and then store it, because the stream is put
    back exactly where the storage layer expects it.
    """
    digest = hashlib.sha256()
    for chunk in uploaded_file.chunks():
        digest.update(chunk)
    uploaded_file.seek(0)
    return digest.hexdigest()


def validate_local_file(path: str | Path) -> Path:
    """Validate an already-stored file: it must exist, be sized sanely, be allowed.

    Contract
    --------
    in : a path on the local filesystem.
    out: the resolved :class:`Path`.

    Business purpose: the CLI and the tests hand the pipeline real paths rather
    than uploads; they get the same extension and size checks a browser upload
    would.
    """
    resolved = Path(path)
    if not resolved.is_file():
        raise UnsafeFileError(f"no such file: {resolved}")
    validate_extension(resolved.name)
    assert_within_size(resolved.stat().st_size)
    return resolved


def store_upload(*, job, uploaded_file) -> IngestionFile:
    """Persist an ``UploadedFile`` under ``MEDIA_ROOT`` and record it.

    Contract
    --------
    in : the owning job and a Django ``UploadedFile``.
    out: a saved :class:`~apps.ingestion.models.IngestionFile`.

    Business purpose: the file is validated, stored, checksummed, and linked to
    its job in one step, so a job can always be traced back to the exact bytes it
    ran on. The checksum doubles as the idempotency key via
    ``uniq_file_job_checksum``.
    """
    name = safe_filename(uploaded_file.name)
    validate_extension(name)
    assert_within_size(uploaded_file.size)

    relative = f"ingestion/{job.pk}/{name}"
    digest = hashlib.sha256()
    stored_name = default_storage.save(relative, uploaded_file)
    with default_storage.open(stored_name, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)

    return IngestionFile.objects.create(
        job=job,
        original_filename=name,
        storage_path=stored_name,
        checksum=digest.hexdigest(),
        content_type=content_type_for(name, getattr(uploaded_file, "content_type", None)),
    )


def register_local_file(*, job, path: str | Path, original_filename: str | None = None) -> IngestionFile:
    """Record an existing local file against a job without moving it.

    Contract
    --------
    in : the owning job and a path that has already passed
         :func:`validate_local_file`.
    out: a saved :class:`~apps.ingestion.models.IngestionFile` whose
         ``storage_path`` is the absolute source path.

    Business purpose: lets the management command and tests ingest a file in
    place — for example the committed sample dataset — while still giving the job
    a checksum it can be re-run against.
    """
    resolved = validate_local_file(path)
    name = safe_filename(original_filename or resolved.name)
    validate_extension(name)
    return IngestionFile.objects.create(
        job=job,
        original_filename=name,
        storage_path=str(resolved),
        checksum=checksum_of(resolved),
        content_type=content_type_for(name),
    )

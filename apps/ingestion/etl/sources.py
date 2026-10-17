"""Source adapters: turn a CSV, JSON, XLSX, or API payload into raw rows.

Each adapter returns a :class:`SourceReader` — the header it saw plus a lazy
iterator of :class:`RawRow`. The iterator is lazy on purpose: a ten-million-row
CSV must be consumed row by row, not loaded into a list. Only the header is read
eagerly, because normalisation needs the column map before the first row.

Adapters do the least work possible: they yield the file's own values, untyped
and uncorrected. Type coercion and meaning are the normaliser's job, so a rule
written once applies to every source.
"""

from __future__ import annotations

import csv
import itertools
import json
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .errors import SourceFormatError


@dataclass(frozen=True)
class RawRow:
    """One record as read, before any interpretation.

    ``number`` is the 1-based data-row index (the header is not row 1), which is
    the "source row reference" recorded on a validation error.
    """

    number: int
    data: dict[str, Any]


@dataclass
class SourceReader:
    """A parsed source: its header, and a lazy stream of its rows."""

    header: list[str]
    rows: Iterator[RawRow] = field(repr=False)

    @classmethod
    def from_records(cls, records: Iterable[dict[str, Any]]) -> SourceReader:
        """Build a reader from in-memory records (used for manual entry and tests)."""
        iterator = iter(records)
        first, rows = _peek(iterator)
        if first is None:
            return cls(header=[], rows=iter(()))
        if not isinstance(first, dict):
            raise SourceFormatError("each record must be a JSON object")
        return cls(
            header=list(first.keys()),
            rows=_number(_chain(first, rows)),
        )


def _peek(iterator: Iterator[Any]) -> tuple[Any | None, Iterator[Any]]:
    """Return the first item and an iterator that still yields it."""
    try:
        first = next(iterator)
    except StopIteration:
        return None, iter(())
    return first, iterator


def _chain(first: Any, rest: Iterator[Any]) -> Iterator[Any]:
    return itertools.chain([first], rest)


def _number(records: Iterator[dict[str, Any]]) -> Iterator[RawRow]:
    """Attach 1-based row numbers to a stream of record dicts."""
    for number, record in enumerate(records, start=1):
        yield RawRow(number=number, data=record)


# --------------------------------------------------------------------------- #
# CSV
# --------------------------------------------------------------------------- #

def open_csv(path: str | Path) -> SourceReader:
    """Stream a CSV file. The header is read eagerly, the rows are not.

    ``utf-8-sig`` transparently strips a byte-order mark, which Excel writes and
    which would otherwise become part of the first column's name.
    """
    # Opened without a ``with`` block on purpose: the handle must stay open for
    # as long as the returned row iterator is being consumed. ``_csv_rows``
    # closes it when iteration ends.
    handle = Path(path).open("r", newline="", encoding="utf-8-sig")  # noqa: SIM115
    reader = csv.DictReader(handle)
    header = reader.fieldnames
    if not header:
        handle.close()
        raise SourceFormatError("CSV has no header row")
    return SourceReader(header=[str(name) for name in header], rows=_csv_rows(handle, reader))


def _csv_rows(handle, reader: csv.DictReader) -> Iterator[RawRow]:
    try:
        for number, record in enumerate(reader, start=1):
            yield RawRow(number=number, data=dict(record))
    finally:
        handle.close()


# --------------------------------------------------------------------------- #
# JSON
# --------------------------------------------------------------------------- #

def open_json(path: str | Path, *, records_key: str | None = None) -> SourceReader:
    """Stream a JSON document.

    Three shapes are accepted, because all three are common in market-data
    feeds:

    * a top-level array of objects — streamed element by element;
    * an object wrapping the array under ``data`` / ``records`` / ``rows`` (or an
      explicit ``records_key``) — the wrapper is small, so it is loaded whole;
    * JSON Lines, one object per line, when the file ends in ``.jsonl``.

    Business purpose: the array form is the one that gets large, so it is the
    one that is streamed rather than materialised.
    """
    path = Path(path)
    if path.suffix.lower() == ".jsonl":
        return _source_from_json_lines(path)

    with path.open("r", encoding="utf-8-sig") as handle:
        head = handle.read(4096)

    stripped = head.lstrip()
    if not stripped:
        raise SourceFormatError("JSON file is empty")

    if stripped[0] == "[":
        return _source_from_json_array(path)

    if stripped[0] == "{":
        return _source_from_json_object(path, records_key)

    raise SourceFormatError("JSON must be an array or an object of records")


def _source_from_json_array(path: Path) -> SourceReader:
    def records() -> Iterator[dict[str, Any]]:
        with path.open("r", encoding="utf-8-sig") as handle:
            for value in _iter_json_array(handle):
                if not isinstance(value, dict):
                    raise SourceFormatError("a JSON array entry is not an object")
                yield value

    first, rest = _peek(iter(records()))
    if first is None:
        raise SourceFormatError("JSON array contains no records")
    return SourceReader(header=list(first.keys()), rows=_number(_chain(first, rest)))


def _source_from_json_object(path: Path, records_key: str | None) -> SourceReader:
    with path.open("r", encoding="utf-8-sig") as handle:
        try:
            document = json.load(handle)
        except json.JSONDecodeError as exc:
            # A file of several top-level objects is JSON Lines with the wrong
            # name; that is a real feed shape, so accept it rather than fail.
            return _source_from_json_lines(path, reason=str(exc))

    if not isinstance(document, dict):
        raise SourceFormatError("JSON object wrapper is not an object")

    keys = [records_key] if records_key else ["data", "records", "rows", "results"]
    payload = next((document[key] for key in keys if isinstance(document.get(key), list)), None)
    if payload is None:
        raise SourceFormatError(
            "JSON object has no array under any of: " + ", ".join(keys)
        )
    if not payload:
        return SourceReader(header=[], rows=iter(()))
    if not isinstance(payload[0], dict):
        raise SourceFormatError("JSON records must be objects")
    return SourceReader(header=list(payload[0].keys()), rows=_number(iter(payload)))


def _source_from_json_lines(path: Path, *, reason: str = "") -> SourceReader:
    def records() -> Iterator[dict[str, Any]]:
        with path.open("r", encoding="utf-8-sig") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    value = json.loads(line)
                except json.JSONDecodeError as exc:
                    raise SourceFormatError(f"malformed JSON line: {exc}") from exc
                if not isinstance(value, dict):
                    raise SourceFormatError("a JSON Lines entry is not an object")
                yield value

    first, rest = _peek(iter(records()))
    if first is None:
        raise SourceFormatError(reason or "JSON Lines file contains no records")
    return SourceReader(header=list(first.keys()), rows=_number(_chain(first, rest)))


def _iter_json_array(handle) -> Iterator[Any]:
    """Yield the elements of a top-level JSON array without reading it whole.

    ``json`` has no streaming API and ``ijson`` is not a dependency, so this
    scans for one value at a time with ``raw_decode``, refilling the buffer only
    when a value is incomplete. Memory is bounded by the largest single element
    plus the read chunk.
    """
    decoder = json.JSONDecoder()
    chunk_size = 1 << 16
    buffer = ""

    while "[" not in buffer:
        chunk = handle.read(chunk_size)
        if not chunk:
            raise SourceFormatError("JSON array is missing its opening '['")
        buffer += chunk
    buffer = buffer[buffer.index("[") + 1 :]

    while True:
        buffer = buffer.lstrip()
        while buffer.startswith(","):
            buffer = buffer[1:].lstrip()
        if buffer.startswith("]"):
            return

        if not buffer:
            chunk = handle.read(chunk_size)
            if not chunk:
                raise SourceFormatError("JSON array is truncated before ']'")
            buffer += chunk
            continue

        try:
            value, end = decoder.raw_decode(buffer)
        except json.JSONDecodeError:
            chunk = handle.read(chunk_size)
            if not chunk:
                raise SourceFormatError("JSON array contains a malformed value") from None
            buffer += chunk
            continue

        yield value
        buffer = buffer[end:]


# --------------------------------------------------------------------------- #
# XLSX
# --------------------------------------------------------------------------- #

def open_xlsx(path: str | Path, *, sheet_name: str | None = None) -> SourceReader:
    """Stream an Excel worksheet with ``openpyxl`` in read-only mode.

    Read-only mode does not build the whole grid in memory, which is what makes
    a large workbook usable. ``data_only=True`` returns a formula's cached result
    rather than the formula text.
    """
    from openpyxl import load_workbook  # imported lazily: not every run needs it

    workbook = load_workbook(filename=str(path), read_only=True, data_only=True)
    try:
        sheet = workbook[sheet_name] if sheet_name else workbook.active
    except KeyError:
        workbook.close()
        raise SourceFormatError(f"worksheet {sheet_name!r} does not exist") from None

    row_iterator = sheet.iter_rows(values_only=True)
    header_row = next(row_iterator, None)
    if header_row is None:
        workbook.close()
        raise SourceFormatError("worksheet is empty")

    header = ["" if cell is None else str(cell) for cell in header_row]
    return SourceReader(header=header, rows=_xlsx_rows(workbook, row_iterator, header))


def _xlsx_rows(workbook, row_iterator, header: list[str]) -> Iterator[RawRow]:
    try:
        for number, values in enumerate(row_iterator, start=1):
            yield RawRow(number=number, data=dict(zip(header, values)))
    finally:
        workbook.close()


# --------------------------------------------------------------------------- #
# External API
# --------------------------------------------------------------------------- #

def open_api(fetcher: Callable[[], Iterable[dict[str, Any]]]) -> SourceReader:
    """Wrap a callable that yields API records into a :class:`SourceReader`.

    Contract
    --------
    in : ``fetcher()`` returning an iterable of record dicts. It is injected
         rather than hard-coded so tests can supply a stub and so a vendor
         package can be swapped without touching the pipeline.
    out: a reader over those records.

    Business purpose: the API source goes through exactly the same
    normalisation, rules, and persistence as a file, which is the whole point of
    separating source from meaning.
    """
    first_row, rest = _peek(iter(fetcher()))
    if first_row is None:
        return SourceReader(header=[], rows=iter(()))
    if not isinstance(first_row, dict):
        raise SourceFormatError("API records must be objects")
    return SourceReader(header=list(first_row.keys()), rows=_number(_chain(first_row, rest)))


def build_requests_fetcher(
    url: str,
    *,
    params: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
    records_key: str | None = None,
    page_param: str | None = None,
    max_pages: int = 100,
    timeout: float = 30.0,
) -> Callable[[], Iterator[dict[str, Any]]]:
    """Return a paginating fetcher backed by ``requests``.

    Delivery order is not a contract; what matters is that every page is yielded
    as plain dicts so the rest of the pipeline cannot tell an API from a file.
    """
    def fetch() -> Iterator[dict[str, Any]]:
        import requests  # imported lazily so the module imports without network deps

        page = 1
        while page <= max_pages:
            query = dict(params or {})
            if page_param:
                query[page_param] = page
            response = requests.get(url, params=query, headers=headers, timeout=timeout)
            response.raise_for_status()
            payload = response.json()

            if isinstance(payload, list):
                records = payload
            else:
                keys = [records_key] if records_key else ["data", "records", "rows", "results"]
                records = next(
                    (payload[key] for key in keys if isinstance(payload.get(key), list)),
                    None,
                )
                if records is None:
                    raise SourceFormatError("API response has no records array")

            if not records:
                return
            for record in records:
                if not isinstance(record, dict):
                    raise SourceFormatError("API record is not an object")
                yield record
            if not page_param:
                return
            page += 1

    return fetch


# --------------------------------------------------------------------------- #
# Dispatch
# --------------------------------------------------------------------------- #

def read_source(
    source_type: str,
    *,
    path: str | Path | None = None,
    fetcher: Callable[[], Iterable[dict[str, Any]]] | None = None,
    sheet_name: str | None = None,
    records_key: str | None = None,
) -> SourceReader:
    """Open ``source_type`` at ``path`` (or via ``fetcher``) and return a reader.

    Contract
    --------
    in : a ``SourceType`` value, plus whatever that type needs — a path for the
         file formats, a callable for the API.
    out: a :class:`SourceReader`.

    Raises :class:`SourceFormatError` for an unknown type or a missing argument.
    """
    if source_type == "api":
        if fetcher is None:
            raise SourceFormatError("an API source requires a fetcher callable")
        return open_api(fetcher)

    if path is None:
        raise SourceFormatError(f"a {source_type} source requires a path")

    if source_type == "csv" or source_type == "manual":
        return open_csv(path)
    if source_type == "json":
        return open_json(path, records_key=records_key)
    if source_type == "xlsx":
        return open_xlsx(path, sheet_name=sheet_name)

    raise SourceFormatError(f"unsupported source type: {source_type!r}")

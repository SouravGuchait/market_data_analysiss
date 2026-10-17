"""Source adapters: every format yields the same raw rows, or fails loudly.

The adapters are the only part of the engine that knows a file's shape, so these
tests cover both the happy path in each format and the malformed inputs a real
feed produces: an empty file, a truncated array, a wrapper with no records list,
and a workbook with no sheet.
"""

import pytest

from apps.ingestion.etl.errors import SourceFormatError
from apps.ingestion.etl.sources import open_xlsx, read_source

from .helpers import PRICE_HEADER, price_row, write_csv, write_json, write_xlsx


def rows_of(reader):
    return [(row.number, row.data) for row in reader.rows]


# --------------------------------------------------------------------------- #
# CSV
# --------------------------------------------------------------------------- #

def test_csv_reads_header_and_numbers_data_rows_from_one(tmp_path):
    path = write_csv(tmp_path / "prices.csv", [price_row(symbol="AAPL"), price_row(symbol="MSFT")])

    reader = read_source("csv", path=path)
    rows = rows_of(reader)

    assert reader.header == PRICE_HEADER
    assert [number for number, _ in rows] == [1, 2]
    assert rows[0][1]["symbol"] == "AAPL"


def test_csv_strips_a_byte_order_mark_from_the_first_header(tmp_path):
    path = tmp_path / "bom.csv"
    path.write_bytes(b"\xef\xbb\xbfsymbol,trading_date\nAAPL,2024-03-12\n")

    reader = read_source("csv", path=path)

    assert reader.header == ["symbol", "trading_date"]


def test_an_empty_csv_is_a_format_error(tmp_path):
    path = tmp_path / "empty.csv"
    path.write_text("", encoding="utf-8")

    with pytest.raises(SourceFormatError):
        read_source("csv", path=path)


# --------------------------------------------------------------------------- #
# JSON
# --------------------------------------------------------------------------- #

def test_json_array_is_streamed(tmp_path):
    path = write_json(tmp_path / "prices.json", [{"symbol": "AAPL"}, {"symbol": "MSFT"}])

    reader = read_source("json", path=path)
    rows = rows_of(reader)

    assert reader.header == ["symbol"]
    assert [data["symbol"] for _, data in rows] == ["AAPL", "MSFT"]


def test_an_empty_json_array_is_a_format_error(tmp_path):
    path = write_json(tmp_path / "empty.json", [])

    with pytest.raises(SourceFormatError):
        read_source("json", path=path)


def test_a_truncated_json_array_is_a_format_error(tmp_path):
    path = tmp_path / "truncated.json"
    path.write_text('[{"symbol": "AAPL"}', encoding="utf-8")

    reader = read_source("json", path=path)
    with pytest.raises(SourceFormatError):
        list(reader.rows)


def test_a_json_object_wrapper_is_unwrapped(tmp_path):
    path = write_json(tmp_path / "wrapped.json", {"data": [{"symbol": "AAPL"}]})

    reader = read_source("json", path=path)

    assert [data["symbol"] for _, data in rows_of(reader)] == ["AAPL"]


def test_a_json_object_without_a_records_list_is_a_format_error(tmp_path):
    path = write_json(tmp_path / "no-records.json", {"symbol": "AAPL"})

    with pytest.raises(SourceFormatError):
        read_source("json", path=path)


def test_json_lines_are_read_one_object_per_line(tmp_path):
    path = tmp_path / "prices.jsonl"
    path.write_text('{"symbol": "AAPL"}\n\n{"symbol": "MSFT"}\n', encoding="utf-8")

    reader = read_source("json", path=path)

    assert [data["symbol"] for _, data in rows_of(reader)] == ["AAPL", "MSFT"]


def test_several_json_objects_without_the_jsonl_suffix_still_read(tmp_path):
    path = tmp_path / "prices.json"
    path.write_text('{"symbol": "AAPL"}\n{"symbol": "MSFT"}\n', encoding="utf-8")

    reader = read_source("json", path=path)

    assert [data["symbol"] for _, data in rows_of(reader)] == ["AAPL", "MSFT"]


# --------------------------------------------------------------------------- #
# XLSX
# --------------------------------------------------------------------------- #

def test_xlsx_reads_header_and_rows(tmp_path):
    path = write_xlsx(tmp_path / "prices.xlsx", [price_row(symbol="AAPL"), price_row(symbol="MSFT")])

    reader = read_source("xlsx", path=path)
    rows = rows_of(reader)

    assert reader.header == PRICE_HEADER
    assert [data["symbol"] for _, data in rows] == ["AAPL", "MSFT"]


def test_xlsx_can_select_a_named_sheet(tmp_path):
    path = write_xlsx(tmp_path / "prices.xlsx", [price_row(symbol="AAPL")], sheet_title="daily")

    reader = read_source("xlsx", path=path, sheet_name="daily")

    assert [data["symbol"] for _, data in rows_of(reader)] == ["AAPL"]


def test_a_missing_worksheet_is_a_format_error(tmp_path):
    path = write_xlsx(tmp_path / "prices.xlsx", [price_row()], sheet_title="daily")

    with pytest.raises(SourceFormatError):
        open_xlsx(path, sheet_name="weekly")


def test_an_empty_worksheet_is_a_format_error(tmp_path):
    from openpyxl import Workbook

    path = tmp_path / "empty.xlsx"
    Workbook().save(path)

    with pytest.raises(SourceFormatError):
        open_xlsx(path)


# --------------------------------------------------------------------------- #
# API
# --------------------------------------------------------------------------- #

def test_the_api_adapter_reads_records_from_an_injected_fetcher():
    reader = read_source("api", fetcher=lambda: [{"symbol": "AAPL"}, {"symbol": "MSFT"}])

    assert [data["symbol"] for _, data in rows_of(reader)] == ["AAPL", "MSFT"]


def test_the_api_adapter_rejects_non_object_records():
    with pytest.raises(SourceFormatError):
        read_source("api", fetcher=lambda: [["AAPL"]])


# --------------------------------------------------------------------------- #
# Dispatch
# --------------------------------------------------------------------------- #

def test_an_unknown_source_type_is_a_format_error(tmp_path):
    with pytest.raises(SourceFormatError):
        read_source("parquet", path=tmp_path / "x.parquet")


def test_an_api_source_needs_a_fetcher():
    with pytest.raises(SourceFormatError):
        read_source("api")


def test_a_file_source_needs_a_path():
    with pytest.raises(SourceFormatError):
        read_source("csv")

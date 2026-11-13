from __future__ import annotations

import pytest

from app.ingestion import (
    IngestionError,
    extract_requests_from_tables,
    parse_csv_to_tables,
    parse_deadline_days,
    parse_discount_pct,
    parse_google_sheet_url,
    parse_vnd_amount,
    parse_xlsx_to_tables,
)
from app.master_data import SAMPLE_CSV_PATH, build_sample_workspace_xlsx_bytes


def test_parse_google_sheet_url_variants() -> None:
    sid, gid = parse_google_sheet_url(
        "https://docs.google.com/spreadsheets/d/1AbCdEfGhI_12345/edit#gid=987654"
    )
    assert sid == "1AbCdEfGhI_12345"
    assert gid == "987654"

    sid2, gid2 = parse_google_sheet_url(
        "https://docs.google.com/spreadsheets/d/1AbCdEfGhI_12345/edit?usp=sharing&gid=0"
    )
    assert sid2 == "1AbCdEfGhI_12345"
    assert gid2 == "0"

    with pytest.raises(IngestionError):
        parse_google_sheet_url("https://example.com/not-a-sheet")


def test_parse_amounts_deadlines_discounts() -> None:
    assert parse_vnd_amount("500 triệu") == 500_000_000
    assert parse_vnd_amount("1.1 tỷ") == 1_100_000_000
    assert parse_vnd_amount("320,000,000") == 320_000_000
    assert parse_vnd_amount("Chưa có") is None

    assert parse_deadline_days("5 ngày") == 5
    assert parse_deadline_days("2 tuần") == 14
    assert parse_discount_pct("8%") == 8.0
    assert parse_discount_pct(0.08) == 8.0


def test_parse_sample_csv_and_multisheet_xlsx() -> None:
    csv_tables = parse_csv_to_tables(SAMPLE_CSV_PATH.read_bytes())
    reqs = extract_requests_from_tables(csv_tables)
    assert len(reqs) == 6
    assert reqs[0].request_id == "YC-2026-001"

    xlsx_bytes = build_sample_workspace_xlsx_bytes()
    xlsx_tables = parse_xlsx_to_tables(xlsx_bytes)
    assert "Yeu_Cau_Khach_Hang" in xlsx_tables
    assert "Danh_Muc_Gia" in xlsx_tables
    assert "Ton_Kho_Trien_Khai" in xlsx_tables

    reqs_xlsx = extract_requests_from_tables(xlsx_tables)
    assert len(reqs_xlsx) == 6
    assert reqs_xlsx[1].customer_name == "Tập đoàn Dược phẩm An Khang"


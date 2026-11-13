from __future__ import annotations

import csv
import io
import re
import unicodedata
from datetime import datetime
from typing import Any
from urllib.parse import parse_qs, urlparse

import httpx
from openpyxl import load_workbook

from app.models import RawCustomerRequest


class IngestionError(ValueError):
    """Raised when an input file or Google Sheets link cannot be ingested."""


def strip_accents(text: str) -> str:
    text = text.replace("đ", "d").replace("Đ", "D")
    nfkd = unicodedata.normalize("NFKD", text)
    return "".join(ch for ch in nfkd if not unicodedata.combining(ch))


def normalize_header(header: Any) -> str:
    raw = strip_accents(str(header or "").strip().lower())
    raw = re.sub(r"[^a-z0-9]+", "_", raw).strip("_")
    return raw


def parse_vnd_amount(value: Any) -> float | None:
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        return float(value) if value > 0 else None
    text = strip_accents(str(value).strip().lower())
    if not text or text in {"chua co", "chua ro", "n/a", "na", "none", "-", "null"}:
        return None
    # Handle "1.1 ty", "500 trieu", "500tr", "500,000,000"
    m = re.search(r"(\d+(?:[.,]\d+)?)\s*(ty|t|trieu|tr|trieu dong|m)\b", text)
    if m:
        num = float(m.group(1).replace(",", "."))
        unit = m.group(2)
        if unit in {"ty", "t"}:
            return num * 1_000_000_000
        return num * 1_000_000
    cleaned = re.sub(r"[^\d.]", "", text.replace(",", ""))
    if not cleaned:
        return None
    try:
        num = float(cleaned)
        return num if num > 0 else None
    except ValueError:
        return None


def parse_deadline_days(value: Any) -> int | None:
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        return int(round(value)) if value > 0 else None
    text = strip_accents(str(value).strip().lower())
    if not text or text in {"chua ro", "n/a", "na", "-", "none"}:
        return None
    m_week = re.search(r"(\d+(?:[.,]\d+)?)\s*tuan\b", text)
    if m_week:
        return max(1, int(round(float(m_week.group(1).replace(",", ".")) * 7)))
    m_month = re.search(r"(\d+(?:[.,]\d+)?)\s*thang\b", text)
    if m_month:
        return max(1, int(round(float(m_month.group(1).replace(",", ".")) * 30)))
    m_day = re.search(r"(\d+)", text)
    if m_day:
        days = int(m_day.group(1))
        return days if days > 0 else None
    return None


def parse_discount_pct(value: Any) -> float | None:
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        val = float(value)
        if 0 < val < 0.5:
            # Handle Excel percentage fraction like 0.08 -> 8%
            return round(val * 100, 2)
        return round(val, 2)
    text = str(value).strip().replace("%", "").replace(",", ".")
    if not text:
        return None
    m = re.search(r"(\d+(?:\.\d+)?)", text)
    if not m:
        return None
    val = float(m.group(1))
    if 0 < val < 0.5 and "%" not in str(value):
        return round(val * 100, 2)
    return round(val, 2)


def decode_csv_bytes(raw_bytes: bytes) -> str:
    for enc in ("utf-8-sig", "utf-8", "cp1258", "cp1252", "latin-1"):
        try:
            return raw_bytes.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw_bytes.decode("utf-8", errors="replace")


def parse_csv_to_tables(raw_bytes: bytes, sheet_name: str = "CSV_Input") -> dict[str, list[dict[str, Any]]]:
    text = decode_csv_bytes(raw_bytes)
    lines = [ln for ln in text.splitlines() if ln.strip()]
    if not lines:
        raise IngestionError("File CSV trống hoặc không có dòng dữ liệu hợp lệ.")
    sample = "\n".join(lines[:10])
    delimiter = ","
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=",;\t|")
        delimiter = dialect.delimiter
    except csv.Error:
        pass
    reader = csv.DictReader(io.StringIO("\n".join(lines)), delimiter=delimiter)
    if not reader.fieldnames:
        raise IngestionError("Không tìm thấy dòng tiêu đề cột trong file CSV.")
    rows: list[dict[str, Any]] = []
    for row in reader:
        cleaned = {
            (k.strip() if k else f"col_{i}"): (v.strip() if isinstance(v, str) else v)
            for i, (k, v) in enumerate(row.items())
            if k is not None
        }
        if any(str(v or "").strip() for v in cleaned.values()):
            rows.append(cleaned)
    return {sheet_name: rows}


def parse_xlsx_to_tables(raw_bytes: bytes) -> dict[str, list[dict[str, Any]]]:
    try:
        wb = load_workbook(io.BytesIO(raw_bytes), read_only=True, data_only=True)
    except Exception as exc:
        raise IngestionError(f"Không thể đọc file Excel (.xlsx): {exc}") from exc

    tables: dict[str, list[dict[str, Any]]] = {}
    try:
        for ws in wb.worksheets:
            rows_iter = ws.iter_rows(values_only=True)
            header_row = None
            for r in rows_iter:
                if r and any(cell is not None and str(cell).strip() for cell in r):
                    header_row = [str(c).strip() if c is not None else f"col_{idx}" for idx, c in enumerate(r)]
                    break
            if not header_row:
                continue
            sheet_rows: list[dict[str, Any]] = []
            for r in rows_iter:
                if not r or not any(cell is not None and str(cell).strip() for cell in r):
                    continue
                row_dict: dict[str, Any] = {}
                for idx, header in enumerate(header_row):
                    val = r[idx] if idx < len(r) else None
                    if isinstance(val, datetime):
                        val = val.strftime("%Y-%m-%d %H:%M")
                    row_dict[header] = val
                sheet_rows.append(row_dict)
            if sheet_rows:
                tables[ws.title] = sheet_rows
    finally:
        wb.close()

    if not tables:
        raise IngestionError("File Excel không chứa dữ liệu trong bất kỳ sheet nào.")
    return tables


def parse_google_sheet_url(url: str) -> tuple[str, str | None]:
    """Extract spreadsheet_id and optional gid from a Google Sheets URL."""
    cleaned = url.strip()
    m = re.search(r"/spreadsheets/d/([a-zA-Z0-9_-]+)", cleaned)
    if not m:
        raise IngestionError(
            "Link Google Sheets không hợp lệ. Vui lòng dán link có dạng "
            "'https://docs.google.com/spreadsheets/d/<ID>/...'."
        )
    spreadsheet_id = m.group(1)
    parsed = urlparse(cleaned)
    gid: str | None = None
    qs = parse_qs(parsed.query)
    if "gid" in qs and qs["gid"]:
        gid = qs["gid"][0]
    elif parsed.fragment:
        frag_m = re.search(r"gid=(\d+)", parsed.fragment)
        if frag_m:
            gid = frag_m.group(1)
    return spreadsheet_id, gid


async def fetch_google_sheet_tables(url: str) -> tuple[dict[str, list[dict[str, Any]]], str]:
    """Download Google Sheets as multi-sheet XLSX (or fallback to CSV for single tab)."""
    spreadsheet_id, gid = parse_google_sheet_url(url)
    xlsx_url = f"https://docs.google.com/spreadsheets/d/{spreadsheet_id}/export?format=xlsx"
    csv_url = f"https://docs.google.com/spreadsheets/d/{spreadsheet_id}/export?format=csv" + (
        f"&gid={gid}" if gid else ""
    )

    headers = {"User-Agent": "MinhAn-Sales-Agent/1.0"}
    async with httpx.AsyncClient(follow_redirects=True, timeout=25.0, headers=headers) as client:
        # If explicit gid was requested, still try xlsx first so master data + request sheets both load,
        # unless xlsx fails or lacks data.
        try:
            resp = await client.get(xlsx_url)
            content_type = resp.headers.get("content-type", "").lower()
            if resp.status_code == 200 and (
                "spreadsheet" in content_type
                or "octet-stream" in content_type
                or resp.content[:2] == b"PK"
            ):
                tables = parse_xlsx_to_tables(resp.content)
                return tables, f"Google Sheets ({spreadsheet_id})"
        except httpx.HTTPError:
            pass

        try:
            resp_csv = await client.get(csv_url)
        except httpx.HTTPError as exc:
            raise IngestionError(
                f"Không thể kết nối tới Google Sheets ({exc}). Kiểm tra kết nối mạng hoặc tải file .csv/.xlsx lên trực tiếp."
            ) from exc

        if resp_csv.status_code in (401, 403):
            raise IngestionError(
                "Google Sheets đang bật chế độ riêng tư (HTTP 401/403). Vui lòng chia sẻ 'Bất kỳ ai có đường liên kết đều có thể xem' hoặc tải xuống dạng .xlsx/.csv."
            )
        if resp_csv.status_code != 200:
            raise IngestionError(
                f"Không thể tải Google Sheets (HTTP {resp_csv.status_code}). Kiểm tra lại đường dẫn hoặc quyền truy cập."
            )

        content_type = resp_csv.headers.get("content-type", "").lower()
        body_lower = resp_csv.text[:500].lower()
        if "text/html" in content_type or "<!doctype html" in body_lower or "accounts.google.com" in body_lower:
            raise IngestionError(
                "Google Sheets yêu cầu đăng nhập Google (file chưa mở chia sẻ công khai). "
                "Vui lòng bật 'Anyone with the link -> Viewer' hoặc tải file .xlsx/.csv lên."
            )

        tables = parse_csv_to_tables(resp_csv.content, sheet_name="Google_Sheet_Tab")
        return tables, f"Google Sheets ({spreadsheet_id})"


_FIELD_ALIASES: dict[str, set[str]] = {
    "request_id": {"request_id", "ma_yeu_cau", "ma_yc", "id", "code", "stt"},
    "received_at": {"received_at", "ngay_nhan", "thoi_gian_nhan", "ngay_gui", "created_at", "timestamp", "date"},
    "source": {"source", "nguon", "kenh", "kenh_tiep_nhan", "channel"},
    "customer_name": {"customer_name", "khach_hang", "ten_khach_hang", "cong_ty", "ten_cong_ty", "doanh_nghiep", "company"},
    "contact_name": {"contact_name", "nguoi_lien_he", "lien_he", "ten_nguoi_lien_he", "sender"},
    "contact_email": {"contact_email", "email", "email_khach_hang", "email_lien_he"},
    "customer_approver": {"customer_approver", "nguoi_phe_duyet", "nguoi_duyet", "nguoi_quyet_dinh", "approver"},
    "assigned_rep": {"assigned_rep", "nvkd", "nvkd_phu_trach", "nhan_vien_kinh_doanh", "sales_rep", "owner"},
    "raw_text": {
        "raw_text",
        "noi_dung",
        "noi_dung_yeu_cau",
        "yeu_cau",
        "mo_ta",
        "chi_tiet_yeu_cau",
        "request_text",
        "content",
        "message",
        "chi_tiet",
    },
    "stated_budget": {"stated_budget", "ngan_sach", "ngan_sach_tong", "budget", "du_toan"},
    "requested_deadline_days": {
        "requested_deadline_days",
        "thoi_han",
        "thoi_han_trien_khai",
        "thoi_han_ngay",
        "deadline_days",
        "deadline",
        "so_ngay_trien_khai",
    },
    "requested_discount_pct": {"requested_discount_pct", "chiet_khau", "chiet_khau_de_nghi", "muc_chiet_khau", "discount"},
    "notes": {"notes", "ghi_chu", "note"},
}


def _is_master_data_sheet(sheet_name: str, norm_headers: set[str]) -> bool:
    sname = strip_accents(sheet_name).lower()
    if any(k in sname for k in ("danh_muc", "ton_kho", "chinh_sach", "doi_ngu", "catalog", "inventory", "policy")):
        return True
    if "unit_price" in norm_headers or "available_stock" in norm_headers or ("parameter" in norm_headers and "value" in norm_headers):
        return True
    if "specialty_categories" in norm_headers:
        return True
    return False


def extract_requests_from_tables(tables: dict[str, list[dict[str, Any]]]) -> list[RawCustomerRequest]:
    """Identify the customer request sheet(s) and convert rows into RawCustomerRequest objects."""
    requests: list[RawCustomerRequest] = []
    candidate_sheets: list[tuple[str, list[dict[str, Any]]]] = []

    for sheet_name, rows in tables.items():
        if not rows:
            continue
        norm_headers = {normalize_header(k) for k in rows[0].keys()}
        if _is_master_data_sheet(sheet_name, norm_headers):
            continue
        candidate_sheets.append((sheet_name, rows))

    if not candidate_sheets:
        raise IngestionError(
            "Không tìm thấy bảng/sheet chứa Yêu cầu khách hàng (chỉ thấy dữ liệu danh mục/chính sách)."
        )

    counter = 1
    for _, rows in candidate_sheets:
        for row in rows:
            mapped: dict[str, Any] = {}
            unmapped_parts: list[str] = []
            for orig_k, val in row.items():
                nk = normalize_header(orig_k)
                matched_field = None
                for field, aliases in _FIELD_ALIASES.items():
                    if nk in aliases:
                        matched_field = field
                        break
                if matched_field:
                    mapped[matched_field] = val
                elif val is not None and str(val).strip():
                    unmapped_parts.append(f"{orig_k}: {val}")

            raw_text = str(mapped.get("raw_text") or "").strip()
            if not raw_text and unmapped_parts:
                raw_text = " | ".join(unmapped_parts)
            if not raw_text:
                # Try joining any non-empty descriptive fields if the sheet uses nonstandard headers
                fallback_parts = [f"{k}: {v}" for k, v in row.items() if v is not None and str(v).strip()]
                raw_text = " | ".join(fallback_parts)
            if not raw_text:
                continue

            req_id = str(mapped.get("request_id") or "").strip() or f"YC-{counter:03d}"
            counter += 1

            requests.append(
                RawCustomerRequest(
                    request_id=req_id,
                    received_at=str(mapped.get("received_at") or "").strip()
                    or datetime.now().strftime("%Y-%m-%d %H:%M"),
                    source=str(mapped.get("source") or "Tải lên").strip(),
                    customer_name=str(mapped.get("customer_name") or "").strip(),
                    contact_name=str(mapped.get("contact_name") or "").strip(),
                    contact_email=str(mapped.get("contact_email") or "").strip(),
                    customer_approver=str(mapped.get("customer_approver") or "").strip(),
                    assigned_rep=str(mapped.get("assigned_rep") or "").strip(),
                    raw_text=raw_text,
                    stated_budget=parse_vnd_amount(mapped.get("stated_budget")),
                    requested_deadline_days=parse_deadline_days(mapped.get("requested_deadline_days")),
                    requested_discount_pct=parse_discount_pct(mapped.get("requested_discount_pct")),
                    notes=str(mapped.get("notes") or "").strip(),
                )
            )

    if not requests:
        raise IngestionError("Không có dòng yêu cầu khách hàng hợp lệ nào để phân tích.")
    return requests


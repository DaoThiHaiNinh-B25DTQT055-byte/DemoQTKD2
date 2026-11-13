from __future__ import annotations

import copy
import io
import json
from pathlib import Path
from typing import Any

from openpyxl import Workbook

from app.models import DiscountPolicy, MasterData, ProductItem, SalesRep

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = BASE_DIR / "data"
MASTER_DATA_PATH = DATA_DIR / "master_data.json"
SAMPLE_CSV_PATH = DATA_DIR / "sample_minh_an_requests.csv"
SAMPLE_XLSX_PATH = DATA_DIR / "sample_minh_an_workspace.xlsx"

_DEFAULT_MASTER_DATA: MasterData | None = None


def load_master_data() -> MasterData:
    """Load current active master data from disk."""
    global _DEFAULT_MASTER_DATA
    raw = json.loads(MASTER_DATA_PATH.read_text(encoding="utf-8"))
    data = MasterData.model_validate(raw)
    if _DEFAULT_MASTER_DATA is None:
        _DEFAULT_MASTER_DATA = copy.deepcopy(data)
    return data


def save_master_data(data: MasterData) -> MasterData:
    """Persist master data updates to disk."""
    MASTER_DATA_PATH.write_text(
        json.dumps(data.model_dump(), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return data


def build_sample_workspace_xlsx_bytes(master: MasterData | None = None) -> bytes:
    """Generate a formatted multi-sheet Excel workbook for Minh An Co."""
    if master is None:
        master = load_master_data()

    wb = Workbook()

    # Sheet 1: Yeu_Cau_Khach_Hang
    ws_req = wb.active
    ws_req.title = "Yeu_Cau_Khach_Hang"
    if SAMPLE_CSV_PATH.exists():
        import csv

        with SAMPLE_CSV_PATH.open("r", encoding="utf-8-sig", newline="") as f:
            reader = csv.reader(f)
            for row in reader:
                converted: list[Any] = []
                for idx, val in enumerate(row):
                    if idx in (9, 10, 11) and val.strip():
                        try:
                            converted.append(float(val) if "." in val else int(val))
                            continue
                        except ValueError:
                            pass
                    converted.append(val)
                ws_req.append(converted)

    # Sheet 2: Danh_Muc_Gia
    ws_cat = wb.create_sheet("Danh_Muc_Gia")
    ws_cat.append(
        [
            "sku",
            "name",
            "category",
            "unit",
            "unit_price",
            "previous_price",
            "price_effective_date",
            "aliases",
            "recommended_companions",
            "companion_min_qty",
        ]
    )
    for p in master.products:
        ws_cat.append(
            [
                p.sku,
                p.name,
                p.category,
                p.unit,
                p.unit_price,
                p.previous_price or "",
                p.price_effective_date,
                " | ".join(p.aliases),
                " | ".join(p.recommended_companions),
                p.companion_min_qty,
            ]
        )

    # Sheet 3: Ton_Kho_Trien_Khai
    ws_inv = wb.create_sheet("Ton_Kho_Trien_Khai")
    ws_inv.append(
        [
            "sku",
            "name",
            "available_stock",
            "incoming_stock",
            "incoming_eta_days",
            "deployment_days",
        ]
    )
    for p in master.products:
        ws_inv.append(
            [
                p.sku,
                p.name,
                p.available_stock,
                p.incoming_stock,
                p.incoming_eta_days,
                p.deployment_days,
            ]
        )

    # Sheet 4: Chinh_Sach_Chiet_Khau
    ws_pol = wb.create_sheet("Chinh_Sach_Chiet_Khau")
    ws_pol.append(["parameter", "value", "description"])
    ws_pol.append(["sales_rep_max_discount_pct", master.policy.sales_rep_max_discount_pct, "Hạn mức NVKD tự duyệt (%)"])
    ws_pol.append(["manager_max_discount_pct", master.policy.manager_max_discount_pct, "Hạn mức Trưởng phòng KD duyệt (%)"])
    ws_pol.append(
        [
            "director_approval_contract_threshold",
            master.policy.director_approval_contract_threshold,
            "Ngưỡng giá trị hợp đồng lớn cần BGĐ lưu ý (VND)",
        ]
    )
    ws_pol.append(["sales_manager_name", master.policy.sales_manager_name, "Trưởng phòng Kinh doanh"])
    ws_pol.append(["director_name", master.policy.director_name, "Đại diện Ban Giám đốc"])
    ws_pol.append(["default_clarification_sla_hours", master.policy.default_clarification_sla_hours, "SLA hỏi lại khách hàng (giờ)"])
    ws_pol.append(["follow_up_hours", master.policy.follow_up_hours, "SLA theo dõi lại cơ hội (giờ)"])

    # Sheet 5: Doi_Ngu_NVKD
    ws_rep = wb.create_sheet("Doi_Ngu_NVKD")
    ws_rep.append(["id", "name", "email", "specialty_categories", "active_weekly_load"])
    for r in master.sales_reps:
        ws_rep.append([r.id, r.name, r.email, " | ".join(r.specialty_categories), r.active_weekly_load])

    for ws in wb.worksheets:
        for col in ws.columns:
            max_len = 0
            col_letter = col[0].column_letter
            for cell in col:
                val_str = str(cell.value or "")
                max_len = max(max_len, min(len(val_str), 48))
            ws.column_dimensions[col_letter].width = max(14, max_len + 3)

    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def ensure_sample_xlsx_exists() -> Path:
    """Create sample_minh_an_workspace.xlsx on disk if missing."""
    if not SAMPLE_XLSX_PATH.exists():
        SAMPLE_XLSX_PATH.write_bytes(build_sample_workspace_xlsx_bytes())
    return SAMPLE_XLSX_PATH


def merge_master_data_from_tables(
    current: MasterData,
    tables: dict[str, list[dict[str, Any]]],
) -> tuple[MasterData, list[str]]:
    """Update product catalog, inventory, policy, or sales reps if matching sheets exist."""
    updated = current.model_copy(deep=True)
    updated_sections: list[str] = []
    products_by_sku: dict[str, ProductItem] = {p.sku.upper(): p for p in updated.products}

    def norm_key(k: str) -> str:
        return k.strip().lower().replace(" ", "_")

    for sheet_name, rows in tables.items():
        if not rows:
            continue
        headers = {norm_key(k) for k in rows[0].keys()}
        sname = sheet_name.lower()

        # Catalog / Price list sheet
        if "unit_price" in headers or "don_gia" in headers or "gia_ban" in headers or "danh_muc" in sname:
            for row in rows:
                r = {norm_key(k): v for k, v in row.items()}
                sku = str(r.get("sku") or r.get("ma_sp") or "").strip().upper()
                if not sku:
                    continue
                existing = products_by_sku.get(sku)
                price_raw = r.get("unit_price") or r.get("don_gia") or r.get("gia_ban")
                prev_raw = r.get("previous_price") or r.get("gia_cu")
                if existing:
                    if r.get("name") or r.get("ten_san_pham"):
                        existing.name = str(r.get("name") or r.get("ten_san_pham")).strip()
                    if price_raw not in (None, ""):
                        existing.unit_price = float(str(price_raw).replace(",", ""))
                    if prev_raw not in (None, ""):
                        existing.previous_price = float(str(prev_raw).replace(",", ""))
                    if r.get("available_stock") not in (None, ""):
                        existing.available_stock = int(float(r["available_stock"]))
                    if r.get("deployment_days") not in (None, ""):
                        existing.deployment_days = int(float(r["deployment_days"]))
                else:
                    aliases_raw = str(r.get("aliases") or r.get("tu_khoa") or "").strip()
                    comp_raw = str(r.get("recommended_companions") or "").strip()
                    products_by_sku[sku] = ProductItem(
                        sku=sku,
                        name=str(r.get("name") or r.get("ten_san_pham") or sku).strip(),
                        category=str(r.get("category") or r.get("nhom") or "Thiết bị CNTT").strip(),
                        unit=str(r.get("unit") or r.get("don_vi") or "Bộ").strip(),
                        unit_price=float(str(price_raw or 0).replace(",", "")),
                        previous_price=float(str(prev_raw).replace(",", "")) if prev_raw not in (None, "") else None,
                        price_effective_date=str(r.get("price_effective_date") or "2026-11-01"),
                        available_stock=int(float(r.get("available_stock") or 0)),
                        incoming_stock=int(float(r.get("incoming_stock") or 0)),
                        incoming_eta_days=int(float(r.get("incoming_eta_days") or 0)),
                        deployment_days=int(float(r.get("deployment_days") or 2)),
                        aliases=[a.strip().lower() for a in aliases_raw.split("|") if a.strip()],
                        recommended_companions=[c.strip().upper() for c in comp_raw.split("|") if c.strip()],
                        companion_min_qty=int(float(r.get("companion_min_qty") or 1)),
                    )
            updated.products = list(products_by_sku.values())
            updated_sections.append(f"Danh mục & Giá ({sheet_name})")

        # Inventory & Lead time sheet
        elif "available_stock" in headers or "ton_kha_dung" in headers or "ton_kho" in sname:
            for row in rows:
                r = {norm_key(k): v for k, v in row.items()}
                sku = str(r.get("sku") or r.get("ma_sp") or "").strip().upper()
                if sku in products_by_sku:
                    p = products_by_sku[sku]
                    if r.get("available_stock") not in (None, ""):
                        p.available_stock = int(float(r["available_stock"]))
                    if r.get("ton_kha_dung") not in (None, ""):
                        p.available_stock = int(float(r["ton_kha_dung"]))
                    if r.get("incoming_stock") not in (None, ""):
                        p.incoming_stock = int(float(r["incoming_stock"]))
                    if r.get("incoming_eta_days") not in (None, ""):
                        p.incoming_eta_days = int(float(r["incoming_eta_days"]))
                    if r.get("deployment_days") not in (None, ""):
                        p.deployment_days = int(float(r["deployment_days"]))
            updated.products = list(products_by_sku.values())
            updated_sections.append(f"Tồn khả dụng & Triển khai ({sheet_name})")

        # Policy sheet
        elif "parameter" in headers and "value" in headers:
            pol_dict = updated.policy.model_dump()
            for row in rows:
                r = {norm_key(k): v for k, v in row.items()}
                param = str(r.get("parameter") or "").strip()
                val = r.get("value")
                if param in pol_dict and val not in (None, ""):
                    if isinstance(pol_dict[param], float):
                        pol_dict[param] = float(val)
                    elif isinstance(pol_dict[param], int):
                        pol_dict[param] = int(float(val))
                    else:
                        pol_dict[param] = str(val)
            updated.policy = DiscountPolicy.model_validate(pol_dict)
            updated_sections.append(f"Chính sách chiết khấu ({sheet_name})")

        # Sales reps sheet
        elif "specialty_categories" in headers or "doi_ngu" in sname:
            reps: list[SalesRep] = []
            for row in rows:
                r = {norm_key(k): v for k, v in row.items()}
                if not (r.get("name") or r.get("ten")):
                    continue
                cats = str(r.get("specialty_categories") or "").strip()
                reps.append(
                    SalesRep(
                        id=str(r.get("id") or f"NVKD-{len(reps)+1:02d}").strip(),
                        name=str(r.get("name") or r.get("ten")).strip(),
                        email=str(r.get("email") or "").strip(),
                        specialty_categories=[c.strip() for c in cats.split("|") if c.strip()],
                        active_weekly_load=int(float(r.get("active_weekly_load") or 28)),
                    )
                )
            if reps:
                updated.sales_reps = reps
                updated_sections.append(f"Đội ngũ NVKD ({sheet_name})")

    return updated, updated_sections


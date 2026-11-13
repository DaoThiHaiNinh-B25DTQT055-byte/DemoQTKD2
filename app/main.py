from __future__ import annotations

import os
from pathlib import Path

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles

from app.exporter import export_batch_to_xlsx_bytes
from app.ingestion import (
    IngestionError,
    extract_requests_from_tables,
    fetch_google_sheet_tables,
    parse_csv_to_tables,
    parse_xlsx_to_tables,
)
from app.master_data import (
    SAMPLE_CSV_PATH,
    ensure_sample_xlsx_exists,
    load_master_data,
    merge_master_data_from_tables,
    save_master_data,
)
from app.models import (
    BatchProcessResponse,
    GoogleSheetImportPayload,
    MasterData,
    RawCustomerRequest,
    SingleRequestPayload,
)
from app.workflow import process_batch_requests

BASE_DIR = Path(__file__).resolve().parent.parent
STATIC_DIR = BASE_DIR / "static"

app = FastAPI(
    title="Minh An AI Sales Agent",
    description="Hệ thống AI Agent xử lý yêu cầu báo giá, hồ sơ cơ hội & đề xuất kinh doanh cho Công ty Minh An",
    version="0.1.0",
)

app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


@app.on_event("startup")
def _startup_init() -> None:
    ensure_sample_xlsx_exists()


@app.get("/")
def serve_index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/api/health")
def health_check() -> dict[str, object]:
    master = load_master_data()
    return {
        "status": "ok",
        "company_name": master.company_name,
        "price_list_version": master.price_list_version,
        "product_count": len(master.products),
        "sales_rep_count": len(master.sales_reps),
        "has_env_gemini_key": bool((os.getenv("GEMINI_API_KEY") or "").strip()),
    }


@app.get("/api/master-data", response_model=MasterData)
def get_master_data() -> MasterData:
    return load_master_data()


@app.post("/api/master-data/upload")
async def upload_master_data(file: UploadFile = File(...)) -> dict[str, object]:
    raw_bytes = await file.read()
    filename = (file.filename or "").lower()
    try:
        if filename.endswith(".csv"):
            tables = parse_csv_to_tables(raw_bytes, sheet_name=Path(filename).stem or "Master_CSV")
        elif filename.endswith(".xlsx") or filename.endswith(".xlsm"):
            tables = parse_xlsx_to_tables(raw_bytes)
        else:
            raise IngestionError("Chỉ hỗ trợ file .csv hoặc .xlsx để cập nhật dữ liệu gốc.")
        current = load_master_data()
        updated, sections = merge_master_data_from_tables(current, tables)
        if not sections:
            raise IngestionError(
                "Không nhận diện được sheet/cột dữ liệu gốc hợp lệ (Danh mục giá, Tồn kho, Chính sách hoặc Đội ngũ NVKD)."
            )
        save_master_data(updated)
        return {
            "message": "Đã cập nhật dữ liệu gốc thành công.",
            "updated_sections": sections,
            "master_data": updated,
        }
    except IngestionError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/api/templates/sample-xlsx")
def download_sample_xlsx() -> Response:
    path = ensure_sample_xlsx_exists()
    return Response(
        content=path.read_bytes(),
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": 'attachment; filename="minh_an_mau_du_lieu.xlsx"'},
    )


@app.get("/api/templates/sample-csv")
def download_sample_csv() -> Response:
    if not SAMPLE_CSV_PATH.exists():
        raise HTTPException(status_code=404, detail="Không tìm thấy file CSV mẫu.")
    return Response(
        content=SAMPLE_CSV_PATH.read_bytes(),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": 'attachment; filename="minh_an_mau_yeu_cau.csv"'},
    )


@app.post("/api/process/sample", response_model=BatchProcessResponse)
def run_sample_batch(
    gemini_api_key: str | None = Form(default=None),
    model_name: str = Form(default="gemini-3.7-flash"),
) -> BatchProcessResponse:
    if not SAMPLE_CSV_PATH.exists():
        raise HTTPException(status_code=404, detail="Chưa có dữ liệu mẫu.")
    tables = parse_csv_to_tables(SAMPLE_CSV_PATH.read_bytes(), sheet_name="Yeu_Cau_Mau")
    requests = extract_requests_from_tables(tables)
    master = load_master_data()
    return process_batch_requests(
        requests=requests,
        master=master,
        source_label="Dữ liệu mẫu Công ty Minh An (6 tình huống thực tế)",
        gemini_api_key=gemini_api_key,
        model_name=model_name,
    )


@app.post("/api/process/upload", response_model=BatchProcessResponse)
async def process_uploaded_file(
    file: UploadFile = File(...),
    gemini_api_key: str | None = Form(default=None),
    model_name: str = Form(default="gemini-3.7-flash"),
    sync_master_sheets: bool = Form(default=True),
) -> BatchProcessResponse:
    raw_bytes = await file.read()
    filename = file.filename or "uploaded_file"
    lower_name = filename.lower()
    try:
        if lower_name.endswith(".csv"):
            tables = parse_csv_to_tables(raw_bytes, sheet_name="CSV_Requests")
        elif lower_name.endswith(".xlsx") or lower_name.endswith(".xlsm"):
            tables = parse_xlsx_to_tables(raw_bytes)
        else:
            raise IngestionError("Định dạng chưa hỗ trợ. Vui lòng tải lên file .csv hoặc .xlsx.")

        master = load_master_data()
        updated_sections: list[str] = []
        if sync_master_sheets and len(tables) > 1:
            candidate_master, updated_sections = merge_master_data_from_tables(master, tables)
            if updated_sections:
                master = save_master_data(candidate_master)

        requests = extract_requests_from_tables(tables)
        label = f"File tải lên: {filename}"
        if updated_sections:
            label += f" (Đã đồng bộ {', '.join(updated_sections)})"
        return process_batch_requests(
            requests=requests,
            master=master,
            source_label=label,
            gemini_api_key=gemini_api_key,
            model_name=model_name,
        )
    except IngestionError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/api/process/google-sheet", response_model=BatchProcessResponse)
async def process_google_sheet(payload: GoogleSheetImportPayload) -> BatchProcessResponse:
    try:
        tables, label = await fetch_google_sheet_tables(payload.sheet_url)
        master = load_master_data()
        updated_sections: list[str] = []
        if len(tables) > 1 or payload.use_for == "master_data":
            candidate_master, updated_sections = merge_master_data_from_tables(master, tables)
            if updated_sections:
                master = save_master_data(candidate_master)

        requests = extract_requests_from_tables(tables)
        if updated_sections:
            label += f" (Đã đồng bộ {', '.join(updated_sections)})"
        return process_batch_requests(
            requests=requests,
            master=master,
            source_label=label,
            gemini_api_key=payload.gemini_api_key,
            model_name=payload.model_name,
        )
    except IngestionError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/api/process/single", response_model=BatchProcessResponse)
def process_single(payload: SingleRequestPayload) -> BatchProcessResponse:
    if not payload.raw_text.strip():
        raise HTTPException(status_code=400, detail="Nội dung yêu cầu khách hàng không được để trống.")
    master = load_master_data()
    req = RawCustomerRequest(
        request_id=payload.request_id.strip() or "YC-NHANH-001",
        source=payload.source,
        customer_name=payload.customer_name.strip(),
        contact_name=payload.contact_name.strip(),
        contact_email=payload.contact_email.strip(),
        customer_approver=payload.customer_approver.strip(),
        raw_text=payload.raw_text.strip(),
        stated_budget=payload.stated_budget,
        requested_deadline_days=payload.requested_deadline_days,
        requested_discount_pct=payload.requested_discount_pct,
    )
    return process_batch_requests(
        requests=[req],
        master=master,
        source_label=f"Phân tích nhanh yêu cầu ({req.request_id})",
        gemini_api_key=payload.gemini_api_key,
        model_name=payload.model_name,
    )


@app.post("/api/export/xlsx")
def export_results_xlsx(batch: BatchProcessResponse) -> Response:
    data = export_batch_to_xlsx_bytes(batch)
    return Response(
        content=data,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="MinhAn_BaoGia_{batch.batch_id}.xlsx"'},
    )


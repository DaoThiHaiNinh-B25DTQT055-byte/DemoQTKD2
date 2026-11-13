from __future__ import annotations

import io
from openpyxl import Workbook
from openpyxl.styles import Font, PatternFill

from app.models import BatchProcessResponse


def export_batch_to_xlsx_bytes(batch: BatchProcessResponse) -> bytes:
    """Export processed Opportunity Records, Line Items, and Draft Responses to an Excel workbook."""
    wb = Workbook()

    # Sheet 1: Ho_So_Co_Hoi
    ws_opp = wb.active
    ws_opp.title = "Ho_So_Co_Hoi"
    headers_opp = [
        "Mã Cơ Hội",
        "Mã Yêu Cầu",
        "Khách Hàng",
        "Nguồn",
        "NVKD Phụ Trách",
        "Trạng Thái Duyệt",
        "Người Phê Duyệt",
        "Giá Trị Trước CK (VND)",
        "CK Đề Nghị (%)",
        "Giá Trị Sau CK (VND)",
        "Ngân Sách KH (VND)",
        "Trạng Thái Ngân Sách",
        "Hạn Mong Muốn (Ngày)",
        "Khả Thi Thực Tế (Ngày)",
        "Trạng Thái Tiến Độ",
        "Mức Rủi Ro",
        "Cảnh Báo Chính",
        "Hành Động Tiếp Theo",
        "SLA",
    ]
    ws_opp.append(headers_opp)

    header_fill = PatternFill(start_color="0F172A", end_color="0F172A", fill_type="solid")
    header_font = Font(color="FFFFFF", bold=True)
    for cell in ws_opp[1]:
        cell.fill = header_fill
        cell.font = header_font

    for r in batch.results:
        ws_opp.append(
            [
                r.opportunity.opportunity_id,
                r.request_id,
                r.opportunity.customer_name,
                r.opportunity.source,
                r.opportunity.owner_name,
                r.opportunity.approval_status,
                r.opportunity.approver_required,
                r.subtotal_before_discount,
                r.applied_discount_pct,
                r.final_total,
                r.stated_budget or "",
                r.budget_status,
                r.requested_deadline_days or "",
                r.feasible_delivery_days,
                r.timeline_status,
                r.overall_risk_level,
                " | ".join(r.risk_flags),
                r.opportunity.next_action,
                r.opportunity.sla_deadline,
            ]
        )

    # Sheet 2: Chi_Tiet_Hang_Muc
    ws_items = wb.create_sheet("Chi_Tiet_Hang_Muc")
    ws_items.append(
        [
            "Mã Yêu Cầu",
            "Khách Hàng",
            "SKU",
            "Sản Phẩm / Dịch Vụ",
            "Số Lượng",
            "Đơn Vị",
            "Đơn Giá Hiệu Lực",
            "Thành Tiền",
            "Tồn Sẵn",
            "Trạng Thái Tồn",
            "Số Ngày Dự Kiến",
            "Cảnh Báo",
        ]
    )
    for cell in ws_items[1]:
        cell.fill = header_fill
        cell.font = header_font

    for r in batch.results:
        for it in r.line_items:
            ws_items.append(
                [
                    r.request_id,
                    r.opportunity.customer_name,
                    it.sku or "NGOÀI DANH MỤC",
                    it.product_name,
                    it.quantity,
                    it.unit,
                    it.unit_price,
                    it.line_subtotal,
                    it.available_stock,
                    it.stock_status,
                    it.estimated_ready_days,
                    " | ".join(it.alerts),
                ]
            )

    # Sheet 3: De_Xuat_Va_Phan_Hoi
    ws_draft = wb.create_sheet("De_Xuat_Va_Phan_Hoi")
    ws_draft.append(
        [
            "Mã Yêu Cầu",
            "Khách Hàng",
            "NVKD Phụ Trách",
            "Khuyến Nghị Kinh Doanh",
            "Tiêu Đề Email",
            "Bản Nháp Phản Hồi",
        ]
    )
    for cell in ws_draft[1]:
        cell.fill = header_fill
        cell.font = header_font

    for r in batch.results:
        ws_draft.append(
            [
                r.request_id,
                r.opportunity.customer_name,
                r.opportunity.owner_name,
                "\n".join(f"- {rec}" for rec in r.business_recommendations),
                r.draft_customer_subject,
                r.draft_customer_response,
            ]
        )

    for ws in wb.worksheets:
        for col in ws.columns:
            max_len = 0
            col_letter = col[0].column_letter
            for cell in col:
                val = str(cell.value or "")
                max_len = max(max_len, min(len(val), 46))
            ws.column_dimensions[col_letter].width = max(14, max_len + 3)

    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


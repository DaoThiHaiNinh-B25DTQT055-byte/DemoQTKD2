from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any
from uuid import uuid4

from app.extractor import extract_with_gemini_or_fallback
from app.ingestion import strip_accents
from app.models import (
    AnalyzedLineItem,
    BatchProcessResponse,
    ExtractionResult,
    MasterData,
    OpportunityRecord,
    ProductItem,
    ProposalAnalysis,
    RawCustomerRequest,
    SalesRep,
    WorkflowStepTrace,
)


def format_vnd(amount: float | None) -> str:
    if amount is None:
        return "Chưa xác định"
    return f"{round(amount):,}".replace(",", ".") + " đ"


def _assign_owner(
    req: RawCustomerRequest,
    matched_categories: list[str],
    reps: list[SalesRep],
    load_tracker: dict[str, int],
) -> SalesRep:
    if req.assigned_rep.strip():
        target = strip_accents(req.assigned_rep.strip().lower())
        for r in reps:
            if target in strip_accents(r.name.lower()) or target == r.id.lower():
                load_tracker[r.id] = load_tracker.get(r.id, r.active_weekly_load) + 1
                return r

    # Score by category match first, then lowest active load
    def score_rep(rep: SalesRep) -> tuple[int, int]:
        cat_hits = sum(1 for c in matched_categories if c in rep.specialty_categories)
        current_load = load_tracker.get(rep.id, rep.active_weekly_load)
        return (-cat_hits, current_load)

    chosen = min(reps, key=score_rep)
    load_tracker[chosen.id] = load_tracker.get(chosen.id, chosen.active_weekly_load) + 1
    return chosen


def _compute_recommended_max_discount(subtotal: float, master: MasterData) -> float:
    for tier in master.policy.volume_tiers:
        if float(tier["min_amount"]) <= subtotal < float(tier["max_amount"]):
            return float(tier["recommended_max_pct"])
    return master.policy.sales_rep_max_discount_pct


def _analyze_line_items_and_inventory(
    extraction: ExtractionResult,
    master: MasterData,
) -> tuple[
    list[AnalyzedLineItem],
    list[dict[str, Any]],
    list[str],
    list[str],
    list[str],
]:
    products_by_sku = {p.sku.upper(): p for p in master.products}
    analyzed: list[AnalyzedLineItem] = []
    unmatched_items: list[str] = []
    price_alerts: list[str] = []
    stock_alerts: list[str] = []
    requested_skus: set[str] = set()

    for ext_item in extraction.items:
        prod: ProductItem | None = (
            products_by_sku.get(ext_item.matched_sku.upper()) if ext_item.matched_sku else None
        )
        if prod is None:
            unmatched_items.append(f"{ext_item.quantity}x {ext_item.raw_mention}")
            analyzed.append(
                AnalyzedLineItem(
                    raw_mention=ext_item.raw_mention,
                    sku=None,
                    product_name=ext_item.raw_mention,
                    category="Ngoài danh mục chuẩn",
                    quantity=ext_item.quantity,
                    unit="Mục",
                    unit_price=0.0,
                    line_subtotal=0.0,
                    stock_status="Ngoài danh mục",
                    estimated_ready_days=0,
                    alerts=["Chưa có trong bảng giá chuẩn Minh An — cần Thu mua báo giá bổ sung để tránh bỏ sót."],
                )
            )
            continue

        requested_skus.add(prod.sku.upper())
        qty = max(1, ext_item.quantity)
        subtotal = prod.unit_price * qty
        item_alerts: list[str] = []

        # Price check: prevent outdated price usage
        if ext_item.cited_old_price is not None and abs(ext_item.cited_old_price - prod.unit_price) > 1:
            msg = (
                f"Khách nhắc đơn giá cũ ({format_vnd(ext_item.cited_old_price)}) cho {prod.name}; "
                f"đã tự động cập nhật theo bảng giá {master.price_list_version}: {format_vnd(prod.unit_price)}."
            )
            item_alerts.append(msg)
            price_alerts.append(msg)
        elif prod.previous_price and prod.previous_price < prod.unit_price:
            item_alerts.append(
                f"Áp dụng giá mới hiệu lực {prod.price_effective_date}: {format_vnd(prod.unit_price)} "
                f"(giá cũ {format_vnd(prod.previous_price)})."
            )

        # Stock & lead time check
        if qty <= prod.available_stock:
            stock_status = "Đủ tồn"
            ready_days = prod.deployment_days
        elif qty <= prod.available_stock + prod.incoming_stock:
            stock_status = "Thiếu một phần"
            ready_days = prod.incoming_eta_days + prod.deployment_days
            shortage = qty - prod.available_stock
            msg = (
                f"{prod.sku} ({prod.name}): cần {qty}, tồn sẵn {prod.available_stock} (thiếu {shortage}); "
                f"hàng đang về {prod.incoming_stock} sau {prod.incoming_eta_days} ngày -> cần {ready_days} ngày để giao đủ."
            )
            item_alerts.append(msg)
            stock_alerts.append(msg)
        else:
            stock_status = "Hết tồn"
            shortage = qty - prod.available_stock
            eta_fallback = max(prod.incoming_eta_days, 14)
            ready_days = eta_fallback + prod.deployment_days
            msg = (
                f"{prod.sku} ({prod.name}): cần {qty}, tồn sẵn {prod.available_stock}, đang về {prod.incoming_stock} "
                f"(còn thiếu {shortage}) -> cần đặt bổ sung tối thiểu {ready_days} ngày."
            )
            item_alerts.append(msg)
            stock_alerts.append(msg)

        analyzed.append(
            AnalyzedLineItem(
                raw_mention=ext_item.raw_mention,
                sku=prod.sku,
                product_name=prod.name,
                category=prod.category,
                quantity=qty,
                unit=prod.unit,
                unit_price=prod.unit_price,
                previous_price=prod.previous_price,
                cited_old_price=ext_item.cited_old_price,
                line_subtotal=subtotal,
                available_stock=prod.available_stock,
                incoming_stock=prod.incoming_stock,
                incoming_eta_days=prod.incoming_eta_days,
                stock_status=stock_status,  # type: ignore[arg-type]
                estimated_ready_days=ready_days,
                alerts=item_alerts,
            )
        )

    # Anti-omission check: companion deployment services
    companions_map: dict[str, dict[str, Any]] = {}
    for line in analyzed:
        if not line.sku:
            continue
        prod = products_by_sku.get(line.sku.upper())
        if not prod:
            continue
        if line.quantity >= prod.companion_min_qty:
            for comp_sku in prod.recommended_companions:
                if comp_sku.upper() not in requested_skus and comp_sku.upper() in products_by_sku:
                    comp = products_by_sku[comp_sku.upper()]
                    companions_map[comp.sku] = {
                        "sku": comp.sku,
                        "name": comp.name,
                        "unit_price": comp.unit_price,
                        "deployment_days": comp.deployment_days,
                        "reason": f"Đi kèm khi triển khai {line.quantity} {line.unit} {prod.name} (tránh bỏ sót chi phí kỹ thuật).",
                    }

    return analyzed, list(companions_map.values()), unmatched_items, price_alerts, stock_alerts


def _build_draft_response(
    req: RawCustomerRequest,
    extraction: ExtractionResult,
    line_items: list[AnalyzedLineItem],
    companions: list[dict[str, Any]],
    unmatched_items: list[str],
    owner: SalesRep,
    approval_status: str,
    approver_required: str,
    subtotal: float,
    applied_discount_pct: float,
    requested_discount_pct: float,
    final_total: float,
    budget_status: str,
    budget_gap: float | None,
    timeline_status: str,
    feasible_days: int,
    recommendations: list[str],
) -> tuple[str, str]:
    greeting_name = extraction.contact_name or extraction.customer_name
    subject = f"[Minh An Tech] Phản hồi & Đề xuất báo giá cho yêu cầu {req.request_id} - {extraction.customer_name}"

    lines: list[str] = [
        f"Kính gửi {greeting_name},",
        "",
        f"Cảm ơn Quý khách ({extraction.customer_name}) đã gửi yêu cầu báo giá mã {req.request_id} tới Công ty Cổ phần Công nghệ Minh An.",
    ]

    if extraction.missing_fields:
        lines.extend(
            [
                "",
                "Để Minh An lập phương án cấu hình và báo giá tối ưu nhất trong một lần phản hồi, kính nhờ Anh/Chị bổ sung giúp một số thông tin sau:",
            ]
        )
        for mf in extraction.missing_fields:
            lines.append(f"- {mf}")

    lines.extend(["", "1. Tóm tắt hạng mục & đơn giá hiệu lực hiện hành:"])
    for idx, item in enumerate(line_items, start=1):
        if item.sku:
            lines.append(
                f"- {idx}. {item.product_name} ({item.sku}) | SL: {item.quantity} {item.unit} | "
                f"Đơn giá: {format_vnd(item.unit_price)} | Thành tiền: {format_vnd(item.line_subtotal)} "
                f"[{item.stock_status}, dự kiến {item.estimated_ready_days} ngày]"
            )
        else:
            lines.append(
                f"- {idx}. {item.product_name} | SL: {item.quantity} | Đang kiểm tra giá & nguồn hàng bổ sung (chưa thuộc danh mục chuẩn)"
            )

    lines.extend(
        [
            "",
            f"2. Tổng hợp tài chính (chưa gồm hạng mục ngoài danh mục nếu có):",
            f"- Tổng giá trị theo bảng giá hiệu lực: {format_vnd(subtotal)}",
            f"- Mức chiết khấu đề xuất áp dụng: {applied_discount_pct:.1f}% (Khách hàng đề nghị: {requested_discount_pct:.1f}%)",
            f"- Tổng giá trị sau chiết khấu (dự kiến): {format_vnd(final_total)}",
        ]
    )

    if extraction.stated_budget is not None:
        if budget_status == "Đạt ngân sách":
            lines.append(f"- Đối chiếu ngân sách ({format_vnd(extraction.stated_budget)}): Phù hợp trong ngân sách dự kiến.")
        else:
            lines.append(
                f"- Đối chiếu ngân sách ({format_vnd(extraction.stated_budget)}): Đang vượt khoảng {format_vnd(abs(budget_gap or 0))}. "
                "Minh An đề xuất trao đổi phương án tối ưu cấu hình/chia giai đoạn để khớp ngân sách."
            )

    if extraction.requested_deadline_days is not None:
        lines.append(
            f"3. Tiến độ triển khai: Khách hàng mong muốn {extraction.requested_deadline_days} ngày | "
            f"Khả năng đáp ứng thực tế của Minh An: {feasible_days} ngày ({timeline_status})."
        )
        if timeline_status != "Đáp ứng kịp":
            lines.append(
                "- Đề xuất: Có thể ưu tiên giao Đợt 1 các thiết bị có sẵn tại kho ngay, và bàn giao phần còn lại theo lịch nhập hàng để đảm bảo vận hành sớm."
            )

    if companions:
        lines.append("")
        lines.append("4. Lưu ý hạng mục dịch vụ đi kèm khuyến nghị (tránh phát sinh khi triển khai):")
        for c in companions:
            lines.append(f"- {c['name']} ({c['sku']}): {format_vnd(c['unit_price'])} — {c['reason']}")

    if unmatched_items:
        lines.append("")
        lines.append(
            "5. Hạng mục đặt riêng ngoài danh mục chuẩn ("
            + ", ".join(unmatched_items)
            + "): Bộ phận Thu mua đang kiểm tra giá hãng và sẽ cập nhật trong báo giá chính thức."
        )

    if approval_status != "Tự duyệt (NVKD)":
        lines.append("")
        lines.append(
            f"*Lưu ý nội bộ trước khi gửi:* Hồ sơ đang ở trạng thái [{approval_status}] — "
            f"cần xác nhận từ {approver_required} trước khi phát hành cam kết chiết khấu/tiến độ cuối cùng."
        )
    elif recommendations:
        lines.append("")
        lines.append("Gợi ý triển khai từ Minh An: " + recommendations[0])

    lines.extend(
        [
            "",
            f"Đầu mối phụ trách trực tiếp hồ sơ này: {owner.name} ({owner.email}).",
            "Trân trọng,",
            f"{owner.name} - Phòng Kinh doanh Công ty Cổ phần Công nghệ Minh An",
        ]
    )
    return subject, "\n".join(lines)


def process_single_request(
    req: RawCustomerRequest,
    master: MasterData,
    load_tracker: dict[str, int],
    gemini_api_key: str | None = None,
    model_name: str = "gemini-3.7-flash",
) -> ProposalAnalysis:
    """Execute the 6-step AI Sales Workflow for a single customer request."""
    trace: list[WorkflowStepTrace] = []

    # Step 1: Ingest & Normalize
    trace.append(
        WorkflowStepTrace(
            step_number=1,
            step_code="INGEST_NORMALIZE",
            step_name="Tiếp nhận & Chuẩn hóa yêu cầu",
            status="completed",
            summary=f"Đã tiếp nhận mã {req.request_id} từ nguồn '{req.source}'.",
            details=[
                f"Thời gian nhận: {req.received_at or 'Hiện tại'}",
                f"Độ dài nội dung văn bản: {len(req.raw_text)} ký tự",
            ],
        )
    )

    # Step 2: AI Extraction & Completeness Audit
    extraction = extract_with_gemini_or_fallback(
        req=req,
        master=master,
        gemini_api_key=gemini_api_key,
        model_name=model_name,
    )
    step2_status = "warning" if (extraction.missing_fields or extraction.ambiguities) else "completed"
    trace.append(
        WorkflowStepTrace(
            step_number=2,
            step_code="AI_EXTRACT_AUDIT",
            step_name="AI Trích xuất hạng mục & Kiểm tra thiếu thông tin",
            status=step2_status,  # type: ignore[arg-type]
            summary=f"Trích xuất {len(extraction.items)} hạng mục (chế độ: {extraction.extraction_mode}); phát hiện {len(extraction.missing_fields)} trường thiếu.",
            details=(extraction.missing_fields + extraction.ambiguities) or ["Đủ thông tin đầu vào cơ bản."],
        )
    )

    # Step 3: Catalog & Active Pricing Verification
    line_items, companions, unmatched_items, price_alerts, stock_alerts = _analyze_line_items_and_inventory(
        extraction, master
    )
    matched_categories = [it.category for it in line_items if it.sku]
    subtotal = sum(it.line_subtotal for it in line_items)

    step3_alerts = price_alerts + ([f"Có {len(unmatched_items)} hạng mục ngoài danh mục chuẩn"] if unmatched_items else []) + (
        [f"Gợi ý {len(companions)} dịch vụ đi kèm tránh bỏ sót"] if companions else []
    )
    trace.append(
        WorkflowStepTrace(
            step_number=3,
            step_code="CATALOG_PRICING_CHECK",
            step_name="Đối chiếu Danh mục chuẩn & Bảng giá hiệu lực",
            status="warning" if step3_alerts else "completed",
            summary=f"Tạm tính theo bảng giá {master.price_list_version}: {format_vnd(subtotal)}.",
            details=step3_alerts or ["100% hạng mục khớp bảng giá hiệu lực mới nhất, không phát hiện dùng giá cũ."],
        )
    )

    # Step 4: Inventory & Deployment Timeline Check
    matched_ready_days = [it.estimated_ready_days for it in line_items if it.sku]
    feasible_days = max(matched_ready_days, default=0)
    has_stock_shortage = any(it.stock_status in ("Thiếu một phần", "Hết tồn") for it in line_items)
    has_partial_deliverable = any(it.available_stock > 0 for it in line_items if it.sku)

    req_days = extraction.requested_deadline_days
    if req_days is None:
        timeline_status = "Chưa rõ thời hạn"
    elif feasible_days <= req_days:
        timeline_status = "Đáp ứng kịp"
    elif has_stock_shortage and has_partial_deliverable:
        timeline_status = "Cần giao từng phần"
    else:
        timeline_status = "Không kịp hạn"

    step4_details = stock_alerts.copy()
    if req_days is not None:
        step4_details.append(f"Khách hàng yêu cầu: {req_days} ngày | Thực tế Minh An đáp ứng đủ: {feasible_days} ngày -> {timeline_status}.")
    else:
        step4_details.append(f"Thời gian triển khai ước tính: {feasible_days} ngày (chưa có hạn mong muốn từ khách hàng).")
    trace.append(
        WorkflowStepTrace(
            step_number=4,
            step_code="INVENTORY_TIMELINE_CHECK",
            step_name="Kiểm tra Tồn khả dụng & Khả năng đáp ứng tiến độ",
            status="action_required" if timeline_status in ("Không kịp hạn", "Cần giao từng phần") else ("warning" if has_stock_shortage or req_days is None else "completed"),
            summary=f"Tồn kho & Tiến độ: {timeline_status} ({feasible_days} ngày khả thi).",
            details=step4_details,
        )
    )

    # Step 5: Discount Policy, Budget Fit & Approval Routing (Zero Unowned Bouncing)
    owner = _assign_owner(req, matched_categories, master.sales_reps, load_tracker)
    requested_discount = extraction.requested_discount_pct or 0.0
    recommended_max_discount = _compute_recommended_max_discount(subtotal, master)
    applied_discount = requested_discount
    discount_amount = subtotal * (applied_discount / 100.0)
    final_total = max(0.0, subtotal - discount_amount)

    budget = extraction.stated_budget
    if budget is None:
        budget_status = "Thiếu ngân sách"
        budget_gap = None
    else:
        budget_gap = round(final_total - budget, 0)
        budget_status = "Đạt ngân sách" if final_total <= budget else "Vượt ngân sách"

    risk_flags: list[str] = []
    if price_alerts:
        risk_flags.append("Phát hiện khách hàng nhắc giá cũ")
    if unmatched_items:
        risk_flags.append(f"Có {len(unmatched_items)} hạng mục ngoài danh mục")
    if companions:
        risk_flags.append(f"Nguy cơ bỏ sót {len(companions)} dịch vụ triển khai đi kèm")
    if has_stock_shortage:
        risk_flags.append("Thiếu tồn khả dụng tại kho")
    if timeline_status in ("Không kịp hạn", "Cần giao từng phần"):
        risk_flags.append(f"Rủi ro tiến độ ({timeline_status}: cần {feasible_days} ngày vs yêu cầu {req_days} ngày)")
    if budget_status == "Vượt ngân sách":
        risk_flags.append(f"Vượt ngân sách {format_vnd(abs(budget_gap or 0))}")
    if extraction.missing_fields:
        risk_flags.extend(extraction.missing_fields)

    policy = master.policy
    needs_clarification = bool(
        budget is None or not extraction.customer_approver.strip() or not any(it.sku for it in line_items)
    )

    if requested_discount > policy.manager_max_discount_pct:
        if needs_clarification:
            approval_status = "Chờ KH bổ sung thông tin"
            approver_required = f"{policy.director_name} (sau khi KH bổ sung đủ thông tin)"
            next_action = (
                f"{owner.name} liên hệ KH hỏi rõ ngân sách & người phê duyệt trong {policy.default_clarification_sla_hours}h; "
                f"đồng thời trình {policy.director_name} xem xét mức chiết khấu vượt trần ({requested_discount:.1f}% > {policy.manager_max_discount_pct:.1f}%)."
            )
        else:
            approval_status = "Chờ Ban Giám đốc duyệt"
            approver_required = policy.director_name
            next_action = (
                f"{owner.name} hoàn thiện hồ sơ giải trình biên lợi nhuận và trình {policy.director_name} "
                f"duyệt chiết khấu {requested_discount:.1f}% (vượt trần TP KD {policy.manager_max_discount_pct:.1f}%)."
            )
    elif needs_clarification:
        approval_status = "Chờ KH bổ sung thông tin"
        approver_required = (
            policy.sales_manager_name
            if requested_discount > policy.sales_rep_max_discount_pct or has_stock_shortage
            else owner.name
        )
        next_action = (
            f"{owner.name} chịu trách nhiệm duy nhất gửi email làm rõ {', '.join(extraction.missing_fields)} "
            f"trong {policy.default_clarification_sla_hours}h và theo dõi lại sau {policy.follow_up_hours}h."
        )
    elif (
        requested_discount > policy.sales_rep_max_discount_pct
        or timeline_status in ("Không kịp hạn", "Cần giao từng phần")
        or budget_status == "Vượt ngân sách"
        or has_stock_shortage
        or final_total >= policy.director_approval_contract_threshold
    ):
        if final_total >= policy.director_approval_contract_threshold and requested_discount > policy.sales_rep_max_discount_pct:
            approval_status = "Chờ Trưởng phòng KD duyệt"
            approver_required = f"{policy.sales_manager_name} (đồng thông báo BGĐ do đơn >= {format_vnd(policy.director_approval_contract_threshold)})"
        else:
            approval_status = "Chờ Trưởng phòng KD duyệt"
            approver_required = policy.sales_manager_name
        reasons = []
        if requested_discount > policy.sales_rep_max_discount_pct:
            reasons.append(f"chiết khấu {requested_discount:.1f}% > trần NVKD {policy.sales_rep_max_discount_pct:.1f}%")
        if has_stock_shortage or timeline_status != "Đáp ứng kịp":
            reasons.append(f"tồn kho / tiến độ ({timeline_status})")
        if budget_status == "Vượt ngân sách":
            reasons.append("phương án điều chỉnh vượt ngân sách")
        next_action = (
            f"{owner.name} trình {policy.sales_manager_name} duyệt ngoại lệ ({'; '.join(reasons)}) "
            f"trước khi gửi cam kết chính thức."
        )
    else:
        approval_status = "Tự duyệt (NVKD)"
        approver_required = f"{owner.name} (Trong hạn mức NVKD <= {policy.sales_rep_max_discount_pct:.1f}%)"
        next_action = f"{owner.name} kiểm tra nhanh bản nháp và gửi báo giá chính thức cho khách hàng."

    if requested_discount > policy.manager_max_discount_pct:
        risk_flags.append(
            f"Chiết khấu đề nghị {requested_discount:.1f}% vượt trần TP KD ({policy.manager_max_discount_pct:.1f}%)"
        )
    elif requested_discount > policy.sales_rep_max_discount_pct:
        risk_flags.append(
            f"Chiết khấu đề nghị {requested_discount:.1f}% vượt quyền NVKD ({policy.sales_rep_max_discount_pct:.1f}%)"
        )

    if len(risk_flags) >= 2 or approval_status in ("Chờ Ban Giám đốc duyệt", "Chờ KH bổ sung thông tin") or timeline_status == "Không kịp hạn":
        overall_risk = "Cao"
    elif risk_flags or approval_status == "Chờ Trưởng phòng KD duyệt":
        overall_risk = "Trung bình"
    else:
        overall_risk = "Thấp"

    sla_dt = datetime.now() + timedelta(hours=policy.default_clarification_sla_hours)
    sla_deadline = f"Trong {policy.default_clarification_sla_hours}h (trước {sla_dt.strftime('%H:%M %d/%m')}) | Follow-up: +{policy.follow_up_hours}h"

    trace.append(
        WorkflowStepTrace(
            step_number=5,
            step_code="POLICY_APPROVAL_ROUTING",
            step_name="Kiểm tra Chính sách Chiết khấu, Ngân sách & Định tuyến Phê duyệt",
            status="completed" if approval_status == "Tự duyệt (NVKD)" else "action_required",
            summary=f"Trạng thái: {approval_status} | Owner: {owner.name} | Phê duyệt: {approver_required}.",
            details=[
                f"Giá trị sau CK ({applied_discount:.1f}%): {format_vnd(final_total)} | Ngân sách: {budget_status}",
                f"Hành động tiếp theo: {next_action}",
            ],
        )
    )

    # Step 6: Business Recommendations, Opportunity Record & Draft Customer Reply
    recommendations: list[str] = []
    if price_alerts:
        recommendations.append(
            f"Giải thích rõ với khách hàng bảng giá đã cập nhật phiên bản {master.price_list_version}; "
            "cân nhắc giữ mức chiết khấu khuyến nghị để bù chênh lệch thay vì áp đơn giá cũ."
        )
    if companions:
        comp_names = ", ".join(c["name"] for c in companions)
        recommendations.append(
            f"Đề xuất bổ sung rõ hạng mục dịch vụ đi kèm ({comp_names}) thành tùy chọn tách dòng để khách không bị phát sinh chi phí ngoài dự kiến."
        )
    if timeline_status in ("Cần giao từng phần", "Không kịp hạn"):
        recommendations.append(
            f"Đề xuất phương án giao 2 đợt: Đợt 1 giao ngay lượng tồn sẵn tại kho trong 2-3 ngày, "
            f"Đợt 2 hoàn tất phần còn lại sau {feasible_days} ngày để đáp ứng vận hành sớm."
        )
    if budget_status == "Vượt ngân sách" and budget_gap:
        recommendations.append(
            f"Báo giá đang vượt ngân sách {format_vnd(budget_gap)}. Gợi ý giảm nhẹ số lượng thiết bị phụ trợ hoặc tách phần dịch vụ/mở rộng sang giai đoạn 2, hoặc trình TP KD duyệt nâng chiết khấu lên {min(recommended_max_discount + 2, 10):.1f}%."
        )
    if unmatched_items:
        recommendations.append(
            f"Chuyển ngay bộ phận Thu mua lấy báo giá nhà phân phối cho: {', '.join(unmatched_items)} trong vòng 2 giờ."
        )
    if requested_discount > recommended_max_discount:
        recommendations.append(
            f"Mức chiết khấu khuyến nghị theo quy mô đơn là {recommended_max_discount:.1f}% (khách đang đề nghị {requested_discount:.1f}%). "
            "Nếu giữ nguyên mức khách đề nghị cần giải trình biên lợi nhuận hoặc cam kết thanh toán 100% trước giao hàng."
        )
    if not recommendations:
        recommendations.append("Hồ sơ đạt chuẩn 100%: đủ tồn, đúng giá mới, kịp tiến độ và trong hạn mức tự duyệt — có thể chốt báo giá gửi khách ngay.")

    opportunity = OpportunityRecord(
        opportunity_id=f"OPP-{req.request_id}",
        request_id=req.request_id,
        source=req.source,
        received_at=req.received_at,
        customer_name=extraction.customer_name,
        contact_info=f"{extraction.contact_name} ({extraction.contact_email})".strip(" ()") or "Chưa có thông tin liên hệ",
        customer_approver=extraction.customer_approver or "Chưa xác định (Cần làm rõ)",
        owner_name=owner.name,
        owner_email=owner.email,
        approver_required=approver_required,
        approval_status=approval_status,  # type: ignore[arg-type]
        sla_deadline=sla_deadline,
        next_action=next_action,
    )

    draft_subject, draft_body = _build_draft_response(
        req=req,
        extraction=extraction,
        line_items=line_items,
        companions=companions,
        unmatched_items=unmatched_items,
        owner=owner,
        approval_status=approval_status,
        approver_required=approver_required,
        subtotal=subtotal,
        applied_discount_pct=applied_discount,
        requested_discount_pct=requested_discount,
        final_total=final_total,
        budget_status=budget_status,
        budget_gap=budget_gap,
        timeline_status=timeline_status,
        feasible_days=feasible_days,
        recommendations=recommendations,
    )

    trace.append(
        WorkflowStepTrace(
            step_number=6,
            step_code="PROPOSAL_DRAFT_OUTPUT",
            step_name="Tạo Hồ sơ Cơ hội, Đề xuất Kinh doanh & Bản nháp Phản hồi",
            status="completed",
            summary=f"Đã sinh Hồ sơ {opportunity.opportunity_id} & bản nháp phản hồi khách hàng sẵn sàng chỉnh sửa.",
            details=[
                f"Số khuyến nghị kinh doanh: {len(recommendations)}",
                f"Mức rủi ro tổng thể: {overall_risk}",
            ],
        )
    )

    return ProposalAnalysis(
        request_id=req.request_id,
        opportunity=opportunity,
        extraction=extraction,
        line_items=line_items,
        recommended_companions=companions,
        unmatched_items=unmatched_items,
        subtotal_before_discount=round(subtotal, 0),
        requested_discount_pct=requested_discount,
        applied_discount_pct=applied_discount,
        recommended_max_discount_pct=recommended_max_discount,
        discount_amount=round(discount_amount, 0),
        final_total=round(final_total, 0),
        stated_budget=budget,
        budget_gap=budget_gap,
        budget_status=budget_status,  # type: ignore[arg-type]
        requested_deadline_days=req_days,
        feasible_delivery_days=feasible_days,
        timeline_status=timeline_status,  # type: ignore[arg-type]
        overall_risk_level=overall_risk,  # type: ignore[arg-type]
        risk_flags=risk_flags,
        business_recommendations=recommendations,
        draft_customer_subject=draft_subject,
        draft_customer_response=draft_body,
        workflow_trace=trace,
    )


def process_batch_requests(
    requests: list[RawCustomerRequest],
    master: MasterData,
    source_label: str,
    gemini_api_key: str | None = None,
    model_name: str = "gemini-3.7-flash",
) -> BatchProcessResponse:
    """Process a batch of customer requests through the 6-step Minh An Sales Agent Workflow."""
    load_tracker = {r.id: r.active_weekly_load for r in master.sales_reps}
    results: list[ProposalAnalysis] = [
        process_single_request(
            req=req,
            master=master,
            load_tracker=load_tracker,
            gemini_api_key=gemini_api_key,
            model_name=model_name,
        )
        for req in requests
    ]

    total_pipeline = sum(r.final_total for r in results)
    auto_approved = sum(1 for r in results if r.opportunity.approval_status == "Tự duyệt (NVKD)")
    manager_review = sum(1 for r in results if r.opportunity.approval_status == "Chờ Trưởng phòng KD duyệt")
    director_review = sum(1 for r in results if r.opportunity.approval_status == "Chờ Ban Giám đốc duyệt")
    clarification_needed = sum(1 for r in results if r.opportunity.approval_status == "Chờ KH bổ sung thông tin")
    high_risk_count = sum(1 for r in results if r.overall_risk_level == "Cao")
    old_price_catches = sum(1 for r in results if any("giá cũ" in f.lower() for f in r.risk_flags))
    omission_catches = sum(1 for r in results if r.recommended_companions or r.unmatched_items)
    llm_modes = sorted({r.extraction.extraction_mode for r in results})

    workflow_overview = [
        {
            "step": 1,
            "name": "Tiếp nhận & Chuẩn hóa",
            "description": "Đọc Google Sheets / .csv / .xlsx, chuẩn hóa nguồn Email & Biểu mẫu.",
            "passed": len(results),
            "flagged": 0,
        },
        {
            "step": 2,
            "name": "AI Trích xuất & Audit",
            "description": "Tách đa sản phẩm, ngân sách, hạn, chiết khấu và phát hiện thông tin thiếu.",
            "passed": sum(1 for r in results if not r.extraction.missing_fields),
            "flagged": sum(1 for r in results if r.extraction.missing_fields),
        },
        {
            "step": 3,
            "name": "Bảng giá & Chống sót mục",
            "description": f"Áp dụng giá mới {master.price_list_version}, chặn giá cũ & gợi ý dịch vụ kèm.",
            "passed": len(results) - old_price_catches - omission_catches,
            "flagged": old_price_catches + omission_catches,
        },
        {
            "step": 4,
            "name": "Tồn kho & Tiến độ",
            "description": "Đối chiếu tồn sẵn/đang về & tổng ngày triển khai với hạn khách hàng.",
            "passed": sum(1 for r in results if r.timeline_status == "Đáp ứng kịp"),
            "flagged": sum(1 for r in results if r.timeline_status != "Đáp ứng kịp"),
        },
        {
            "step": 5,
            "name": "Chính sách & Gán Owner",
            "description": "Định tuyến NVKD / TP KD / BGĐ, chống chuyển qua lại vô chủ.",
            "passed": auto_approved,
            "flagged": len(results) - auto_approved,
        },
        {
            "step": 6,
            "name": "Hồ sơ Cơ hội & Bản nháp",
            "description": "Xuất hồ sơ CRM, đề xuất tối ưu kinh doanh & email phản hồi.",
            "passed": len(results),
            "flagged": 0,
        },
    ]

    return BatchProcessResponse(
        batch_id=f"BATCH-{uuid4().hex[:8].upper()}",
        source_label=source_label,
        processed_at=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        price_list_version=master.price_list_version,
        llm_mode_used=", ".join(llm_modes) if llm_modes else "hybrid_rules",
        summary_metrics={
            "total_requests": len(results),
            "total_pipeline_vnd": round(total_pipeline, 0),
            "auto_approved_count": auto_approved,
            "manager_review_count": manager_review,
            "director_review_count": director_review,
            "clarification_needed_count": clarification_needed,
            "high_risk_count": high_risk_count,
            "old_price_catches": old_price_catches,
            "omission_catches": omission_catches,
        },
        workflow_overview=workflow_overview,
        results=results,
    )


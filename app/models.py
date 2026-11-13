from __future__ import annotations

from typing import Any, Literal
from pydantic import BaseModel, Field


class ProductItem(BaseModel):
    sku: str
    name: str
    category: str
    unit: str = "Bộ"
    unit_price: float
    previous_price: float | None = None
    price_effective_date: str = "2026-11-01"
    available_stock: int = 0
    incoming_stock: int = 0
    incoming_eta_days: int = 0
    deployment_days: int = 1
    aliases: list[str] = Field(default_factory=list)
    recommended_companions: list[str] = Field(default_factory=list)
    companion_min_qty: int = 1


class SalesRep(BaseModel):
    id: str
    name: str
    email: str
    specialty_categories: list[str] = Field(default_factory=list)
    active_weekly_load: int = 0


class DiscountPolicy(BaseModel):
    sales_rep_max_discount_pct: float = 5.0
    manager_max_discount_pct: float = 12.0
    director_approval_contract_threshold: float = 800_000_000.0
    sales_manager_name: str = "Ông Nguyễn Quốc Bảo (Trưởng phòng Kinh doanh)"
    director_name: str = "Bà Trần Hải Yến (Ban Giám đốc)"
    default_clarification_sla_hours: int = 4
    follow_up_hours: int = 24
    volume_tiers: list[dict[str, Any]] = Field(default_factory=list)


class MasterData(BaseModel):
    company_name: str = "Công ty Cổ phần Công nghệ Minh An"
    price_list_version: str = "BG-2026.11"
    products: list[ProductItem] = Field(default_factory=list)
    sales_reps: list[SalesRep] = Field(default_factory=list)
    policy: DiscountPolicy = Field(default_factory=DiscountPolicy)


class RawCustomerRequest(BaseModel):
    request_id: str
    received_at: str = ""
    source: str = "Email"
    customer_name: str = ""
    contact_name: str = ""
    contact_email: str = ""
    customer_approver: str = ""
    assigned_rep: str = ""
    raw_text: str
    stated_budget: float | None = None
    requested_deadline_days: int | None = None
    requested_discount_pct: float | None = None
    notes: str = ""


class ExtractedLineItem(BaseModel):
    raw_mention: str
    quantity: int = 1
    cited_old_price: float | None = None
    matched_sku: str | None = None
    matched_name: str | None = None
    confidence: float = 1.0


class ExtractionResult(BaseModel):
    customer_name: str
    contact_name: str = ""
    contact_email: str = ""
    customer_approver: str = ""
    stated_budget: float | None = None
    requested_deadline_days: int | None = None
    requested_discount_pct: float | None = None
    items: list[ExtractedLineItem] = Field(default_factory=list)
    missing_fields: list[str] = Field(default_factory=list)
    ambiguities: list[str] = Field(default_factory=list)
    extraction_mode: Literal["gemini", "hybrid_rules"] = "hybrid_rules"
    raw_summary: str = ""


class AnalyzedLineItem(BaseModel):
    raw_mention: str
    sku: str | None = None
    product_name: str
    category: str = "Chưa phân loại"
    quantity: int
    unit: str = "Bộ"
    unit_price: float = 0.0
    previous_price: float | None = None
    cited_old_price: float | None = None
    line_subtotal: float = 0.0
    available_stock: int = 0
    incoming_stock: int = 0
    incoming_eta_days: int = 0
    stock_status: Literal["Đủ tồn", "Thiếu một phần", "Hết tồn", "Ngoài danh mục"] = "Đủ tồn"
    estimated_ready_days: int = 0
    alerts: list[str] = Field(default_factory=list)


class WorkflowStepTrace(BaseModel):
    step_number: int
    step_code: str
    step_name: str
    status: Literal["completed", "warning", "action_required"]
    summary: str
    details: list[str] = Field(default_factory=list)


class OpportunityRecord(BaseModel):
    opportunity_id: str
    request_id: str
    source: str
    received_at: str
    customer_name: str
    contact_info: str
    customer_approver: str
    owner_name: str
    owner_email: str
    approver_required: str
    approval_status: Literal[
        "Tự duyệt (NVKD)",
        "Chờ Trưởng phòng KD duyệt",
        "Chờ Ban Giám đốc duyệt",
        "Chờ KH bổ sung thông tin",
    ]
    sla_deadline: str
    next_action: str


class ProposalAnalysis(BaseModel):
    request_id: str
    opportunity: OpportunityRecord
    extraction: ExtractionResult
    line_items: list[AnalyzedLineItem] = Field(default_factory=list)
    recommended_companions: list[dict[str, Any]] = Field(default_factory=list)
    unmatched_items: list[str] = Field(default_factory=list)
    subtotal_before_discount: float = 0.0
    requested_discount_pct: float = 0.0
    applied_discount_pct: float = 0.0
    recommended_max_discount_pct: float = 0.0
    discount_amount: float = 0.0
    final_total: float = 0.0
    stated_budget: float | None = None
    budget_gap: float | None = None
    budget_status: Literal["Đạt ngân sách", "Vượt ngân sách", "Thiếu ngân sách"] = "Thiếu ngân sách"
    requested_deadline_days: int | None = None
    feasible_delivery_days: int = 0
    timeline_status: Literal["Đáp ứng kịp", "Cần giao từng phần", "Không kịp hạn", "Chưa rõ thời hạn"] = "Chưa rõ thời hạn"
    overall_risk_level: Literal["Thấp", "Trung bình", "Cao"] = "Thấp"
    risk_flags: list[str] = Field(default_factory=list)
    business_recommendations: list[str] = Field(default_factory=list)
    draft_customer_subject: str = ""
    draft_customer_response: str = ""
    workflow_trace: list[WorkflowStepTrace] = Field(default_factory=list)


class BatchProcessResponse(BaseModel):
    batch_id: str
    source_label: str
    processed_at: str
    price_list_version: str
    llm_mode_used: str
    summary_metrics: dict[str, Any]
    workflow_overview: list[dict[str, Any]]
    results: list[ProposalAnalysis]


class SingleRequestPayload(BaseModel):
    request_id: str = "YC-NHANH-001"
    customer_name: str = ""
    contact_name: str = ""
    contact_email: str = ""
    customer_approver: str = ""
    source: str = "Biểu mẫu Web"
    raw_text: str
    stated_budget: float | None = None
    requested_deadline_days: int | None = None
    requested_discount_pct: float | None = None
    gemini_api_key: str | None = None
    model_name: str = "gemini-3.7-flash"


class GoogleSheetImportPayload(BaseModel):
    sheet_url: str
    gemini_api_key: str | None = None
    model_name: str = "gemini-3.7-flash"
    use_for: Literal["requests", "master_data", "auto"] = "auto"


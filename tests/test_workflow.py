from __future__ import annotations

from app.ingestion import extract_requests_from_tables, parse_csv_to_tables
from app.master_data import SAMPLE_CSV_PATH, load_master_data
from app.workflow import process_batch_requests


def test_minh_an_sample_workflow_solves_all_pain_points() -> None:
    master = load_master_data()
    tables = parse_csv_to_tables(SAMPLE_CSV_PATH.read_bytes())
    requests = extract_requests_from_tables(tables)

    batch = process_batch_requests(
        requests=requests,
        master=master,
        source_label="Unit Test Sample",
    )

    assert batch.summary_metrics["total_requests"] == 6
    by_id = {r.request_id: r for r in batch.results}

    # 1. Clean auto-approved request
    r1 = by_id["YC-2026-001"]
    assert r1.opportunity.approval_status == "Tự duyệt (NVKD)"
    assert r1.budget_status == "Đạt ngân sách"
    assert r1.timeline_status == "Đáp ứng kịp"
    assert r1.opportunity.owner_name != ""

    # 2. Old price detection + companion service check + manager escalation (8% > 5%)
    r2 = by_id["YC-2026-002"]
    assert any("giá cũ" in f.lower() for f in r2.risk_flags)
    assert any(c["sku"] == "MA-SVC-NET" for c in r2.recommended_companions)
    assert r2.opportunity.approval_status == "Chờ Trưởng phòng KD duyệt"
    dell_line = next(it for it in r2.line_items if it.sku == "MA-LAP-01")
    assert dell_line.unit_price == 24_500_000
    assert dell_line.cited_old_price == 23_200_000

    # 3. Stock shortage + infeasible deadline (4 days requested vs 19 days feasible)
    r3 = by_id["YC-2026-003"]
    assert r3.timeline_status in ("Cần giao từng phần", "Không kịp hạn")
    assert r3.feasible_delivery_days >= 19
    assert any("tồn" in f.lower() for f in r3.risk_flags)
    assert any(c["sku"] == "MA-SVC-SRV" for c in r3.recommended_companions)

    # 4. Missing budget + missing approver + 15% discount -> must assign Owner + SLA + clarification
    r4 = by_id["YC-2026-004"]
    assert r4.opportunity.approval_status == "Chờ KH bổ sung thông tin"
    assert r4.opportunity.owner_name != ""
    assert "Ban Giám đốc" in r4.opportunity.approver_required
    assert r4.budget_status == "Thiếu ngân sách"

    # 5. Over budget detection
    r5 = by_id["YC-2026-005"]
    assert r5.budget_status == "Vượt ngân sách"
    assert r5.budget_gap is not None and r5.budget_gap > 0
    assert any("vượt ngân sách" in rec.lower() for rec in r5.business_recommendations)

    # 6. Out-of-catalog item preserved (never omitted)
    r6 = by_id["YC-2026-006"]
    assert any("Zebra ZC300" in u for u in r6.unmatched_items)
    assert any(it.stock_status == "Ngoài danh mục" for it in r6.line_items)


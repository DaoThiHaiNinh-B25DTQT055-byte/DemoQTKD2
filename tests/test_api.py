from __future__ import annotations

from fastapi.testclient import TestClient

from app.main import app
from app.master_data import SAMPLE_CSV_PATH, build_sample_workspace_xlsx_bytes

client = TestClient(app)


def test_health_and_master_data_endpoints() -> None:
    resp = client.get("/api/health")
    assert resp.status_code == 200
    payload = resp.json()
    assert payload["status"] == "ok"
    assert payload["sales_rep_count"] == 6

    md = client.get("/api/master-data")
    assert md.status_code == 200
    assert len(md.json()["products"]) == 14


def test_sample_process_and_export_xlsx() -> None:
    resp = client.post("/api/process/sample")
    assert resp.status_code == 200
    batch = resp.json()
    assert batch["summary_metrics"]["total_requests"] == 6
    assert len(batch["workflow_overview"]) == 6

    exp = client.post("/api/export/xlsx", json=batch)
    assert exp.status_code == 200
    assert exp.content[:2] == b"PK"


def test_upload_csv_and_xlsx_endpoints() -> None:
    resp_csv = client.post(
        "/api/process/upload",
        files={"file": ("requests.csv", SAMPLE_CSV_PATH.read_bytes(), "text/csv")},
    )
    assert resp_csv.status_code == 200
    assert resp_csv.json()["summary_metrics"]["total_requests"] == 6

    xlsx_bytes = build_sample_workspace_xlsx_bytes()
    resp_xlsx = client.post(
        "/api/process/upload",
        files={
            "file": (
                "workspace.xlsx",
                xlsx_bytes,
                "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            )
        },
    )
    assert resp_xlsx.status_code == 200
    assert resp_xlsx.json()["summary_metrics"]["total_requests"] == 6


def test_single_request_endpoint_and_index_page() -> None:
    home = client.get("/")
    assert home.status_code == 200
    assert "Minh An AI Sales Agent" in home.text

    single = client.post(
        "/api/process/single",
        json={
            "request_id": "YC-TEST-99",
            "customer_name": "Công ty ABC",
            "raw_text": "Cần 10 Laptop Dell Latitude 5450 (giá cũ 23.2 triệu) và 1 Firewall FortiGate 60F. Ngân sách 300 triệu, giao trong 5 ngày, chiết khấu 6%.",
        },
    )
    assert single.status_code == 200
    res = single.json()["results"][0]
    assert res["request_id"] == "YC-TEST-99"
    assert any("giá cũ" in f.lower() for f in res["risk_flags"])


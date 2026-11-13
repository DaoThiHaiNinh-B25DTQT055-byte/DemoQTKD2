# Minh An AI Sales Agent

Hệ thống AI Agent xử lý yêu cầu báo giá B2B, đối chiếu bảng giá hiệu lực, tồn khả dụng, thời gian triển khai, chính sách chiết khấu/chuyển duyệt và xuất Hồ sơ Cơ hội + Đề xuất Kinh doanh + Bản nháp Phản hồi trực tiếp trên giao diện localhost cho **Công ty Minh An**.

## Tính năng chính
- **Đầu vào đa dạng:** Tải trực tiếp link **Google Sheets** (hỗ trợ cả single-tab và multi-tab) hoặc upload file **`.csv`**, **`.xlsx`**, hoặc nhập nhanh yêu cầu văn bản trên giao diện.
- **Chống 4 lỗi nghiệp vụ cốt lõi:**
  1. Chống bỏ sót hạng mục (tách đa sản phẩm, giữ lại hạng mục ngoài danh mục, cảnh báo dịch vụ triển khai đi kèm).
  2. Chặn dùng giá cũ (đối chiếu bảng giá hiệu lực hiện hành, phát hiện khách hàng/dữ liệu nhắc giá cũ).
  3. Kiểm soát tiến độ & tồn khả dụng (tính tổng ngày khả thi theo tồn sẵn, hàng đang về ETA và thời gian triển khai; gợi ý giao từng phần).
  4. Chống chuyển qua lại vô chủ (100% yêu cầu được gán 1 NVKD Owner duy nhất + Người phê duyệt + SLA cụ thể).
- **Trả kết quả trực tiếp trên giao diện localhost** & cho phép xuất báo cáo `.xlsx`.

## Cách chạy trên localhost
```powershell
uv sync
uv run uvicorn app.main:app --host 127.0.0.1 --port 8000
```
Sau đó mở trình duyệt tại `http://127.0.0.1:8000`.


from __future__ import annotations

import json
import os
import re
from typing import Any

from pydantic import BaseModel, Field

from app.ingestion import parse_deadline_days, parse_discount_pct, parse_vnd_amount, strip_accents
from app.models import ExtractedLineItem, ExtractionResult, MasterData, ProductItem, RawCustomerRequest


class _GeminiLineItemSchema(BaseModel):
    raw_mention: str
    quantity: int = 1
    cited_old_price: float | None = None
    matched_sku: str | None = None


class _GeminiExtractionSchema(BaseModel):
    customer_name: str = ""
    contact_name: str = ""
    contact_email: str = ""
    customer_approver: str = ""
    stated_budget: float | None = None
    requested_deadline_days: int | None = None
    requested_discount_pct: float | None = None
    items: list[_GeminiLineItemSchema] = Field(default_factory=list)
    ambiguities: list[str] = Field(default_factory=list)
    raw_summary: str = ""


_NON_ITEM_MARKERS = (
    "ngan sach",
    "du toan",
    "chiet khau",
    "thoi han",
    "ban giao hoan tat",
    "kinh gui",
    "chao minh an",
    "xin chao",
    "tran trong",
    "nguoi phe duyet",
    "mong muon",
    "yeu cau giao",
    "trien khai trong",
    "tong ngan sach",
    "thoi gian trien khai",
)

_STOP_TOKENS = {
    "va",
    "kem",
    "bo",
    "chiec",
    "cai",
    "may",
    "goi",
    "ban",
    "quyen",
    "he",
    "thong",
    "thiet",
    "bi",
    "ben",
    "minh",
    "em",
    "can",
    "mua",
    "bao",
    "gia",
    "gap",
    "giup",
    "cho",
    "cua",
    "cac",
    "them",
    "moi",
    "chuan",
    "hoa",
    "don",
    "vi",
    "trang",
    "gom",
    "nhu",
    "sau",
    "de",
    "nghi",
    "xin",
    "moi",
}


def _tokenize(text: str) -> set[str]:
    norm = strip_accents(text.lower())
    tokens = re.findall(r"[a-z0-9]+", norm)
    return {t for t in tokens if len(t) >= 2 and t not in _STOP_TOKENS}


def match_product_in_catalog(mention: str, products: list[ProductItem]) -> tuple[ProductItem | None, float]:
    """Match a text mention to the best product in Minh An's catalog, or None if out of catalog."""
    norm_mention = strip_accents(mention.lower())
    # Remove quantity prefix and parenthetical price notes before matching
    cleaned = re.sub(r"\([^)]*\)", " ", norm_mention)
    cleaned = re.sub(r"^\s*\d+\s*(bo|chiec|cai|may|license|goi|user|ban quyen)?\s*", "", cleaned).strip()
    if not cleaned:
        return None, 0.0

    # 1. Exact SKU match
    for p in products:
        if p.sku.lower() in norm_mention:
            return p, 1.0

    # 2. Alias substring match (prefer longest alias match)
    best_alias_product: ProductItem | None = None
    best_alias_len = 0
    for p in products:
        for alias in p.aliases:
            norm_alias = strip_accents(alias.lower())
            if norm_alias and norm_alias in cleaned:
                if len(norm_alias) > best_alias_len:
                    best_alias_len = len(norm_alias)
                    best_alias_product = p
    if best_alias_product is not None:
        return best_alias_product, 0.95

    # 3. Token overlap scoring with strict threshold to avoid false positives
    mention_tokens = _tokenize(cleaned)
    if not mention_tokens:
        return None, 0.0

    best_prod: ProductItem | None = None
    best_score = 0.0
    for p in products:
        prod_text = f"{p.name} {' '.join(p.aliases)}"
        prod_tokens = _tokenize(prod_text)
        if not prod_tokens:
            continue
        overlap = mention_tokens & prod_tokens
        if not overlap:
            continue
        # Require at least one distinctive brand/model or 2 meaningful domain tokens
        score = len(overlap) / max(len(mention_tokens), 2)
        if len(overlap) >= 2 or (len(overlap) == 1 and len(mention_tokens) <= 2 and score >= 0.5):
            if score > best_score:
                best_score = score
                best_prod = p

    if best_prod is not None and best_score >= 0.45:
        return best_prod, round(min(best_score, 0.9), 2)
    return None, 0.0


def _extract_cited_old_price(clause: str) -> float | None:
    norm = strip_accents(clause.lower())
    m = re.search(
        r"(?:gia cu|don gia|gia dot truoc|gia thang truoc|ap dung gia)\s*(?:la|:)?\s*(\d+(?:[.,]\d+)?)\s*(trieu|tr|m|ty)?",
        norm,
    )
    if not m:
        return None
    val = float(m.group(1).replace(",", "."))
    unit = m.group(2)
    if unit in {"trieu", "tr", "m"}:
        return val * 1_000_000
    if unit == "ty":
        return val * 1_000_000_000
    if val < 1000:
        return val * 1_000_000
    return val


def _extract_budget_from_text(text: str) -> float | None:
    norm = strip_accents(text.lower())
    m = re.search(
        r"(?:ngan sach|du toan|kinh phi|budget)[^.;\n]{0,35}?(\d+(?:[.,]\d+)?)\s*(ty|t|trieu|tr|trieu dong|m)\b",
        norm,
    )
    if m:
        return parse_vnd_amount(f"{m.group(1)} {m.group(2)}")
    return None


def _extract_deadline_from_text(text: str) -> int | None:
    norm = strip_accents(text.lower())
    m = re.search(
        r"(?:thoi han|ban giao|trien khai|hoan tat|giao hang|can trong|trong vong|deadline)[^.;\n]{0,35}?(\d+(?:[.,]\d+)?\s*(?:ngay|tuan|thang))",
        norm,
    )
    if m:
        return parse_deadline_days(m.group(1))
    return None


def _extract_discount_from_text(text: str) -> float | None:
    norm = strip_accents(text.lower())
    m = re.search(r"(?:chiet khau|giam gia|discount)[^.;\n]{0,30}?(\d+(?:[.,]\d+)?)\s*%", norm)
    if m:
        return float(m.group(1).replace(",", "."))
    return None


def _extract_customer_name_from_text(text: str) -> str:
    m = re.search(
        r"(?:Công ty(?:\s+(?:CP|Cổ phần|TNHH))?|Tập đoàn|Bệnh viện|Hệ thống|Đơn vị)\s+[^\n,.;:]{2,55}",
        text,
        flags=re.IGNORECASE,
    )
    if m:
        candidate = m.group(0).strip()
        if "minh an" not in strip_accents(candidate.lower()):
            return candidate
    return ""


def _split_item_clauses(text: str) -> list[str]:
    """Isolate product request clauses from greeting/budget/deadline sentences."""
    # Remove greeting sentences and metadata sentences before splitting item list
    sentences = re.split(r"(?<!\d)[.;\n]+(?!\d)", text)
    product_chunks: list[str] = []

    for sent in sentences:
        s = sent.strip()
        if not s:
            continue
        norm_s = strip_accents(s.lower())

        # Skip pure greetings / sign-offs
        if re.match(r"^(kinh gui|chao|xin chao|tran trong|cam on)\b", norm_s) and len(norm_s.split()) <= 8:
            continue

        # If sentence introduces a list with ':', keep the right-hand part
        if ":" in s:
            left, right = s.split(":", 1)
            norm_left = strip_accents(left.lower())
            if any(k in norm_left for k in ("bao gia", "can", "gom", "mua", "trang bi", "bo sung", "yeu cau", "chi tiet")):
                s = right.strip()
                norm_s = strip_accents(s.lower())
        else:
            # Strip common lead-in prefixes like "Đơn vị An Khang cần trang bị cho chi nhánh mới gồm ..."
            s = re.sub(
                r"^.*?\b(?:cần báo giá(?:\s+gấp)?|cần trang bị[^,]*?gồm|cần hỏi giá|cần mua(?:\s+bổ sung)?|cần|báo giá giúp)\b[:\s]*",
                "",
                s,
                flags=re.IGNORECASE,
            ).strip()
            norm_s = strip_accents(s.lower())

        # Filter out metadata-only sentences
        if any(marker in norm_s for marker in _NON_ITEM_MARKERS):
            # Only skip if it does NOT also contain a clear quantity + hardware/software term
            if not re.search(r"\b\d+\s+(?:laptop|may|bo|switch|tuong lua|firewall|wifi|wi-fi|server|nas|ban quyen|ups)\b", norm_s):
                continue

        # Split on commas and standalone " và " while preserving phrase pairs like "cài đặt và bàn giao"
        parts = re.split(r"\s*,\s*|\s+\bvà\b\s+", s, flags=re.IGNORECASE)
        for p in parts:
            p = p.strip(" .-•\t")
            if not p:
                continue
            norm_p = strip_accents(p.lower())
            if any(marker in norm_p for marker in _NON_ITEM_MARKERS) and not re.search(r"\b\d+\b", norm_p):
                continue
            # Merge back dangling service tails like "bàn giao máy chuẩn hóa" if previous item was "dịch vụ cài đặt"
            if product_chunks and re.match(r"^(ban giao|bao hanh|cau hinh|ao hoa)\b", norm_p) and "dich vu" in strip_accents(product_chunks[-1].lower()):
                product_chunks[-1] = f"{product_chunks[-1]} và {p}"
                continue
            product_chunks.append(p)

    return product_chunks


def extract_with_hybrid_rules(req: RawCustomerRequest, master: MasterData) -> ExtractionResult:
    """Deterministic Vietnamese B2B request extractor that always runs cleanly without external APIs."""
    text = req.raw_text.strip()
    customer_name = req.customer_name or _extract_customer_name_from_text(text) or f"Khách hàng ({req.request_id})"
    budget = req.stated_budget if req.stated_budget is not None else _extract_budget_from_text(text)
    deadline_days = req.requested_deadline_days if req.requested_deadline_days is not None else _extract_deadline_from_text(text)
    discount_pct = req.requested_discount_pct if req.requested_discount_pct is not None else _extract_discount_from_text(text)

    clauses = _split_item_clauses(text)
    items: list[ExtractedLineItem] = []

    for clause in clauses:
        # Look for leading quantity: "15 Laptop..." or "Dịch vụ..." (defaults to 1)
        m_qty = re.match(r"^\s*(\d+)\s+(?:chiếc|cái|bộ|máy|gói)?\s*(.+)$", clause, flags=re.IGNORECASE)
        if m_qty:
            qty = max(1, int(m_qty.group(1)))
            mention_body = m_qty.group(2).strip()
        else:
            qty = 1
            mention_body = clause

        # Avoid parsing pure deadline/budget fragments that slipped through
        norm_body = strip_accents(mention_body.lower())
        if re.fullmatch(r"(?:trieu|ty|ngay|tuan|thang|%|phan tram|dong)(\s+\w+){0,3}", norm_body):
            continue

        cited_old_price = _extract_cited_old_price(clause)
        clean_mention = re.sub(r"\([^)]*\)", "", mention_body).strip()
        matched_prod, conf = match_product_in_catalog(clean_mention, master.products)

        items.append(
            ExtractedLineItem(
                raw_mention=clean_mention or mention_body,
                quantity=qty,
                cited_old_price=cited_old_price,
                matched_sku=matched_prod.sku if matched_prod else None,
                matched_name=matched_prod.name if matched_prod else None,
                confidence=conf,
            )
        )

    missing_fields: list[str] = []
    ambiguities: list[str] = []
    if budget is None:
        missing_fields.append("Thiếu ngân sách tổng dự kiến")
    if not req.customer_approver.strip():
        missing_fields.append("Chưa rõ người phê duyệt ngân sách phía khách hàng")
    if deadline_days is None:
        missing_fields.append("Chưa rõ thời hạn triển khai mong muốn")
    if not req.contact_email.strip() and not req.contact_name.strip():
        missing_fields.append("Thiếu thông tin đầu mối liên hệ khách hàng")
    if not items:
        ambiguities.append("Chưa tách được danh sách sản phẩm/dịch vụ cụ thể từ nội dung yêu cầu")

    return ExtractionResult(
        customer_name=customer_name,
        contact_name=req.contact_name,
        contact_email=req.contact_email,
        customer_approver=req.customer_approver,
        stated_budget=budget,
        requested_deadline_days=deadline_days,
        requested_discount_pct=discount_pct,
        items=items,
        missing_fields=missing_fields,
        ambiguities=ambiguities,
        extraction_mode="hybrid_rules",
        raw_summary=f"Đã trích xuất {len(items)} hạng mục từ yêu cầu {req.request_id}.",
    )


def extract_with_gemini_or_fallback(
    req: RawCustomerRequest,
    master: MasterData,
    gemini_api_key: str | None = None,
    model_name: str = "gemini-3.7-flash",
) -> ExtractionResult:
    """Use Gemini API (google-genai) when API key is provided; fallback safely to hybrid rule extractor."""
    api_key = (gemini_api_key or os.getenv("GEMINI_API_KEY") or "").strip()
    baseline = extract_with_hybrid_rules(req, master)
    if not api_key:
        return baseline

    try:
        from google import genai
        from google.genai import types

        catalog_brief = "\n".join(
            f"- {p.sku}: {p.name} (Aliases: {', '.join(p.aliases)})"
            for p in master.products
        )
        prompt = f"""Bạn là AI Sales Analyst của Công ty Công nghệ Minh An.
Hãy trích xuất có cấu trúc từ yêu cầu khách hàng sau đây:
- Mã yêu cầu: {req.request_id}
- Nguồn: {req.source}
- Khách hàng (cột): {req.customer_name}
- Người liên hệ: {req.contact_name} ({req.contact_email})
- Người phê duyệt phía KH (cột): {req.customer_approver}
- Ngân sách (cột VND): {req.stated_budget}
- Thời hạn mong muốn (cột ngày): {req.requested_deadline_days}
- Chiết khấu đề nghị (cột %): {req.requested_discount_pct}
- Nội dung văn bản gốc:
\"\"\"{req.raw_text}\"\"\"

Danh mục SKU hiện hành của Minh An:
{catalog_brief}

Quy tắc:
1. Trích xuất ĐẦY ĐỦ tất cả sản phẩm/dịch vụ khách đề cập (không được bỏ sót hạng mục).
2. Nếu hạng mục có trong danh mục Minh An, điền matched_sku chính xác. Nếu ngoài danh mục, để matched_sku = null và giữ nguyên raw_mention.
3. Nếu khách nhắc đơn giá cũ trong văn bản (vd: 23.2 triệu), quy đổi ra VND vào cited_old_price.
4. Ngân sách quy đổi ra VND (số). Thời hạn quy đổi ra số ngày (int). Chiết khấu là số phần trăm (vd 8 cho 8%).
5. Chỉ ghi nhận ambiguities nếu nội dung mơ hồ, thiếu quy cách hoặc thiếu số lượng rõ ràng."""

        client = genai.Client(api_key=api_key)
        response = client.models.generate_content(
            model=model_name or "gemini-3.7-flash",
            contents=prompt,
            config=types.GenerateContentConfig(
                response_mime_type="application/json",
                response_schema=_GeminiExtractionSchema,
                temperature=0.1,
            ),
        )
        parsed = _GeminiExtractionSchema.model_validate_json(response.text)
        products_by_sku = {p.sku.upper(): p for p in master.products}

        extracted_items: list[ExtractedLineItem] = []
        for it in parsed.items:
            sku_upper = it.matched_sku.strip().upper() if it.matched_sku else None
            prod = products_by_sku.get(sku_upper) if sku_upper else None
            if prod is None:
                prod, conf = match_product_in_catalog(it.raw_mention, master.products)
            else:
                conf = 0.99
            extracted_items.append(
                ExtractedLineItem(
                    raw_mention=it.raw_mention,
                    quantity=max(1, it.quantity),
                    cited_old_price=it.cited_old_price,
                    matched_sku=prod.sku if prod else None,
                    matched_name=prod.name if prod else None,
                    confidence=conf,
                )
            )

        # Preserve explicit structured column values if Gemini missed them
        budget = req.stated_budget if req.stated_budget is not None else (parsed.stated_budget or baseline.stated_budget)
        deadline_days = (
            req.requested_deadline_days
            if req.requested_deadline_days is not None
            else (parsed.requested_deadline_days or baseline.requested_deadline_days)
        )
        discount_pct = (
            req.requested_discount_pct
            if req.requested_discount_pct is not None
            else (parsed.requested_discount_pct if parsed.requested_discount_pct is not None else baseline.requested_discount_pct)
        )
        approver = req.customer_approver.strip() or parsed.customer_approver.strip()

        missing_fields: list[str] = []
        if budget is None:
            missing_fields.append("Thiếu ngân sách tổng dự kiến")
        if not approver:
            missing_fields.append("Chưa rõ người phê duyệt ngân sách phía khách hàng")
        if deadline_days is None:
            missing_fields.append("Chưa rõ thời hạn triển khai mong muốn")
        if not (req.contact_email or parsed.contact_email or req.contact_name or parsed.contact_name):
            missing_fields.append("Thiếu thông tin đầu mối liên hệ khách hàng")

        return ExtractionResult(
            customer_name=req.customer_name or parsed.customer_name or baseline.customer_name,
            contact_name=req.contact_name or parsed.contact_name,
            contact_email=req.contact_email or parsed.contact_email,
            customer_approver=approver,
            stated_budget=budget,
            requested_deadline_days=deadline_days,
            requested_discount_pct=discount_pct,
            items=extracted_items or baseline.items,
            missing_fields=missing_fields,
            ambiguities=parsed.ambiguities,
            extraction_mode="gemini",
            raw_summary=parsed.raw_summary or baseline.raw_summary,
        )
    except Exception as exc:
        # Seamless fallback so workflow never crashes on API errors / invalid key
        baseline.ambiguities.append(f"Lưu ý AI fallback (dùng bộ tách quy tắc chuẩn): {exc}")
        return baseline


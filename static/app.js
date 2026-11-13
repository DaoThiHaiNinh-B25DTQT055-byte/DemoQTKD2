(() => {
  const state = {
    batch: null,
    selectedId: null,
    masterData: null,
    selectedFile: null,
    geminiApiKey: sessionStorage.getItem("minhan_gemini_key") || "",
    geminiModel: sessionStorage.getItem("minhan_gemini_model") || "gemini-3.7-flash",
    hasEnvGeminiKey: false,
  };

  const el = (id) => document.getElementById(id);

  function escapeHtml(value) {
    return String(value ?? "")
      .replaceAll("&", "&amp;")
      .replaceAll("<", "&lt;")
      .replaceAll(">", "&gt;")
      .replaceAll('"', "&quot;")
      .replaceAll("'", "&#39;");
  }

  function formatVnd(num) {
    if (num === null || num === undefined || Number.isNaN(Number(num))) return "Chưa xác định";
    return Math.round(Number(num)).toLocaleString("vi-VN") + " đ";
  }

  function showBanner(targetId, message, type = "info") {
    const banner = el(targetId);
    if (!banner) return;
    banner.hidden = false;
    banner.className = `alert-banner alert-banner--${type}`;
    banner.textContent = message;
  }

  function hideBanner(targetId) {
    const banner = el(targetId);
    if (banner) banner.hidden = true;
  }

  function updateAiStatusPill() {
    const pill = el("llmStatusPill");
    if (state.geminiApiKey) {
      pill.textContent = `AI Engine: Gemini (${state.geminiModel})`;
    } else if (state.hasEnvGeminiKey) {
      pill.textContent = `AI Engine: Gemini (.env) + Hybrid`;
    } else {
      pill.textContent = "Chế độ: Hybrid Rules (Sẵn sàng)";
    }
  }

  async function fetchHealthAndMasterData() {
    try {
      const [hRes, mRes] = await Promise.all([
        fetch("/api/health"),
        fetch("/api/master-data"),
      ]);
      if (hRes.ok) {
        const health = await hRes.json();
        el("priceVersionPill").textContent = `Bảng giá: ${health.price_list_version}`;
        state.hasEnvGeminiKey = Boolean(health.has_env_gemini_key);
        updateAiStatusPill();
      }
      if (mRes.ok) {
        state.masterData = await mRes.json();
        renderMasterDataView();
      }
    } catch (err) {
      showBanner("statusBanner", `Không thể kết nối máy chủ localhost: ${err.message}`, "error");
    }
  }

  // Navigation Tabs
  document.querySelectorAll(".nav-tab").forEach((btn) => {
    btn.addEventListener("click", () => {
      const view = btn.dataset.view;
      document.querySelectorAll(".nav-tab").forEach((t) => {
        t.classList.toggle("is-active", t === btn);
        t.setAttribute("aria-selected", t === btn ? "true" : "false");
      });
      document.querySelectorAll(".view-section").forEach((sec) => {
        sec.classList.toggle("is-active", sec.id === `view-${view}`);
      });
    });
  });

  // Input Tabs
  document.querySelectorAll(".input-tab").forEach((btn) => {
    btn.addEventListener("click", () => {
      const tab = btn.dataset.inputTab;
      document.querySelectorAll(".input-tab").forEach((t) => {
        t.classList.toggle("is-active", t === btn);
        t.setAttribute("aria-selected", t === btn ? "true" : "false");
      });
      document.querySelectorAll(".input-panel").forEach((p) => {
        p.classList.toggle("is-active", p.id === `panel-${tab}`);
      });
    });
  });

  // AI Settings Modal
  const dialog = el("aiSettingsDialog");
  el("openAiSettingsBtn").addEventListener("click", () => {
    el("geminiKeyInput").value = state.geminiApiKey;
    el("geminiModelSelect").value = state.geminiModel;
    dialog.showModal();
  });
  el("closeAiDialogBtn").addEventListener("click", () => dialog.close());
  el("clearAiKeyBtn").addEventListener("click", () => {
    state.geminiApiKey = "";
    sessionStorage.removeItem("minhan_gemini_key");
    el("geminiKeyInput").value = "";
    updateAiStatusPill();
    dialog.close();
  });
  el("aiSettingsForm").addEventListener("submit", (e) => {
    e.preventDefault();
    state.geminiApiKey = el("geminiKeyInput").value.trim();
    state.geminiModel = el("geminiModelSelect").value;
    if (state.geminiApiKey) {
      sessionStorage.setItem("minhan_gemini_key", state.geminiApiKey);
    } else {
      sessionStorage.removeItem("minhan_gemini_key");
    }
    sessionStorage.setItem("minhan_gemini_model", state.geminiModel);
    updateAiStatusPill();
    dialog.close();
  });

  // File Drag & Drop
  const dropzone = el("dropzone");
  const fileInput = el("fileInput");
  const uploadSubmitBtn = el("uploadSubmitBtn");

  function setSelectedFile(file) {
    state.selectedFile = file || null;
    if (file) {
      el("selectedFileName").textContent = `Đã chọn: ${file.name} (${(file.size / 1024).toFixed(1)} KB)`;
      uploadSubmitBtn.disabled = false;
    } else {
      el("selectedFileName").textContent =
        "Hỗ trợ file yêu cầu đơn lẻ hoặc workbook Excel nhiều sheet (tự động đồng bộ Danh mục giá, Tồn kho, Chính sách).";
      uploadSubmitBtn.disabled = true;
    }
  }

  fileInput.addEventListener("change", () => setSelectedFile(fileInput.files[0]));
  dropzone.addEventListener("dragover", (e) => {
    e.preventDefault();
    dropzone.classList.add("is-dragover");
  });
  dropzone.addEventListener("dragleave", () => dropzone.classList.remove("is-dragover"));
  dropzone.addEventListener("drop", (e) => {
    e.preventDefault();
    dropzone.classList.remove("is-dragover");
    if (e.dataTransfer.files.length) {
      setSelectedFile(e.dataTransfer.files[0]);
    }
  });

  async function handleBatchResponse(res) {
    const data = await res.json();
    if (!res.ok) {
      throw new Error(data.detail || "Không thể xử lý dữ liệu.");
    }
    state.batch = data;
    state.selectedId = data.results[0]?.request_id || null;
    renderBatch();
    // Refresh master data in case multi-sheet upload synced catalog/policy
    fetchHealthAndMasterData();
  }

  async function runWithLoading(btn, labelLoading, actionFn) {
    const origText = btn.textContent;
    btn.disabled = true;
    btn.textContent = labelLoading;
    hideBanner("statusBanner");
    try {
      await actionFn();
    } catch (err) {
      showBanner("statusBanner", err.message, "error");
    } finally {
      btn.disabled = false;
      btn.textContent = origText;
    }
  }

  // Run Sample Data
  el("runSampleBtn").addEventListener("click", () => {
    const btn = el("runSampleBtn");
    runWithLoading(btn, "Đang phân tích dữ liệu mẫu...", async () => {
      const form = new FormData();
      if (state.geminiApiKey) form.append("gemini_api_key", state.geminiApiKey);
      form.append("model_name", state.geminiModel);
      const res = await fetch("/api/process/sample", { method: "POST", body: form });
      await handleBatchResponse(res);
      showBanner("statusBanner", "Đã phân tích hoàn tất 6 yêu cầu mẫu của Công ty Minh An.", "success");
    });
  });

  // Upload CSV / XLSX
  el("uploadForm").addEventListener("submit", (e) => {
    e.preventDefault();
    if (!state.selectedFile) return;
    runWithLoading(uploadSubmitBtn, "Đang đọc & phân tích file...", async () => {
      const form = new FormData();
      form.append("file", state.selectedFile);
      form.append("sync_master_sheets", el("syncMasterSheets").checked ? "true" : "false");
      if (state.geminiApiKey) form.append("gemini_api_key", state.geminiApiKey);
      form.append("model_name", state.geminiModel);
      const res = await fetch("/api/process/upload", { method: "POST", body: form });
      await handleBatchResponse(res);
      showBanner("statusBanner", `Đã xử lý thành công tệp ${state.selectedFile.name}.`, "success");
    });
  });

  // Google Sheets Import
  el("gsheetForm").addEventListener("submit", (e) => {
    e.preventDefault();
    const url = el("gsheetUrlInput").value.trim();
    if (!url) return;
    const btn = el("gsheetSubmitBtn");
    runWithLoading(btn, "Đang tải Google Sheets...", async () => {
      const res = await fetch("/api/process/google-sheet", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          sheet_url: url,
          gemini_api_key: state.geminiApiKey || null,
          model_name: state.geminiModel,
        }),
      });
      await handleBatchResponse(res);
      showBanner("statusBanner", "Đã tải và phân tích dữ liệu từ Google Sheets thành công.", "success");
    });
  });

  // Single Request Quick Simulator
  el("fillQuickExampleBtn").addEventListener("click", () => {
    el("singleCustomer").value = "Công ty CP Cơ khí Tân Tiến";
    el("singleApprover").value = "";
    el("singleSource").value = "Email";
    el("singleRawText").value =
      "Chào Minh An, bên mình cần gấp 12 Laptop Dell Latitude 5450 (áp giá cũ 23.2 triệu giúp nhé), 2 Firewall FortiGate 60F và 1 Switch Cisco Catalyst 9200L. Giao trong 4 ngày, đề nghị chiết khấu 8%.";
  });

  el("singleForm").addEventListener("submit", (e) => {
    e.preventDefault();
    const btn = e.target.querySelector('button[type="submit"]');
    runWithLoading(btn, "Agent đang phân tích...", async () => {
      const res = await fetch("/api/process/single", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          request_id: `YC-${Date.now().toString().slice(-4)}`,
          customer_name: el("singleCustomer").value.trim(),
          customer_approver: el("singleApprover").value.trim(),
          source: el("singleSource").value,
          raw_text: el("singleRawText").value.trim(),
          gemini_api_key: state.geminiApiKey || null,
          model_name: state.geminiModel,
        }),
      });
      await handleBatchResponse(res);
      showBanner("statusBanner", "Đã hoàn tất phân tích yêu cầu đơn lẻ.", "success");
    });
  });

  // Export XLSX
  el("exportXlsxBtn").addEventListener("click", async () => {
    if (!state.batch) return;
    const btn = el("exportXlsxBtn");
    btn.disabled = true;
    try {
      const res = await fetch("/api/export/xlsx", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(state.batch),
      });
      if (!res.ok) throw new Error("Xuất Excel thất bại.");
      const blob = await res.blob();
      const url = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url;
      a.download = `MinhAn_HoSo_DeXuat_${state.batch.batch_id}.xlsx`;
      a.click();
      URL.revokeObjectURL(url);
    } catch (err) {
      showBanner("statusBanner", err.message, "error");
    } finally {
      btn.disabled = false;
    }
  });

  function badgeClassForApproval(status) {
    if (status === "Tự duyệt (NVKD)") return "badge--success";
    if (status === "Chờ Trưởng phòng KD duyệt") return "badge--warning";
    if (status === "Chờ Ban Giám đốc duyệt") return "badge--danger";
    return "badge--info";
  }

  function badgeClassForRisk(level) {
    if (level === "Thấp") return "badge--success";
    if (level === "Trung bình") return "badge--warning";
    return "badge--danger";
  }

  function renderBatch() {
    const batch = state.batch;
    if (!batch) return;

    el("workflowSection").hidden = false;
    el("kpiSection").hidden = false;
    el("resultsSection").hidden = false;

    el("batchSourceMeta").textContent =
      `Nguồn: ${batch.source_label} • Bảng giá: ${batch.price_list_version} • Chế độ trích xuất: ${batch.llm_mode_used} • Lúc ${batch.processed_at}`;

    // Render 6-step Workflow Overview
    el("workflowStepsGrid").innerHTML = batch.workflow_overview
      .map(
        (step) => `
        <div class="wf-step">
          <div class="wf-step-head">
            <span class="wf-step-num">${step.step}</span>
            <span class="badge ${step.flagged > 0 ? "badge--warning" : "badge--success"}">
              ${step.flagged > 0 ? `${step.flagged} cần lưu ý` : "Đạt chuẩn"}
            </span>
          </div>
          <div class="wf-step-title">${escapeHtml(step.name)}</div>
          <div class="wf-step-desc">${escapeHtml(step.description)}</div>
          <div class="wf-step-stats">
            <span style="color: var(--success);">✓ ${step.passed}</span>
            <span style="color: var(--warning);">⚠ ${step.flagged}</span>
          </div>
        </div>
      `
      )
      .join("");

    // Render KPIs
    const m = batch.summary_metrics;
    el("kpiSection").innerHTML = `
      <div class="kpi-card">
        <div class="kpi-label">Tổng Hồ sơ &amp; Giá trị Cơ hội</div>
        <div class="kpi-value">${m.total_requests} yêu cầu</div>
        <div class="kpi-sub">Tổng giá trị sau CK: <strong>${formatVnd(m.total_pipeline_vnd)}</strong></div>
      </div>
      <div class="kpi-card">
        <div class="kpi-label">Phân luồng Phê duyệt</div>
        <div class="kpi-value">${m.auto_approved_count} tự duyệt</div>
        <div class="kpi-sub">${m.manager_review_count} chờ TP KD • ${m.director_review_count} chờ BGĐ</div>
      </div>
      <div class="kpi-card">
        <div class="kpi-label">Chặn lỗi Giá cũ &amp; Bỏ sót</div>
        <div class="kpi-value">${m.old_price_catches + m.omission_catches} cảnh báo</div>
        <div class="kpi-sub">${m.old_price_catches} đơn nhắc giá cũ • ${m.omission_catches} đơn nguy cơ sót mục</div>
      </div>
      <div class="kpi-card">
        <div class="kpi-label">Chờ Làm rõ &amp; Rủi ro Cao</div>
        <div class="kpi-value">${m.clarification_needed_count} cần hỏi lại</div>
        <div class="kpi-sub">${m.high_risk_count} hồ sơ rủi ro cao (100% đã gán Owner)</div>
      </div>
    `;

    renderFilteredList();
  }

  function getFilteredResults() {
    if (!state.batch) return [];
    const q = el("searchInput").value.trim().toLowerCase();
    const st = el("statusFilter").value;
    const rk = el("riskFilter").value;
    return state.batch.results.filter((r) => {
      if (st !== "ALL" && r.opportunity.approval_status !== st) return false;
      if (rk !== "ALL" && r.overall_risk_level !== rk) return false;
      if (q) {
        const hay = [
          r.request_id,
          r.opportunity.customer_name,
          r.opportunity.owner_name,
          r.extraction.raw_summary,
          ...r.line_items.map((i) => `${i.sku || ""} ${i.product_name}`),
        ]
          .join(" ")
          .toLowerCase();
        if (!hay.includes(q)) return false;
      }
      return true;
    });
  }

  ["searchInput", "statusFilter", "riskFilter"].forEach((id) => {
    el(id).addEventListener("input", renderFilteredList);
    el(id).addEventListener("change", renderFilteredList);
  });

  function renderFilteredList() {
    const items = getFilteredResults();
    el("filteredCount").textContent = items.length;
    const listEl = el("opportunityList");

    if (!items.length) {
      listEl.innerHTML = `<div class="muted" style="padding: 16px; text-align: center;">Không có hồ sơ nào khớp bộ lọc.</div>`;
      el("detailPanel").innerHTML = `<div class="card empty-detail">Không có hồ sơ phù hợp bộ lọc hiện tại.</div>`;
      return;
    }

    if (!items.some((i) => i.request_id === state.selectedId)) {
      state.selectedId = items[0].request_id;
    }

    listEl.innerHTML = items
      .map((r) => {
        const selected = r.request_id === state.selectedId;
        return `
        <button type="button" class="opp-item ${selected ? "is-selected" : ""}" data-req-id="${escapeHtml(r.request_id)}">
          <div class="opp-item-top">
            <span class="opp-id">${escapeHtml(r.opportunity.opportunity_id)}</span>
            <span class="badge ${badgeClassForRisk(r.overall_risk_level)}">Rủi ro: ${escapeHtml(r.overall_risk_level)}</span>
          </div>
          <div class="opp-customer">${escapeHtml(r.opportunity.customer_name)}</div>
          <div class="opp-meta-row">
            <span>Owner: <strong>${escapeHtml(r.opportunity.owner_name)}</strong></span>
            <strong>${formatVnd(r.final_total)}</strong>
          </div>
          <div class="opp-badges">
            <span class="badge ${badgeClassForApproval(r.opportunity.approval_status)}">${escapeHtml(r.opportunity.approval_status)}</span>
            <span class="badge badge--neutral">${escapeHtml(r.timeline_status)}</span>
          </div>
        </button>
      `;
      })
      .join("");

    listEl.querySelectorAll(".opp-item").forEach((btn) => {
      btn.addEventListener("click", () => {
        state.selectedId = btn.dataset.reqId;
        renderFilteredList();
      });
    });

    const active = items.find((i) => i.request_id === state.selectedId) || items[0];
    renderDetail(active);
  }

  function renderDetail(r) {
    const opp = r.opportunity;
    const budgetDiffText =
      r.stated_budget === null
        ? "Chưa cung cấp ngân sách"
        : r.budget_gap <= 0
          ? `Dư ${formatVnd(Math.abs(r.budget_gap))}`
          : `Vượt ${formatVnd(r.budget_gap)}`;

    const stockBadge = (status) => {
      if (status === "Đủ tồn") return "badge--success";
      if (status === "Thiếu một phần") return "badge--warning";
      return "badge--danger";
    };

    const companionsHtml = r.recommended_companions.length
      ? `
        <div class="alert-banner alert-banner--info" style="margin-top: 12px;">
          <strong>Kiểm tra chống bỏ sót hạng mục (Dịch vụ đi kèm khuyến nghị):</strong>
          <ul style="margin: 6px 0 0; padding-left: 18px;">
            ${r.recommended_companions
              .map(
                (c) =>
                  `<li><strong>${escapeHtml(c.name)}</strong> (${escapeHtml(c.sku)}) — ${formatVnd(c.unit_price)}: ${escapeHtml(c.reason)}</li>`
              )
              .join("")}
          </ul>
        </div>
      `
      : "";

    const risksHtml = r.risk_flags.length
      ? `<ul class="risk-list">${r.risk_flags.map((f) => `<li>${escapeHtml(f)}</li>`).join("")}</ul>`
      : `<p style="color: var(--success); font-weight: 600; margin-top: 6px;">Không phát hiện rủi ro hoặc thiếu sót trọng yếu.</p>`;

    el("detailPanel").innerHTML = `
      <!-- 1. HỒ SƠ CƠ HỘI & PHÊ DUYỆT -->
      <div class="card detail-header-card">
        <div class="detail-title-row">
          <div>
            <div class="opp-badges" style="margin-bottom: 6px;">
              <span class="badge badge--neutral">${escapeHtml(opp.opportunity_id)}</span>
              <span class="badge badge--neutral">Nguồn: ${escapeHtml(opp.source)}</span>
              <span class="badge ${badgeClassForApproval(opp.approval_status)}">${escapeHtml(opp.approval_status)}</span>
              <span class="badge ${badgeClassForRisk(r.overall_risk_level)}">Rủi ro: ${escapeHtml(r.overall_risk_level)}</span>
            </div>
            <h2>${escapeHtml(opp.customer_name)}</h2>
            <p class="muted small-note">Đầu mối KH: ${escapeHtml(opp.contact_info)} • Tiếp nhận: ${escapeHtml(opp.received_at)}</p>
          </div>
        </div>

        <div class="opp-grid">
          <div>
            <div class="opp-field-label">NVKD Phụ trách (Owner duy nhất)</div>
            <div class="opp-field-val">${escapeHtml(opp.owner_name)}</div>
          </div>
          <div>
            <div class="opp-field-label">Thẩm quyền Phê duyệt</div>
            <div class="opp-field-val">${escapeHtml(opp.approver_required)}</div>
          </div>
          <div>
            <div class="opp-field-label">Người duyệt phía Khách hàng</div>
            <div class="opp-field-val">${escapeHtml(opp.customer_approver)}</div>
          </div>
        </div>

        <div class="next-action-box">
          <strong>Hành động tiếp theo (${escapeHtml(opp.sla_deadline)}):</strong><br />
          ${escapeHtml(opp.next_action)}
        </div>

        <div class="financial-strip">
          <div class="fin-box">
            <div class="opp-field-label">Giá trị niêm yết</div>
            <div class="fin-box-val">${formatVnd(r.subtotal_before_discount)}</div>
          </div>
          <div class="fin-box">
            <div class="opp-field-label">Chiết khấu áp dụng</div>
            <div class="fin-box-val">${r.applied_discount_pct.toFixed(1)}% <span class="muted" style="font-size:12px;">(Chuẩn: ≤${r.recommended_max_discount_pct.toFixed(0)}%)</span></div>
          </div>
          <div class="fin-box">
            <div class="opp-field-label">Tổng sau chiết khấu</div>
            <div class="fin-box-val" style="color: var(--primary);">${formatVnd(r.final_total)}</div>
          </div>
          <div class="fin-box">
            <div class="opp-field-label">Ngân sách &amp; Tiến độ</div>
            <div class="fin-box-val" style="font-size: 13.5px;">
              ${escapeHtml(r.budget_status)} (${escapeHtml(budgetDiffText)})<br />
              <span class="muted">${escapeHtml(r.timeline_status)}: ${r.feasible_delivery_days} ngày vs ${r.requested_deadline_days ?? "?"} ngày</span>
            </div>
          </div>
        </div>
      </div>

      <!-- 2. CHI TIẾT HẠNG MỤC, GIÁ HIỆU LỰC & TỒN KHO -->
      <div class="card">
        <h3>Chi tiết Hạng mục, Đối chiếu Bảng giá Hiệu lực &amp; Tồn khả dụng</h3>
        <div class="table-scroll">
          <table class="data-table">
            <thead>
              <tr>
                <th>SKU / Hạng mục</th>
                <th class="num-cell">SL</th>
                <th class="num-cell">Đơn giá Hiệu lực</th>
                <th class="num-cell">Thành tiền</th>
                <th>Tồn kho &amp; ETA</th>
                <th>Khả thi</th>
                <th>Cảnh báo chống lỗi</th>
              </tr>
            </thead>
            <tbody>
              ${r.line_items
                .map(
                  (it) => `
                <tr>
                  <td>
                    <strong>${escapeHtml(it.product_name)}</strong><br />
                    <span class="muted small-note">${escapeHtml(it.sku || "NGOÀI DANH MỤC")} • ${escapeHtml(it.category)}</span>
                  </td>
                  <td class="num-cell">${it.quantity} ${escapeHtml(it.unit)}</td>
                  <td class="num-cell">
                    ${it.sku ? formatVnd(it.unit_price) : "Chờ báo giá"}
                    ${it.cited_old_price ? `<br /><span class="badge badge--danger">KH nhắc giá cũ: ${formatVnd(it.cited_old_price)}</span>` : ""}
                  </td>
                  <td class="num-cell"><strong>${it.sku ? formatVnd(it.line_subtotal) : "—"}</strong></td>
                  <td>
                    ${it.sku ? `Sẵn: <strong>${it.available_stock}</strong> • Đang về: ${it.incoming_stock} (${it.incoming_eta_days}n)` : "—"}
                  </td>
                  <td>
                    <span class="badge ${stockBadge(it.stock_status)}">${escapeHtml(it.stock_status)}</span>
                    ${it.sku ? `<div class="muted small-note">${it.estimated_ready_days} ngày</div>` : ""}
                  </td>
                  <td>
                    ${it.alerts.length ? it.alerts.map((a) => `<div class="small-note" style="color: var(--warning);">${escapeHtml(a)}</div>`).join("") : `<span class="muted small-note">Hợp lệ</span>`}
                  </td>
                </tr>
              `
                )
                .join("")}
            </tbody>
          </table>
        </div>
        ${companionsHtml}
      </div>

      <!-- 3. CẢNH BÁO RỦI RO & ĐỀ XUẤT KINH DOANH -->
      <div class="card">
        <div class="master-grid-top" style="margin-top: 0;">
          <div>
            <h3>Cảnh báo Rủi ro &amp; Thông tin thiếu</h3>
            ${risksHtml}
          </div>
          <div>
            <h3>Đề xuất Kinh doanh từ AI Agent</h3>
            <ul class="rec-list">
              ${r.business_recommendations.map((rec) => `<li>${escapeHtml(rec)}</li>`).join("")}
            </ul>
          </div>
        </div>
        <details class="mt-sm">
          <summary style="cursor: pointer; font-weight: 600; color: var(--text-muted);">Xem nội dung yêu cầu gốc của khách hàng</summary>
          <div class="raw-quote-box mt-sm">${escapeHtml(r.extraction.raw_summary)} — ${escapeHtml(
            r.line_items.map((l) => `${l.quantity}x ${l.raw_mention}`).join(", ")
          )}</div>
        </details>
      </div>

      <!-- 4. BẢN NHÁP PHẢN HỒI KHÁCH HÀNG -->
      <div class="card">
        <div class="draft-editor-header">
          <div>
            <h3>Bản nháp Phản hồi Khách hàng (Có thể chỉnh sửa trực tiếp)</h3>
            <p class="muted small-note">Tiêu đề: <strong>${escapeHtml(r.draft_customer_subject)}</strong></p>
          </div>
          <button type="button" class="btn btn-primary btn-sm" id="copyDraftBtn">Sao chép Nội dung Email</button>
        </div>
        <textarea class="draft-textarea" id="draftTextarea">${escapeHtml(r.draft_customer_response)}</textarea>
      </div>

      <!-- 5. NHẬT KÝ WORKFLOW 6 BƯỚC -->
      <div class="card">
        <details>
          <summary style="cursor: pointer; font-weight: 700;">Nhật ký Kiểm tra Chi tiết qua 6 Bước Workflow (${r.workflow_trace.length} bước)</summary>
          <div class="trace-list">
            ${r.workflow_trace
              .map(
                (t) => `
              <div class="trace-item">
                <div class="trace-item-head">
                  <span>Bước ${t.step_number}: ${escapeHtml(t.step_name)}</span>
                  <span class="badge ${t.status === "completed" ? "badge--success" : t.status === "warning" ? "badge--warning" : "badge--danger"}">
                    ${escapeHtml(t.status)}
                  </span>
                </div>
                <div class="small-note mt-sm">${escapeHtml(t.summary)}</div>
                <ul class="trace-details">
                  ${t.details.map((d) => `<li>${escapeHtml(d)}</li>`).join("")}
                </ul>
              </div>
            `
              )
              .join("")}
          </div>
        </details>
      </div>
    `;

    el("copyDraftBtn").addEventListener("click", async () => {
      const fullText = `Tiêu đề: ${r.draft_customer_subject}\n\n${el("draftTextarea").value}`;
      await navigator.clipboard.writeText(fullText);
      const btn = el("copyDraftBtn");
      btn.textContent = "Đã sao chép!";
      setTimeout(() => (btn.textContent = "Sao chép Nội dung Email"), 1800);
    });
  }

  // MASTER DATA VIEW
  function renderMasterDataView() {
    const md = state.masterData;
    if (!md) return;
    const pol = md.policy;

    el("policyAndRepsContainer").innerHTML = `
      <div class="subcard">
        <h3>Chính sách Chiết khấu &amp; Phê duyệt (${escapeHtml(md.price_list_version)})</h3>
        <ul class="rec-list" style="margin-top: 4px;">
          <li>NVKD tự duyệt tối đa: <strong>${pol.sales_rep_max_discount_pct}%</strong> (khi đủ tồn, kịp hạn, rõ ngân sách &amp; người duyệt)</li>
          <li>Trưởng phòng KD duyệt (${escapeHtml(pol.sales_manager_name)}): <strong>&gt;${pol.sales_rep_max_discount_pct}% đến ${pol.manager_max_discount_pct}%</strong> hoặc ngoại lệ tồn/tiến độ</li>
          <li>Ban Giám đốc duyệt (${escapeHtml(pol.director_name)}): <strong>&gt;${pol.manager_max_discount_pct}%</strong> hoặc hợp đồng lớn từ <strong>${formatVnd(pol.director_approval_contract_threshold)}</strong></li>
          <li>SLA chống chuyển qua lại: Phản hồi hỏi lại trong <strong>${pol.default_clarification_sla_hours}h</strong>, Follow-up sau <strong>${pol.follow_up_hours}h</strong></li>
        </ul>
      </div>
      <div class="subcard">
        <h3>Đội ngũ 6 Nhân viên Kinh doanh phụ trách</h3>
        <div class="table-scroll">
          <table class="data-table" style="margin-top: 0;">
            <thead>
              <tr><th>Mã</th><th>Họ tên</th><th>Chuyên trách</th><th class="num-cell">Tải/tuần</th></tr>
            </thead>
            <tbody>
              ${md.sales_reps
                .map(
                  (rep) => `
                <tr>
                  <td>${escapeHtml(rep.id)}</td>
                  <td><strong>${escapeHtml(rep.name)}</strong><br /><span class="muted small-note">${escapeHtml(rep.email)}</span></td>
                  <td>${escapeHtml(rep.specialty_categories.join(", "))}</td>
                  <td class="num-cell">${rep.active_weekly_load}</td>
                </tr>
              `
                )
                .join("")}
            </tbody>
          </table>
        </div>
      </div>
    `;

    el("catalogTable").querySelector("tbody").innerHTML = md.products
      .map(
        (p) => `
      <tr>
        <td><code>${escapeHtml(p.sku)}</code></td>
        <td><strong>${escapeHtml(p.name)}</strong></td>
        <td>${escapeHtml(p.category)}</td>
        <td class="num-cell"><strong>${formatVnd(p.unit_price)}</strong></td>
        <td class="num-cell muted">${p.previous_price ? formatVnd(p.previous_price) : "—"}</td>
        <td class="num-cell"><strong>${p.available_stock}</strong> ${escapeHtml(p.unit)}</td>
        <td class="num-cell">${p.incoming_stock} (${p.incoming_eta_days} ngày)</td>
        <td class="num-cell">${p.deployment_days} ngày</td>
      </tr>
    `
      )
      .join("");
  }

  // Master Data CSV/XLSX Upload
  el("chooseMasterFileBtn").addEventListener("click", () => el("masterFileInput").click());
  el("masterFileInput").addEventListener("change", async () => {
    const file = el("masterFileInput").files[0];
    if (!file) return;
    hideBanner("masterUploadBanner");
    const form = new FormData();
    form.append("file", file);
    try {
      const res = await fetch("/api/master-data/upload", { method: "POST", body: form });
      const data = await res.json();
      if (!res.ok) throw new Error(data.detail || "Cập nhật thất bại.");
      state.masterData = data.master_data;
      renderMasterDataView();
      showBanner(
        "masterUploadBanner",
        `Đã cập nhật thành công: ${data.updated_sections.join(", ")}.`,
        "success"
      );
    } catch (err) {
      showBanner("masterUploadBanner", err.message, "error");
    } finally {
      el("masterFileInput").value = "";
    }
  });

  // Boot
  fetchHealthAndMasterData();
})();


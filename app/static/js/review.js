/* Review screen: evidence-linked fields, live math, reviewer actions. */
(function () {
  const EDITABLE = new Set(["needs_review", "ready", "duplicate", "awaiting_second_approval"]);
  const FIELD_DEFS = [
    { key: "vendor_name", label: "Vendor", type: "text", span: 2 },
    { key: "invoice_number", label: "Invoice number", type: "text" },
    { key: "po_number", label: "PO number", type: "text" },
    { key: "invoice_date", label: "Invoice date", type: "date" },
    { key: "due_date", label: "Due date", type: "date" },
    { key: "subtotal", label: "Subtotal", type: "money" },
    { key: "tax", label: "Tax", type: "money" },
    { key: "shipping", label: "Shipping", type: "money" },
    { key: "total", label: "Total", type: "money" },
  ];
  const SOURCE_LABEL = {
    "text-layer": "PDF text", ocr: "OCR", llm: "AI", reviewer: "Checked", computed: "From terms",
    "text-layer+llm": "PDF text + AI", "ocr+llm": "OCR + AI",
  };

  const SOURCE_NAME = { email: "Email", upload: "Upload", demo: "Upload" };
  const $ = (sel, root = document) => root.querySelector(sel);
  const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

  const state = {
    inv: window.INVOICE,
    draftFields: {},
    confirm: new Set(),
    lines: null,
    linesDirty: false,
    tab: "review",
    zoom: 0,
    active: null,
    preview: null,
  };

  function cur() { return state.inv.currency || "USD"; }
  function money(v, currency = cur()) {
    if (v === null || v === undefined || v === "" || Number.isNaN(Number(v))) return "—";
    try {
      return new Intl.NumberFormat("en-US", { style: "currency", currency }).format(Number(v));
    } catch (e) { return Number(v).toFixed(2); }
  }
  function fmtDate(iso) {
    if (!iso) return "—";
    const d = new Date(`${iso}T00:00:00`);
    return Number.isNaN(d.getTime()) ? iso : d.toLocaleDateString("en-US", { month: "short", day: "numeric", year: "numeric" });
  }
  function fmtTime(iso) {
    const d = new Date(iso);
    return Number.isNaN(d.getTime()) ? iso : d.toLocaleString("en-US", { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" });
  }
  function editable() { return EDITABLE.has(state.inv.status); }
  function fieldValue(key) {
    if (key in state.draftFields) return state.draftFields[key];
    const f = state.inv.fields[key];
    return f ? f.value : null;
  }
  function fmtNum(v) {
    const n = numOrNull(v);
    return n === null ? "" : n.toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
  }
  function numOr0(v) { const n = parseFloat(String(v ?? "").replace(/[$,\s]/g, "")); return Number.isFinite(n) ? n : 0; }
  function numOrNull(v) { const n = parseFloat(String(v ?? "").replace(/[$,\s]/g, "")); return Number.isFinite(n) ? n : null; }
  function isDirty() { return Object.keys(state.draftFields).length > 0 || state.linesDirty; }
  function openIssues() { return state.inv.issues.filter((i) => i.status === "open"); }
  function blockingIssues() { return openIssues().filter((i) => i.severity !== "info"); }

  function resetDraft() {
    state.draftFields = {};
    state.confirm = new Set();
    state.lines = state.inv.line_items.map((l) => ({ ...l }));
    state.linesDirty = false;
    state.preview = null;
  }

  // ------------------------------------------------------------------ header & actions
  function renderHeader() {
    const inv = state.inv;
    $("#rh-vendor").textContent = inv.vendor_name || "Unknown vendor";
    $("#rh-number").textContent = inv.invoice_number || "—";
    $("#rh-total").textContent = money(inv.total);
    $("#rh-status").innerHTML = `<span class="pill pill-${inv.status_tone}">${esc(inv.status_label)}</span>`;
    $("#history-count").textContent = inv.events.length;
    const method = inv.extraction_method || "";
    const pageInfo = (inv.pages || [])[0] || {};
    let label = method.startsWith("text-layer") ? "Read from the PDF text layer" :
      `Scanned page, OCR ${Math.round((inv.ocr_quality || 0) * 100)}% confident`;
    if (pageInfo.deskew) label += `, straightened ${Math.abs(pageInfo.deskew).toFixed(1)}°`;
    if (method.includes("llm")) label += ", AI cross-checked";
    $("#doc-method").textContent = label;
    $("#doc-pages").textContent = `${(inv.pages || []).length} page${(inv.pages || []).length === 1 ? "" : "s"}`;
  }

  function renderActions() {
    const inv = state.inv;
    const box = $("#actions");
    const actor = window.currentActor();
    const approvals = inv.approvals || [];
    const needsTwo = (inv.total || 0) >= inv.second_approval_limit;
    const parts = [];
    let hint = "";

    if (["needs_review", "ready", "duplicate", "awaiting_second_approval", "failed", "post_failed"].includes(inv.status)) {
      parts.push(`<button class="btn btn-danger" data-act="reject">Reject</button>`);
    }
    if (inv.status === "failed") parts.push(`<button class="btn" data-act="reprocess">Read again</button>`);
    if (inv.status === "post_failed") parts.push(`<button class="btn btn-primary" data-act="retry">Retry posting</button>`);

    if (["needs_review", "ready", "duplicate", "awaiting_second_approval"].includes(inv.status)) {
      const blockers = blockingIssues().length;
      let label = "Approve and post to QuickBooks";
      if (needsTwo && approvals.length === 0) label = "Approve (1 of 2)";
      if (inv.status === "awaiting_second_approval") label = "Give second approval and post";
      let disabled = false;
      if (isDirty()) { disabled = true; hint = "Save your changes first — they re-run the checks."; }
      else if (blockers) { disabled = true; hint = `Resolve ${blockers} open check${blockers > 1 ? "s" : ""} to approve.`; }
      else if (approvals.some((a) => a.actor.toLowerCase() === actor.toLowerCase())) {
        disabled = true; hint = `You gave the first approval. Switch to a different reviewer (top right) for the second.`;
      } else if (inv.status === "awaiting_second_approval") {
        hint = `Approved by ${approvals.map((a) => a.actor).join(", ")}. Over ${money(inv.second_approval_limit, "USD")} needs two people.`;
      } else if (needsTwo) {
        hint = `Over ${money(inv.second_approval_limit, "USD")}, so a second person must also approve.`;
      }
      parts.push(`<button class="btn btn-primary" data-act="approve" ${disabled ? "disabled" : ""}>${label}</button>`);
    }
    if (window.NEXT_ID && !["needs_review", "duplicate"].includes(inv.status)) {
      parts.push(`<a class="btn" href="/invoices/${window.NEXT_ID}">Next invoice</a>`);
    }
    box.innerHTML = (hint ? `<span class="rh-hint">${esc(hint)}</span>` : "") + parts.join("");
  }

  // ------------------------------------------------------------------ document viewer
  function renderDocument() {
    const stack = $("#doc-stack");
    const pages = state.inv.pages || [];
    stack.innerHTML = pages.map((p, i) => `
      <div class="doc-page" data-page="${i}">
        <img src="/pages/${encodeURIComponent(p.image)}" alt="Page ${i + 1} of the invoice" width="${p.width}" height="${p.height}">
        <div class="hl-layer"></div>
      </div>`).join("") || `<div class="empty">No page image available.</div>`;
    applyZoom();
    drawHighlights();
  }

  function applyZoom() {
    const widths = { "-1": "75%", 0: "100%", 1: "135%", 2: "170%", 3: "210%" };
    $("#doc-stack").style.width = widths[state.zoom] || "100%";
  }

  function bboxFor(key) {
    if (key && key.startsWith("line:")) {
      const line = state.inv.line_items[Number(key.slice(5))];
      return line ? line.bbox : null;
    }
    const f = state.inv.fields[key];
    return f ? f.bbox : null;
  }

  function placeBox(bbox, cls) {
    const page = document.querySelector(`.doc-page[data-page="${bbox.page}"] .hl-layer`);
    if (!page) return null;
    const [x0, y0, x1, y1] = bbox.box;
    const pad = 0.004;
    const el = document.createElement("div");
    el.className = `hl ${cls}`;
    el.style.left = `${(x0 - pad) * 100}%`;
    el.style.top = `${(y0 - pad) * 100}%`;
    el.style.width = `${(x1 - x0 + pad * 2) * 100}%`;
    el.style.height = `${(y1 - y0 + pad * 2) * 100}%`;
    page.appendChild(el);
    return el;
  }

  function drawHighlights(scroll = false) {
    document.querySelectorAll(".hl-layer").forEach((l) => (l.innerHTML = ""));
    if (state.tab === "review" && editable()) {
      openIssues().forEach((issue) => {
        if (issue.field && issue.field !== "line_items") {
          const b = bboxFor(issue.field);
          if (b) placeBox(b, "hl-weak");
        }
        if (issue.field === "line_items" && issue.evidence) {
          const rows = issue.evidence.rows || (issue.evidence.row !== undefined ? [issue.evidence.row] : []);
          rows.forEach((r) => { const b = bboxFor(`line:${r}`); if (b) placeBox(b, "hl-weak"); });
        }
      });
    }
    if (state.active) {
      const b = bboxFor(state.active);
      if (b) {
        const el = placeBox(b, "hl-field hl-pulse");
        if (el && scroll) el.scrollIntoView({ behavior: "smooth", block: "center", inline: "nearest" });
      }
    }
  }

  function focusEvidence(key, scroll = true) {
    state.active = key;
    drawHighlights(scroll);
    document.querySelectorAll(".lines tr.active").forEach((r) => r.classList.remove("active"));
    if (key && key.startsWith("line:")) {
      const tr = document.querySelector(`.lines tr[data-row="${key.slice(5)}"]`);
      if (tr) tr.classList.add("active");
    }
  }

  // ------------------------------------------------------------------ review tab
  function banner() {
    const inv = state.inv;
    const blockers = blockingIssues();
    if (inv.status === "posted") {
      return `<div class="banner banner-posted"><span class="banner-icon">✓</span><div>
        <strong>Bill ${esc(inv.qbo_bill_id)} created in QuickBooks ${inv.qbo_mode === "sandbox" ? "sandbox" : "simulator"}.</strong>
        <p>Saved as an unpaid bill for ${money(inv.total)}, due ${fmtDate(inv.due_date)}. Approved by ${esc((inv.approvals || []).map((a) => a.actor).join(" and "))}.</p></div></div>`;
    }
    if (inv.status === "rejected") {
      return `<div class="banner banner-danger"><span class="banner-icon">×</span><div><strong>Rejected.</strong>
        <p>${esc(inv.rejected_reason || "")}</p></div></div>`;
    }
    if (inv.status === "post_failed") {
      const err = (inv.qbo_response || {}).error || "QuickBooks did not accept the bill.";
      return `<div class="banner banner-danger"><span class="banner-icon">!</span><div><strong>Approved, but posting failed.</strong>
        <p>${esc(err)} The approval is kept — retry once the connection is fixed.</p></div></div>`;
    }
    if (inv.status === "failed") {
      return `<div class="banner banner-danger"><span class="banner-icon">!</span><div><strong>This file couldn’t be read.</strong>
        <p>${esc((inv.issues[0] || {}).message || "")}</p></div></div>`;
    }
    if (inv.status === "duplicate" && window.DUPLICATE) {
      const d = window.DUPLICATE;
      const same = (a, b) => String(a ?? "").trim().toLowerCase() === String(b ?? "").trim().toLowerCase();
      const row = (label, a, b, fmt = (x) => esc(x ?? "—")) =>
        `<tr><th>${label}</th><td class="${same(a, b) ? "same" : ""}">${fmt(a)}</td><td class="${same(a, b) ? "same" : ""}">${fmt(b)}</td></tr>`;
      return `<div class="banner banner-danger"><span class="banner-icon">!</span><div style="flex:1">
        <strong>Held: this invoice was already received.</strong>
        <p>Paying it would pay ${esc(d.vendor_name)} twice. The earlier copy is
          <a class="link" href="/invoices/${d.id}">${esc(d.ref)}</a> (${esc(d.status.replace(/_/g, " "))}${d.qbo_bill_id ? `, bill ${esc(d.qbo_bill_id)}` : ""}).</p>
        <table class="compare"><tr><th></th><th>This copy (${esc(inv.ref)})</th><th>Earlier (${esc(d.ref)})</th></tr>
          ${row("Vendor", inv.vendor_name, d.vendor_name)}
          ${row("Invoice number", inv.invoice_number, d.invoice_number)}
          ${row("Invoice date", inv.invoice_date, d.invoice_date, fmtDate)}
          ${row("Total", inv.total, d.total, (x) => money(x))}
          ${row("Arrived by", SOURCE_NAME[inv.source] || inv.source, SOURCE_NAME[d.source] || d.source)}
        </table></div></div>`;
    }
    if (inv.status === "awaiting_second_approval") {
      return `<div class="banner banner-info"><span class="banner-icon">2</span><div><strong>Waiting for a second approver.</strong>
        <p>${esc((inv.approvals || []).map((a) => a.actor).join(", "))} approved. Invoices of ${money(inv.second_approval_limit, "USD")} or more need a second person before anything reaches QuickBooks.</p></div></div>`;
    }
    if (!blockers.length) {
      return `<div class="banner banner-ok"><span class="banner-icon">✓</span><div><strong>All checks passed.</strong>
        <p>Math reconciles, vendor is approved and no duplicate was found. Approving creates an unpaid bill in QuickBooks.</p></div></div>`;
    }
    const errors = blockers.filter((i) => i.severity === "error").length;
    return `<div class="banner ${errors ? "banner-danger" : "banner-warn"}"><span class="banner-icon">!</span><div>
      <strong>${blockers.length} thing${blockers.length > 1 ? "s" : ""} to check before approval.</strong>
      <p>Highlighted boxes on the page show where each value was read. ${errors ? "Errors must be corrected; " : ""}warnings need a person to confirm.</p></div></div>`;
  }

  function issueActions(issue) {
    if (!editable()) return "";
    const acts = [];
    const f = issue.field;
    if (f && f !== "line_items" && state.inv.fields[f] && state.inv.fields[f].bbox) {
      acts.push(`<button class="btn btn-sm" data-show="${f}">Show on page</button>`);
    }
    if (issue.id.startsWith("low_conf_") && f !== "line_items") {
      acts.push(`<button class="btn btn-sm" data-confirm="${f}">Value is correct</button>`);
      acts.push(`<button class="btn btn-sm btn-quiet" data-edit="${f}">Correct it</button>`);
    }
    if (issue.id === "low_conf_lines") {
      acts.push(`<button class="btn btn-sm" data-confirm-lines>Lines are correct</button>`);
    }
    if (issue.id.startsWith("line_math_") && issue.evidence) {
      const s = issue.evidence.suggestion;
      if (s) acts.push(`<button class="btn btn-sm" data-apply-fix="${issue.evidence.row}" data-fix-field="${s.field}" data-fix-value="${s.value}">Use ${s.field.replace("_", " ")} ${Number(s.value).toFixed(2)}</button>`);
      acts.push(`<button class="btn btn-sm btn-quiet" data-show="line:${issue.evidence.row}">Show on page</button>`);
    }
    if (issue.id === "subtotal_mismatch" || issue.id === "total_mismatch") {
      acts.push(`<button class="btn btn-sm btn-quiet" data-reject-reason="${esc(issue.title)}: ${esc(issue.message.split(" — ")[0])}. Please send a corrected invoice.">Reject and ask for a corrected invoice</button>`);
    }
    if (issue.id === "new_vendor") {
      acts.push(`<button class="btn btn-sm" data-add-vendor>Add to vendor list</button>`);
    }
    if (issue.severity === "warning" && !issue.id.startsWith("low_conf")) {
      acts.push(`<button class="btn btn-sm btn-quiet" data-ack="${issue.id}">Acknowledge</button>`);
    }
    if (issue.severity === "block") {
      const ref = (issue.evidence || {}).duplicate_ref || "an earlier invoice";
      acts.push(`<button class="btn btn-sm" data-reject-reason="Duplicate of ${esc(ref)} — already received.">Reject as duplicate</button>`);
      acts.push(`<button class="btn btn-sm btn-quiet" data-release="${issue.id}">Not a duplicate — release</button>`);
    }
    return acts.length ? `<div class="check-actions">${acts.join("")}</div>` : "";
  }

  function renderChecks() {
    const issues = state.inv.issues;
    const order = { block: 0, error: 1, warning: 2, info: 3 };
    const open = issues.filter((i) => i.status === "open").sort((a, b) => order[a.severity] - order[b.severity]);
    const done = issues.filter((i) => i.status !== "open");
    const sevLabel = { block: "Hold", error: "Error", warning: "Check", info: "Note" };
    let html = "";
    if (open.length) {
      html += `<section><h3>Checks <span class="muted">${open.filter((i) => i.severity !== "info").length} open</span></h3><ul class="checks">`;
      html += open.map((i) => `<li class="check check-${i.severity}">
        <div class="check-title"><span class="sev sev-${i.severity}">${sevLabel[i.severity]}</span>${esc(i.title)}</div>
        <p>${esc(i.message)}</p>${issueActions(i)}</li>`).join("");
      html += `</ul></section>`;
    }
    if (done.length) {
      html += `<details class="resolved"><summary>${done.length} check${done.length > 1 ? "s" : ""} resolved by a reviewer</summary><ul>` +
        done.map((i) => `<li>${esc(i.title)} — ${esc(i.status)} by ${esc((i.resolution || {}).actor || "reviewer")}${(i.resolution || {}).note ? `: “${esc(i.resolution.note)}”` : ""}</li>`).join("") +
        `</ul></details>`;
    }
    return html;
  }

  function confMeter(f) {
    if (!f || f.value === null || f.value === undefined || f.value === "") return `<span class="src">not found</span>`;
    const src = f.source || "";
    if (src === "reviewer") return `<span class="src src-reviewer">✓ Checked</span>`;
    if (src === "computed") return `<span class="src">From vendor terms</span>`;
    const pct = Math.round((f.conf || 0) * 100);
    const low = (f.conf || 0) < state.inv.confidence_threshold;
    return `<span class="conf ${low ? "low" : ""}" title="${pct}% confidence, ${esc(SOURCE_LABEL[src] || src)}${f.corroborated ? ", confirmed by the math" : ""}">
      <span class="conf-bar"><i style="width:${pct}%"></i></span>${pct}%</span>
      <span class="src">${esc(SOURCE_LABEL[src] || src)}${f.corroborated ? " · math ✓" : ""}</span>`;
  }

  function renderFields() {
    const ro = !editable();
    const rows = FIELD_DEFS.map((d) => {
      const f = state.inv.fields[d.key];
      const value = fieldValue(d.key);
      const low = f && f.source !== "reviewer" && f.value !== null && f.value !== undefined && (f.conf || 0) < state.inv.confidence_threshold;
      const changed = d.key in state.draftFields;
      let inputVal = value ?? "";
      if (d.type === "money" && inputVal !== "" && !changed) inputVal = fmtNum(inputVal);
      const type = d.type === "date" ? "date" : "text";
      const extra = d.type === "money" ? `inputmode="decimal"` : "";
      const confirmBtn = low && !ro && !state.confirm.has(d.key) ? `<button class="confirm-btn" data-confirm="${d.key}" title="The value matches the page">Confirm</button>` : "";
      const was = f && f.previous !== undefined && f.source === "reviewer" && f.previous !== null ? `<span class="field-hint">Read as ${esc(d.type === "money" ? money(f.previous) : f.previous)}</span>` :
        f && f.alternative ? `<span class="field-hint">AI read “${esc(f.alternative)}”</span>` :
        d.key === "vendor_name" && low && f.read_as ? `<span class="field-hint">On page: “${esc(f.read_as)}”</span>` : "";
      return `<div class="field ${d.span === 2 ? "span-2" : ""} ${d.type === "money" ? "num" : ""} ${low ? "weak" : ""} ${changed ? "changed" : ""}">
        <div class="field-top"><label for="f-${d.key}">${d.label}</label><span class="row gap-s">${confMeter(f)}${confirmBtn}</span></div>
        <input id="f-${d.key}" data-field="${d.key}" type="${type}" ${extra} value="${esc(inputVal)}" ${ro ? "disabled" : ""} autocomplete="off">
        ${was}</div>`;
    }).join("");
    return `<section><h3>Invoice details <span class="muted">click a field to see where it was read</span></h3><div class="fields">${rows}</div></section>`;
  }

  function lineIssueRows() {
    const bad = new Set();
    openIssues().forEach((i) => { if (i.id.startsWith("line_math_")) bad.add(i.evidence.row); });
    return bad;
  }

  function renderLines() {
    const ro = !editable();
    const bad = lineIssueRows();
    const rows = state.lines.map((l, i) => {
      const low = l.source !== "reviewer" && (l.conf ?? 1) < state.inv.confidence_threshold;
      const dotCls = l.source === "reviewer" ? "rev" : low ? "low" : "";
      const dotTitle = l.source === "reviewer" ? "Checked by a reviewer" : `${Math.round((l.conf ?? 1) * 100)}% confidence`;
      const dis = ro ? "disabled" : "";
      return `<tr data-row="${i}" class="${bad.has(i) ? "bad" : low ? "weak" : ""}">
        <td><span class="row-dot ${dotCls}" title="${dotTitle}"></span></td>
        <td class="col-desc"><input data-line="${i}" data-k="description" value="${esc(l.description)}" aria-label="Line ${i + 1} description" ${dis}></td>
        <td class="n col-q"><input data-line="${i}" data-k="quantity" inputmode="decimal" value="${esc(l.quantity)}" aria-label="Line ${i + 1} quantity" ${dis}></td>
        <td class="n col-p"><input data-line="${i}" data-k="unit_price" inputmode="decimal" value="${esc(fmtNum(l.unit_price))}" aria-label="Line ${i + 1} unit price" ${dis}></td>
        <td class="n col-a"><input data-line="${i}" data-k="amount" inputmode="decimal" value="${esc(fmtNum(l.amount))}" aria-label="Line ${i + 1} amount" ${dis}></td>
        <td class="col-x">${ro ? "" : `<button class="del" data-del="${i}" aria-label="Remove line ${i + 1}">×</button>`}</td>
      </tr>`;
    }).join("");
    return `<section><h3>Line items <span class="muted">${state.lines.length}</span></h3>
      <table class="lines"><thead><tr><th></th><th>Description</th><th class="r">Qty</th><th class="r">Unit price</th><th class="r">Amount</th><th></th></tr></thead>
      <tbody>${rows || `<tr><td colspan="6" class="muted" style="padding:10px 4px">No line items were found. Add them from the page.</td></tr>`}</tbody></table>
      ${ro ? "" : `<div class="lines-foot"><button class="btn btn-sm btn-quiet" data-add-line>+ Add line</button></div>`}</section>`;
  }

  function renderTape() {
    const tol = 0.02;
    const sum = state.lines.reduce((s, l) => s + numOr0(l.amount), 0);
    const subtotal = numOrNull(fieldValue("subtotal"));
    const tax = numOr0(fieldValue("tax"));
    const shipping = numOr0(fieldValue("shipping"));
    const total = numOrNull(fieldValue("total"));
    const base = subtotal ?? sum;
    const computed = base + tax + shipping;
    const lineOk = state.lines.every((l) => Math.abs(numOr0(l.quantity) * numOr0(l.unit_price) - numOr0(l.amount)) <= Math.max(tol, 0.005 * numOr0(l.amount)));
    const subOk = subtotal === null || !state.lines.length || Math.abs(sum - subtotal) <= tol;
    const totOk = total !== null && Math.abs(computed - total) <= tol;
    const mark = (ok) => `<span class="mark ${ok ? "mark-ok" : "mark-bad"}" aria-label="${ok ? "matches" : "does not match"}">${ok ? "✓" : "×"}</span>`;
    return `<section><h3>Math check</h3><div class="tape" aria-label="Reconciliation">
      <div class="tape-row"><span class="tape-op">Σ</span><span class="tape-label">${state.lines.length} line items<small>${lineOk ? "every quantity × price matches its amount" : "a line doesn’t multiply out"}</small></span>
        <span class="tape-val">${money(sum)}${mark(lineOk)}</span></div>
      <div class="tape-row"><span class="tape-op">=</span><span class="tape-label">Subtotal on invoice</span>
        <span class="tape-val">${subtotal === null ? "—" : money(subtotal)}${state.lines.length && subtotal !== null ? mark(subOk) : ""}
        ${!subOk ? `<span class="tape-diff">off by ${money(Math.abs(subtotal - sum))}</span>` : ""}</span></div>
      <div class="tape-row"><span class="tape-op">+</span><span class="tape-label">Tax${subtotal ? `<small>${((tax / (subtotal || 1)) * 100).toFixed(2)}% of subtotal</small>` : ""}</span><span class="tape-val">${money(tax)}</span></div>
      ${shipping ? `<div class="tape-row"><span class="tape-op">+</span><span class="tape-label">Shipping</span><span class="tape-val">${money(shipping)}</span></div>` : ""}
      <div class="tape-row"><span class="tape-op">=</span><span class="tape-label">Calculated total</span><span class="tape-val">${money(computed)}</span></div>
      <div class="tape-row total"><span class="tape-op"></span><span class="tape-label"><strong>Total on invoice</strong></span>
        <span class="tape-val">${total === null ? "—" : money(total)}${mark(totOk)}${!totOk && total !== null ? `<span class="tape-diff">off by ${money(Math.abs(total - computed))}</span>` : ""}</span></div>
    </div></section>`;
  }

  function renderReviewTab() {
    return [banner(), renderChecks(), renderFields(), renderLines(), renderTape()].join("");
  }

  // ------------------------------------------------------------------ QuickBooks & history tabs
  async function renderQboTab() {
    const inv = state.inv;
    const work = $("#work");
    if (!state.preview) {
      work.innerHTML = `<p class="muted">Loading…</p>`;
      try { state.preview = await window.api(`/api/invoices/${inv.id}/quickbooks-preview`, { method: "GET" }); }
      catch (err) { work.innerHTML = `<p class="muted">${esc(err.message)}</p>`; return; }
      if (state.tab !== "qbo") return;
    }
    const p = state.preview;
    const mode = p.mode === "sandbox" ? "QuickBooks Online sandbox" : "QuickBooks simulator";
    const intro = p.posted
      ? `<dl class="kv"><dt>Bill</dt><dd>${esc(inv.qbo_bill_id)}</dd><dt>Created in</dt><dd>${mode}</dd>
          <dt>Posted</dt><dd>${fmtTime(inv.posted_at)}</dd><dt>Status</dt><dd>Unpaid, balance ${money(inv.total)}</dd></dl>`
      : `<p class="muted">This is exactly what will be sent to the ${mode} Accounting API (<code>POST /v3/company/{realm}/bill</code>) when the invoice is approved.
         QuickBooks has no draft state for bills, so it’s created unpaid — nothing is paid until someone schedules payment.</p>`;
    work.innerHTML = `<section><h3>${p.posted ? "Bill in QuickBooks" : "Bill that will be created"}</h3>${intro}
      <pre class="payload">${esc(JSON.stringify(p.payload, null, 2))}</pre></section>`;
  }

  function renderHistoryTab() {
    const items = state.inv.events.slice().reverse().map((e) => `
      <li class="t-${esc(e.action)}"><div>${esc(e.summary)}</div>
        <div class="t-meta"><strong>${e.actor === "system" ? "InvoiceFlow" : esc(e.actor)}</strong>, ${fmtTime(e.ts)} · <code title="${esc(e.hash)}">${esc(e.hash.slice(0, 10))}</code></div></li>`).join("");
    $("#work").innerHTML = `<section><h3>History <span class="muted">every action is hash-chained</span></h3><ol class="timeline">${items}</ol></section>`;
  }

  // ------------------------------------------------------------------ render all
  function render() {
    renderHeader();
    renderActions();
    document.querySelectorAll(".wtab").forEach((t) => {
      const on = t.dataset.tab === state.tab;
      t.classList.toggle("active", on);
      t.setAttribute("aria-selected", on ? "true" : "false");
    });
    if (state.tab === "review") $("#work").innerHTML = renderReviewTab();
    else if (state.tab === "qbo") renderQboTab();
    else renderHistoryTab();
    $("#savebar").hidden = !isDirty();
    drawHighlights();
  }

  function refreshLive() {
    // light re-render after typing: keep focus by only updating tape, save bar and actions
    const tapeSection = document.querySelector(".tape");
    if (tapeSection) tapeSection.closest("section").outerHTML = renderTape();
    $("#savebar").hidden = !isDirty();
    renderActions();
  }

  function apply(data, message) {
    state.inv = data;
    resetDraft();
    render();
    if (message) window.toast(message);
  }

  // ------------------------------------------------------------------ dialog
  function ask({ title, body = "", label = "", placeholder = "", required = false, okLabel = "OK", value = "" }) {
    const dialog = $("#dialog");
    $("#dialog-title").textContent = title;
    $("#dialog-body").textContent = body;
    $("#dialog-label").textContent = label;
    $("#dialog-field").hidden = !label;
    const input = $("#dialog-input");
    input.value = value;
    input.placeholder = placeholder;
    input.required = required;
    $("#dialog-ok").textContent = okLabel;
    dialog.showModal();
    if (label) setTimeout(() => input.focus(), 30);
    return new Promise((resolve) => {
      dialog.addEventListener("close", function handler() {
        dialog.removeEventListener("close", handler);
        resolve(dialog.returnValue === "ok" ? input.value.trim() : null);
      });
    });
  }

  // ------------------------------------------------------------------ actions
  function payload(extra = {}) {
    const body = {};
    if (Object.keys(state.draftFields).length) body.fields = { ...state.draftFields };
    if (state.confirm.size) body.confirm = [...state.confirm];
    if (state.linesDirty || extra.confirmLines) {
      body.line_items = state.lines.map((l) => ({ description: l.description, quantity: numOr0(l.quantity),
        unit_price: numOr0(l.unit_price), amount: numOr0(l.amount), confirmed: !!extra.confirmLines }));
    }
    if (extra.confirm) body.confirm = [...new Set([...(body.confirm || []), ...extra.confirm])];
    return body;
  }

  async function save(extra = {}, message = "Saved. All checks re-run.") {
    try {
      const data = await window.api(`/api/invoices/${state.inv.id}/save`, { body: payload(extra) });
      apply(data, message);
    } catch (err) { window.toast(err.message, true); }
  }

  async function run(path, body, message) {
    try {
      const data = await window.api(path, { body });
      apply(data, message);
      return data;
    } catch (err) { window.toast(err.message, true); return null; }
  }

  async function approve() {
    const inv = state.inv;
    const note = await ask({
      title: inv.status === "awaiting_second_approval" ? "Give the second approval?" : "Approve this invoice?",
      body: `${inv.vendor_name}, invoice ${inv.invoice_number}, ${money(inv.total)}. ` +
        ((inv.total || 0) >= inv.second_approval_limit && inv.status !== "awaiting_second_approval"
          ? "This is the first of two approvals; nothing is posted yet."
          : "An unpaid bill will be created in QuickBooks."),
      label: "Note for the audit trail (optional)", okLabel: "Approve",
    });
    if (note === null) return;
    const data = await run(`/api/invoices/${inv.id}/approve`, { note }, "Approved");
    if (data && data.status === "posted") window.toast(`Approved. Bill ${data.qbo_bill_id} created in QuickBooks.`);
    if (data && data.status === "awaiting_second_approval") window.toast("First approval recorded. A second reviewer must approve.");
  }

  async function reject(prefill = "") {
    const reason = await ask({
      title: "Reject this invoice?", body: "It leaves the queue and nothing is posted. The reason is kept in the audit trail.",
      label: "Reason", required: true, okLabel: "Reject invoice", value: prefill,
      placeholder: "e.g. Subtotal doesn't match line items — asked vendor for a corrected invoice",
    });
    if (reason === null) return;
    await run(`/api/invoices/${state.inv.id}/reject`, { reason }, "Invoice rejected");
  }

  document.addEventListener("click", async (e) => {
    const t = e.target.closest("button, [data-tab]");
    if (!t) return;
    if (t.dataset.tab) { state.tab = t.dataset.tab; render(); return; }
    if (t.dataset.zoom !== undefined) {
      const z = Number(t.dataset.zoom);
      state.zoom = z === 0 ? 0 : Math.max(-1, Math.min(3, state.zoom + z));
      applyZoom(); return;
    }
    if (t.dataset.act === "approve") return approve();
    if (t.dataset.act === "reject") return reject();
    if (t.dataset.act === "retry") return run(`/api/invoices/${state.inv.id}/retry-post`, {}, "Posting retried");
    if (t.dataset.act === "reprocess") {
      window.busy(true, "Reading the file again…", "Scanned pages take about 10 seconds.");
      await run(`/api/invoices/${state.inv.id}/reprocess`, {}, "Read again");
      window.busy(false); return;
    }
    if (t.dataset.action === "save") return save();
    if (t.dataset.action === "discard") { resetDraft(); render(); return; }
    if (t.dataset.show) { focusEvidence(t.dataset.show); return; }
    if (t.dataset.edit) {
      const input = document.getElementById(`f-${t.dataset.edit}`);
      if (input) { input.focus(); input.select(); }
      return;
    }
    if (t.dataset.confirm) return save({ confirm: [t.dataset.confirm] }, "Confirmed");
    if (t.hasAttribute("data-confirm-lines")) return save({ confirmLines: true }, "Line items confirmed");
    if (t.dataset.applyFix !== undefined) {
      const row = Number(t.dataset.applyFix);
      state.lines[row][t.dataset.fixField] = Number(t.dataset.fixValue);
      state.linesDirty = true;
      return save({}, `Line ${row + 1} corrected`);
    }
    if (t.hasAttribute("data-add-vendor")) return run(`/api/invoices/${state.inv.id}/vendor`, {}, "Vendor added to the approved list");
    if (t.dataset.ack) {
      const note = await ask({ title: "Acknowledge this check?", body: "Say what you verified so auditors can see why it was cleared.",
        label: "What did you check?", okLabel: "Acknowledge" });
      if (note === null) return;
      return run(`/api/invoices/${state.inv.id}/issues/${t.dataset.ack}/acknowledge`, { note }, "Acknowledged");
    }
    if (t.dataset.release) {
      const note = await ask({ title: "Release the duplicate hold?",
        body: "Only do this if the vendor confirmed it’s a separate charge. Your reason is recorded.",
        label: "Why is this not a duplicate?", required: true, okLabel: "Release hold" });
      if (note === null) return;
      return run(`/api/invoices/${state.inv.id}/issues/${t.dataset.release}/acknowledge`, { note }, "Hold released");
    }
    if (t.dataset.rejectReason !== undefined) return reject(t.dataset.rejectReason);
    if (t.hasAttribute("data-add-line")) {
      state.lines.push({ description: "", quantity: 1, unit_price: 0, amount: 0, source: "reviewer", conf: 1 });
      state.linesDirty = true; render();
      const inputs = document.querySelectorAll(`input[data-line="${state.lines.length - 1}"]`);
      if (inputs[0]) inputs[0].focus();
      return;
    }
    if (t.dataset.del !== undefined) {
      state.lines.splice(Number(t.dataset.del), 1);
      state.linesDirty = true; render(); return;
    }
  });

  document.addEventListener("focusin", (e) => {
    const t = e.target;
    if (t.dataset && t.dataset.field) focusEvidence(t.dataset.field);
    else if (t.dataset && t.dataset.line !== undefined) focusEvidence(`line:${t.dataset.line}`);
  });

  document.addEventListener("input", (e) => {
    const t = e.target;
    if (t.dataset.field) {
      const key = t.dataset.field;
      const original = state.inv.fields[key] ? state.inv.fields[key].value : null;
      const def = FIELD_DEFS.find((d) => d.key === key);
      const same = def.type === "money" ? numOrNull(t.value) === (original === null ? null : Number(original))
        : String(t.value).trim() === String(original ?? "").trim();
      if (same) delete state.draftFields[key]; else state.draftFields[key] = t.value;
      t.closest(".field").classList.toggle("changed", !same);
      refreshLive();
    } else if (t.dataset.line !== undefined) {
      const row = Number(t.dataset.line);
      const k = t.dataset.k;
      state.lines[row][k] = k === "description" ? t.value : t.value;
      if ((k === "quantity" || k === "unit_price") && document.activeElement === t) {
        // keep amount in step when the reviewer fixes quantity or price
        const q = numOr0(state.lines[row].quantity), p = numOr0(state.lines[row].unit_price);
        const amountInput = document.querySelector(`input[data-line="${row}"][data-k="amount"]`);
        const amt = Math.round(q * p * 100) / 100;
        state.lines[row].amount = amt;
        if (amountInput) amountInput.value = fmtNum(amt);
      }
      state.linesDirty = true;
      refreshLive();
    }
  });

  document.addEventListener("keydown", (e) => {
    if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === "s" && isDirty()) { e.preventDefault(); save(); }
  });
  window.addEventListener("beforeunload", (e) => { if (isDirty()) { e.preventDefault(); e.returnValue = ""; } });
  document.addEventListener("actorchange", renderActions);

  resetDraft();
  renderDocument();
  render();
})();

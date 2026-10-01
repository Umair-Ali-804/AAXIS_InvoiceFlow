/* Shared behaviour: reviewer identity, API calls, uploads, feedback. */
(function () {
  const ACTOR_KEY = "invoiceflow.actor";
  const select = document.getElementById("actor-select");

  function readActor() {
    try { return localStorage.getItem(ACTOR_KEY) || "Maya Chen"; } catch (e) { return "Maya Chen"; }
  }
  window.currentActor = readActor;
  if (select) {
    select.value = readActor();
    select.addEventListener("change", () => {
      try { localStorage.setItem(ACTOR_KEY, select.value); } catch (e) { /* storage unavailable */ }
      toast(`Now reviewing as ${select.value}`);
      document.dispatchEvent(new CustomEvent("actorchange"));
    });
  }

  async function api(path, options = {}) {
    const headers = { "X-Actor": readActor(), ...(options.headers || {}) };
    let body = options.body;
    if (body && !(body instanceof FormData)) {
      headers["Content-Type"] = "application/json";
      body = JSON.stringify(body);
    }
    const res = await fetch(path, { method: options.method || "POST", headers, body });
    let data = null;
    try { data = await res.json(); } catch (e) { /* non-JSON */ }
    if (!res.ok) {
      const message = (data && (data.error || data.detail)) || `Request failed (${res.status})`;
      throw new Error(typeof message === "string" ? message : JSON.stringify(message));
    }
    return data;
  }
  window.api = api;

  let toastTimer;
  function toast(message, isError = false) {
    const el = document.getElementById("toast");
    if (!el) return;
    el.textContent = message;
    el.classList.toggle("error", isError);
    el.classList.add("show");
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => el.classList.remove("show"), isError ? 6000 : 3200);
  }
  window.toast = toast;

  function busy(on, title, sub) {
    const el = document.getElementById("busy");
    if (!el) return;
    if (title) document.getElementById("busy-title").textContent = title;
    if (sub) document.getElementById("busy-sub").textContent = sub;
    el.hidden = !on;
  }
  window.busy = busy;

  async function uploadFiles(files) {
    if (!files || !files.length) return;
    const form = new FormData();
    [...files].forEach((f) => form.append("files", f));
    busy(true, `Reading ${files.length} file${files.length > 1 ? "s" : ""}…`,
      "Text PDFs take a moment. Scanned pages go through OCR and take about 10 seconds each.");
    try {
      const data = await api("/api/invoices/upload", { body: form });
      if (data.errors && data.errors.length) toast(data.errors.join(" "), true);
      if (data.created.length === 1) {
        window.location.href = `/invoices/${data.created[0]}`;
      } else {
        window.location.href = "/invoices?queue=all";
      }
    } catch (err) {
      busy(false);
      toast(err.message, true);
    }
  }

  const dropzone = document.getElementById("dropzone");
  const input = document.getElementById("file-input");
  if (dropzone && input) {
    input.addEventListener("change", () => uploadFiles(input.files));
    ["dragenter", "dragover"].forEach((t) => dropzone.addEventListener(t, (e) => {
      e.preventDefault(); dropzone.classList.add("over");
    }));
    ["dragleave", "drop"].forEach((t) => dropzone.addEventListener(t, () => dropzone.classList.remove("over")));
    dropzone.addEventListener("drop", (e) => { e.preventDefault(); uploadFiles(e.dataTransfer.files); });
  }

  document.addEventListener("click", async (e) => {
    const button = e.target.closest("[data-action]");
    if (button) {
      const action = button.dataset.action;
      if (action === "load-demo") {
        busy(true, "Loading the five demo invoices…",
          "One is a scanned page, so OCR takes about 10 seconds. The rest are instant.");
        try {
          await api("/api/demo/load");
          window.location.href = "/invoices?queue=all";
        } catch (err) { busy(false); toast(err.message, true); }
      } else if (action === "reset-demo") {
        if (!confirm("Delete every invoice and audit entry in this workspace? Vendors and settings are kept.")) return;
        try { await api("/api/demo/reset"); window.location.href = "/"; }
        catch (err) { toast(err.message, true); }
      } else if (action === "poll-email") {
        busy(true, "Checking the AP inbox…", "Unread messages with invoice attachments are imported.");
        try {
          const data = await api("/api/email/poll");
          busy(false);
          toast(`${data.messages} message(s), ${data.created.length} invoice(s) imported`);
          if (data.created.length) setTimeout(() => (window.location.href = "/invoices?queue=all"), 900);
        } catch (err) { busy(false); toast(err.message, true); }
      }
      return;
    }
    const row = e.target.closest("tr[data-href]");
    if (row && !e.target.closest("a")) window.location.href = row.dataset.href;
  });

  document.addEventListener("keydown", (e) => {
    const row = e.target.closest && e.target.closest("tr[data-href]");
    if (row && e.key === "Enter") window.location.href = row.dataset.href;
  });
})();

// MediClear frontend: plain JavaScript, no build tools needed.

const $ = (id) => document.getElementById(id);
let current = null;      // the consultation being viewed
let samples = [];

// ---------- helpers ----------
function esc(s) {
  return String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}
function money(n) {
  const cur = current?.result?.bill?.currency || "Rs.";
  return `${cur} ${Number(n || 0).toLocaleString()}`;
}
function list(items) {
  if (!items || !items.length) return '<p class="empty">Nothing mentioned</p>';
  return `<ul class="plain">${items.map((i) => `<li>${esc(i)}</li>`).join("")}</ul>`;
}
async function api(path, options = {}) {
  const res = await fetch(path, options);
  let data = null;
  try { data = await res.json(); } catch { /* no body */ }
  if (!res.ok) throw new Error(data?.detail || `Request failed (${res.status})`);
  return data;
}
function isApproved() { return current?.status === "approved"; }

// Highlight glossary terms inside plain text. Returns safe HTML.
function highlight(text, hits) {
  const ranges = [];
  for (const h of hits || []) {
    const caseSensitive = h.term === h.term.toUpperCase() && h.term.length <= 4;
    const re = new RegExp(`(?<![\\w/])${h.term.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")}(?![\\w/])`, caseSensitive ? "g" : "gi");
    let m;
    while ((m = re.exec(text))) {
      const r = { start: m.index, end: m.index + m[0].length, meaning: h.meaning };
      if (!ranges.some((x) => r.start < x.end && r.end > x.start)) ranges.push(r);
    }
  }
  ranges.sort((a, b) => a.start - b.start);
  let html = "", pos = 0;
  for (const r of ranges) {
    html += esc(text.slice(pos, r.start));
    html += `<mark class="term" data-meaning="${esc(r.meaning)}" title="${esc(r.meaning)}">${esc(text.slice(r.start, r.end))}</mark>`;
    pos = r.end;
  }
  return html + esc(text.slice(pos));
}

// ---------- status + history ----------
async function checkStatus() {
  const el = $("status");
  try {
    const h = await api("/api/health");
    if (!h.ollama.running) { el.textContent = "AI not running: start Ollama"; el.className = "status bad"; }
    else if (!h.ollama.model_downloaded) { el.textContent = `Model missing: ollama pull ${h.ollama.model}`; el.className = "status bad"; }
    else { el.textContent = `AI ready · ${h.ollama.model}`; el.className = "status ok"; }
  } catch { el.textContent = "Server not reachable"; el.className = "status bad"; }
}

async function loadHistory() {
  const items = await api("/api/consultations");
  $("history").innerHTML = items.length ? items.map((c) => `
    <li data-id="${c.id}" class="${current?.id === c.id ? "active" : ""}">
      <div class="h-name">${esc(c.patient_name || "Unnamed patient")}</div>
      <div class="h-meta"><span>${esc(c.created_at.replace("T", " ").slice(0, 16))}</span>
        <span class="badge ${c.status}">${c.status}</span></div>
    </li>`).join("") : '<li class="muted">No consultations yet</li>';
  $("history").querySelectorAll("li[data-id]").forEach((li) =>
    li.addEventListener("click", async () => showResult(await api(`/api/consultations/${li.dataset.id}`))));
}

// ---------- new consultation ----------
function showNew() {
  current = null;
  $("view-result").classList.add("hidden");
  $("view-new").classList.remove("hidden");
  $("form-new").reset();
  $("drop-text").innerHTML = '<b>Choose a recording or video</b> or drag it here<br><small>mp3, wav, m4a, mp4, mov, webm… or a .txt transcript</small>';
  loadHistory();
}

async function loadSamples() {
  samples = await api("/api/samples");
  $("sample-select").innerHTML = '<option value="">Load a sample conversation…</option>' +
    samples.map((s, i) => `<option value="${i}">${esc(s.name)}</option>`).join("");
}

$("sample-select").addEventListener("change", (e) => {
  if (e.target.value !== "") $("transcript").value = samples[e.target.value].text;
});
$("file").addEventListener("change", () => {
  const f = $("file").files[0];
  if (f) $("drop-text").innerHTML = `<b>${esc(f.name)}</b><br><small>${(f.size / 1048576).toFixed(1)} MB · click to change</small>`;
});
["dragover", "dragenter"].forEach((ev) => $("drop").addEventListener(ev, (e) => { e.preventDefault(); $("drop").classList.add("over"); }));
["dragleave", "drop"].forEach((ev) => $("drop").addEventListener(ev, () => $("drop").classList.remove("over")));
$("drop").addEventListener("drop", (e) => {
  e.preventDefault();
  if (e.dataTransfer.files.length) { $("file").files = e.dataTransfer.files; $("file").dispatchEvent(new Event("change")); }
});

$("form-new").addEventListener("submit", async (e) => {
  e.preventDefault();
  const err = $("new-error");
  err.classList.add("hidden");
  const fd = new FormData();
  const f = $("file").files[0];
  if (f) fd.append("file", f);
  fd.append("transcript", $("transcript").value);
  fd.append("patient_name", $("patient-name").value);
  fd.append("label_speakers", $("label-speakers").checked);
  if (!f && !$("transcript").value.trim()) {
    err.textContent = "Upload a recording or paste a transcript first."; err.classList.remove("hidden"); return;
  }
  $("form-new").classList.add("hidden");
  $("processing").classList.remove("hidden");
  try {
    const c = await api("/api/process", { method: "POST", body: fd });
    showResult(c);
  } catch (ex) {
    err.textContent = ex.message; err.classList.remove("hidden");
  } finally {
    $("form-new").classList.remove("hidden");
    $("processing").classList.add("hidden");
  }
});

// ---------- result ----------
function showResult(c) {
  current = c;
  $("view-new").classList.add("hidden");
  $("view-result").classList.remove("hidden");
  $("r-name").value = c.patient_name || "";
  const tr = c.result.transcript || {};
  const lang = tr.language_name ? ` · ${tr.language_name}${tr.translated ? " → translated to English" : ""}` : "";
  $("r-meta").textContent = `#${c.id} · ${c.source} · ${c.created_at.replace("T", " ")}${lang}` +
    (c.result.timings?.total ? ` · processed in ${c.result.timings.total}s` : "");
  $("r-status").textContent = c.status;
  $("r-status").className = `badge ${c.status}`;
  const locked = isApproved();
  $("btn-save").classList.toggle("hidden", locked);
  $("btn-approve").classList.toggle("hidden", locked);
  $("btn-reopen").classList.toggle("hidden", !locked);
  $("r-name").disabled = locked;
  $("r-warnings").innerHTML = locked ? "" : [...(c.result.warnings || []), ...(c.result.bill.warnings || [])].map((w) => `<div class="warning">⚠ ${esc(w)}</div>`).join("");
  renderSummary(); renderConversation(); renderSoap(); renderSimple(); renderCodes(); renderBill();
  loadHistory();
}

function renderSummary() {
  const x = current.result.extracted;
  const meds = current.result.medicines_plain || [];
  $("tab-summary").innerHTML = `
  <div class="grid">
    <div class="card"><h2>Chief complaint</h2><p>${esc(x.chief_complaint) || '<span class="empty">Not stated</span>'}</p>
      <h3>Visit type</h3><p>${x.visit_type === "follow_up" ? "Follow-up" : "New visit"}</p></div>
    <div class="card"><h2>Diagnoses</h2>${x.diagnoses.length ? `<table>
      ${x.diagnoses.map((d) => `<tr><td><b>${esc(d.name)}</b><br><small class="muted">“${esc(d.doctor_words)}”</small></td>
      <td><span class="chip ${d.status}">${d.status.replace("_", " ")}</span></td></tr>`).join("")}</table>` : '<p class="empty">None stated</p>'}</div>
    <div class="card"><h2>Symptoms</h2>${x.symptoms.length ? `<table><tr><th>Symptom</th><th>Said as</th><th>For</th></tr>
      ${x.symptoms.map((s) => `<tr><td>${esc(s.name)}</td><td class="muted">${esc(s.patient_words)}</td><td>${esc(s.duration)}</td></tr>`).join("")}</table>` : '<p class="empty">None</p>'}</div>
    <div class="card"><h2>Vitals & examination</h2>
      ${x.vitals.length ? `<table>${x.vitals.map((v) => `<tr><td>${esc(v.name)}</td><td><b>${esc(v.value)}</b></td></tr>`).join("")}</table>` : '<p class="empty">No vitals recorded</p>'}
      <h3>Findings</h3>${list(x.exam_findings)}</div>
    <div class="card" style="grid-column: 1 / -1"><h2>Medicines</h2>${meds.length ? `<table>
      <tr><th>Medicine</th><th>As written</th><th>In plain words</th></tr>
      ${meds.map((m) => `<tr><td><b>${esc(m.name)}</b>${m.is_new ? "" : ' <small class="muted">(continued)</small>'}
        ${m.heard_as ? `<br><span class="chip review">heard as “${esc(m.heard_as)}”</span>` : ""}</td>
        <td>${esc([m.dose, m.frequency, m.duration].filter(Boolean).join(" · "))}</td><td>${esc(m.plain)}</td></tr>`).join("")}</table>` : '<p class="empty">No medicines</p>'}</div>
    <div class="card"><h2>Tests ordered</h2>${list(x.tests_ordered)}<h3>Follow-up</h3><p>${esc(x.follow_up) || '<span class="empty">Not stated</span>'}</p></div>
    <div class="card"><h2>History & allergies</h2>${list(x.history)}<h3>Allergies</h3>${list(x.allergies)}</div>
    <div class="card"><h2>Advice given</h2>${list(x.advice)}</div>
  </div>`;
}

function renderConversation() {
  const r = current.result;
  $("tab-conversation").innerHTML = `
    <p class="muted">Highlighted words are medical terms. Hover or tap them to see what they mean.</p>
    ${r.dialogue.map((d) => `<div class="bubble ${esc(d.speaker)}"><div class="who">${esc(d.speaker)}</div>${highlight(d.text, r.glossary_hits)}</div>`).join("")}
    ${glossaryHtml()}`;
}

function glossaryHtml() {
  const hits = current.result.glossary_hits || [];
  if (!hits.length) return "";
  return `<h3>Medical words in this consultation</h3><div class="glossary">
    ${hits.map((h) => `<div><b>${esc(h.term)}</b><br>${esc(h.meaning)}</div>`).join("")}</div>`;
}

function renderSoap() {
  const s = current.result.notes.soap;
  const dis = isApproved() ? "disabled" : "";
  const parts = [["subjective", "S", "Subjective: what the patient reports"], ["objective", "O", "Objective: vitals and examination"],
    ["assessment", "A", "Assessment: diagnosis"], ["plan", "P", "Plan: treatment, tests, follow-up"]];
  $("tab-soap").innerHTML = `<div class="card soap">
    ${parts.map(([k, l, t]) => `<label><span class="letter">${l}</span>${t}
      <textarea data-soap="${k}" rows="${Math.max(3, (s[k] || "").split("\n").length + 1)}" ${dis}>${esc(s[k])}</textarea></label>`).join("")}
    <p class="muted">Edit anything that's wrong, then press <b>Save changes</b>.</p></div>`;
}

function renderSimple() {
  const r = current.result;
  const dis = isApproved() ? "disabled" : "";
  $("tab-simple").innerHTML = `
    <div class="card"><h2>What the doctor said, in simple words</h2>
      <textarea id="patient-summary" class="simple-summary" rows="${Math.max(6, (r.notes.patient_summary || "").split("\n").length + 1)}" ${dis}>${esc(r.notes.patient_summary)}</textarea></div>
    <div class="card"><h2>Your medicines</h2>${(r.medicines_plain || []).length ? `<table>
      ${r.medicines_plain.map((m) => `<tr><td><b>${esc(m.name)}</b></td><td>${esc(m.plain)}</td></tr>`).join("")}</table>` : '<p class="empty">No medicines</p>'}</div>
    ${glossaryHtml()}`;
}

function renderCodes() {
  const c = current.result.coding;
  const dis = isApproved() ? "disabled" : "";
  $("tab-codes").innerHTML = `
    <div class="card"><h2>ICD-10 codes</h2>
      <p class="muted">Matched from the code list by meaning, so different wording for the same illness gets the same code. Yellow ones need a check.</p>
      <table id="codes-table"><tr><th>Code</th><th>Description</th><th>For</th><th>Type</th><th>Match</th><th></th></tr>
      ${c.codes.map((x, i) => `<tr data-i="${i}">
        <td><input data-f="code" value="${esc(x.code)}" style="width:90px" ${dis}></td>
        <td><input data-f="description" value="${esc(x.description)}" ${dis}></td>
        <td>${esc(x.for)}</td><td>${esc(x.kind)}</td>
        <td><span class="chip ${x.needs_review ? "review" : "auto"}">${x.score ?? "manual"}${x.needs_review ? " · check" : ""}</span></td>
        <td>${dis ? "" : `<button class="btn" data-remove="${i}">✕</button>`}</td></tr>`).join("")}
      </table>
      ${dis ? "" : '<button class="btn" id="btn-add-code" style="margin-top:10px">+ Add code</button>'}
    </div>
    <div class="card"><h2>Not coded</h2>${c.not_coded.length ? `<table>
      ${c.not_coded.map((n) => `<tr><td><b>${esc(n.item)}</b></td><td class="muted">${esc(n.reason)}</td></tr>`).join("")}</table>` : '<p class="empty">Everything was coded</p>'}</div>`;
  $("tab-codes").querySelectorAll("[data-remove]").forEach((b) => b.addEventListener("click", () => {
    collectCodes(); current.result.coding.codes.splice(+b.dataset.remove, 1); renderCodes();
  }));
  $("btn-add-code")?.addEventListener("click", () => {
    collectCodes();
    current.result.coding.codes.push({ code: "", description: "", for: "Added by doctor", kind: "Manual", score: null, needs_review: false, source: "manual" });
    renderCodes();
  });
}

function collectCodes() {
  document.querySelectorAll("#codes-table tr[data-i]").forEach((tr) => {
    const item = current.result.coding.codes[+tr.dataset.i];
    tr.querySelectorAll("input[data-f]").forEach((inp) => (item[inp.dataset.f] = inp.value.trim()));
  });
}

function renderBill() {
  const b = current.result.bill;
  const dis = isApproved() ? "disabled" : "";
  $("tab-bill").innerHTML = `
    <div class="card"><h2>${b.status === "approved" ? "Final bill" : "Draft bill"}</h2>
      <table id="bill-table"><tr><th></th><th>Service</th><th>Why it's here</th><th style="text-align:right">Price</th></tr>
      ${b.lines.map((l, i) => `<tr data-i="${i}">
        <td><input type="checkbox" data-f="include" ${l.include ? "checked" : ""} ${dis}></td>
        <td><b>${esc(l.description)}</b><br><small class="muted">${esc(l.service_code)}</small>
          ${l.needs_review ? `<br><span class="chip review">${esc(l.note || "check")}</span>` : ""}</td>
        <td class="muted">${esc(l.source)}</td>
        <td style="text-align:right"><input type="number" min="0" step="50" class="price-input" data-f="price" value="${l.price}" ${dis}></td></tr>`).join("")}
      <tr class="total-row"><td></td><td colspan="2">Total</td><td style="text-align:right" id="bill-total">${money(b.total)}</td></tr>
      </table>
      <p class="muted">Diagnosis codes on this bill: ${current.result.coding.codes.map((c) => esc(c.code)).join(", ") || "none"}</p>
    </div>
    <div class="grid">
      <div class="card"><h2>Included in consultation</h2>${list(b.included_in_consultation)}</div>
      <div class="card"><h2>Not billed here</h2>${b.not_billed.length ? `<table>
        ${b.not_billed.map((n) => `<tr><td><b>${esc(n.item)}</b></td><td class="muted">${esc(n.reason)}</td></tr>`).join("")}</table>` : '<p class="empty">Nothing</p>'}</div>
    </div>`;
  $("bill-table").addEventListener("input", () => { collectBill(); $("bill-total").textContent = money(current.result.bill.total); });
}

function collectBill() {
  const b = current.result.bill;
  document.querySelectorAll("#bill-table tr[data-i]").forEach((tr) => {
    const line = b.lines[+tr.dataset.i];
    line.include = tr.querySelector('[data-f="include"]').checked;
    line.price = parseFloat(tr.querySelector('[data-f="price"]').value) || 0;
  });
  b.total = b.lines.filter((l) => l.include).reduce((s, l) => s + l.price, 0);
}

// ---------- save / approve ----------
async function saveEdits() {
  collectCodes(); collectBill();
  const soap = {};
  document.querySelectorAll("[data-soap]").forEach((t) => (soap[t.dataset.soap] = t.value));
  const body = {
    patient_name: $("r-name").value,
    soap,
    patient_summary: $("patient-summary").value,
    codes: current.result.coding.codes,
    bill_lines: current.result.bill.lines,
  };
  const c = await api(`/api/consultations/${current.id}`, {
    method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body),
  });
  const tab = document.querySelector(".tabs .active").dataset.tab;
  showResult(c); switchTab(tab);
  return c;
}

$("btn-save").addEventListener("click", async () => {
  try { await saveEdits(); } catch (e) { alert(e.message); }
});
$("btn-approve").addEventListener("click", async () => {
  const needsCheck = current.result.bill.lines.some((l) => l.include && l.needs_review && !l.price) ||
    current.result.coding.codes.some((c) => c.needs_review);
  const msg = needsCheck
    ? "Some codes or prices are flagged for checking. Approve anyway?"
    : "Approve this note, codes and bill? It will be locked.";
  if (!confirm(msg)) return;
  try { await saveEdits(); showResult(await api(`/api/consultations/${current.id}/approve`, { method: "POST" })); }
  catch (e) { alert(e.message); }
});
$("btn-reopen").addEventListener("click", async () => {
  showResult(await api(`/api/consultations/${current.id}/reopen`, { method: "POST" }));
});
$("btn-delete").addEventListener("click", async () => {
  if (!confirm("Delete this consultation permanently?")) return;
  await api(`/api/consultations/${current.id}`, { method: "DELETE" });
  showNew();
});

$("btn-print").addEventListener("click", () => {
  if (!isApproved()) collectBill();
  const b = current.result.bill;
  const name = $("r-name").value || "Patient";
  $("print-area").innerHTML = `
    <h1>Medical bill${b.status === "approved" ? "" : " (DRAFT, not approved)"}</h1>
    <p><b>Patient:</b> ${esc(name)}<br><b>Date:</b> ${esc(current.created_at.slice(0, 10))}<br><b>Ref:</b> #${current.id}</p>
    <table><tr><th>Service</th><th>Code</th><th style="text-align:right">Price</th></tr>
      ${b.lines.filter((l) => l.include).map((l) => `<tr><td>${esc(l.description)}</td><td>${esc(l.service_code)}</td><td style="text-align:right">${money(l.price)}</td></tr>`).join("")}
      <tr class="total-row"><td colspan="2">Total</td><td style="text-align:right">${money(b.total)}</td></tr></table>
    <p><b>Diagnosis (ICD-10):</b> ${current.result.coding.codes.map((c) => `${esc(c.code)} ${esc(c.description)}`).join("; ") || "none"}</p>`;
  window.print();
});

// ---------- tabs ----------
function switchTab(name) {
  document.querySelectorAll(".tabs button").forEach((b) => b.classList.toggle("active", b.dataset.tab === name));
  document.querySelectorAll(".tab").forEach((t) => t.classList.toggle("hidden", t.id !== `tab-${name}`));
}
$("tabs").addEventListener("click", (e) => {
  if (e.target.dataset.tab) {
    if (!isApproved() && current) { collectCodes(); collectBill(); }
    switchTab(e.target.dataset.tab);
  }
});

$("btn-new").addEventListener("click", showNew);

// ---------- start ----------
checkStatus();
setInterval(checkStatus, 15000);
loadSamples();
loadHistory();

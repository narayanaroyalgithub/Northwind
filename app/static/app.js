let employees = [];

const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));

async function api(path, options = {}) {
  const res = await fetch(path, options);
  if (!res.ok) {
    const err = await res.json().catch(() => ({ error: res.statusText }));
    throw new Error(err.error || res.statusText);
  }
  return res.json();
}

function money(value) {
  return value == null ? "amount unknown" : `$${Number(value).toFixed(2)}`;
}

function titleize(value) {
  return (value || "").replaceAll("_", " ").replace(/\b\w/g, c => c.toUpperCase());
}

function setTab(name) {
  $$("nav button").forEach(btn => btn.classList.toggle("active", btn.dataset.tab === name));
  $$(".panel").forEach(panel => panel.classList.toggle("active", panel.id === `tab-${name}`));
  if (name === "history") loadHistory();
}

async function loadEmployees() {
  employees = await api("/api/employees");
  for (const select of [$("#employee-select"), $("#history-employee")]) {
    const current = select.value;
    select.innerHTML = select.id === "history-employee" ? `<option value="">All employees</option>` : "";
    employees.forEach(emp => {
      const opt = document.createElement("option");
      opt.value = emp.id;
      opt.textContent = `${emp.name} (${emp.id})`;
      select.appendChild(opt);
    });
    if (current) select.value = current;
  }
  hydrateEmployeeFields();
}

function hydrateEmployeeFields() {
  const emp = employees.find(e => e.id === $("#employee-select").value);
  if (!emp) return;
  $("#purpose").placeholder = emp.trip_purpose || "Trip purpose";
  $("#trip-dates").placeholder = emp.trip_dates || "Trip dates";
}

function renderSubmission(submission, root) {
  if (!submission) {
    root.innerHTML = "";
    return;
  }
  root.innerHTML = `
    <div class="submission-summary">
      <div>
        <h2>${escapeHtml(submission.employee_name)}: ${escapeHtml(submission.purpose || "Expense submission")}</h2>
        <p class="meta">${escapeHtml(submission.trip_dates || "")} · ${submission.items.length} receipts · ${money(submission.total_amount)} · ${titleize(submission.status)}</p>
      </div>
    </div>
  `;
  submission.items.forEach(item => root.appendChild(renderItem(item)));
}

function renderItem(item) {
  const tpl = $("#item-template").content.cloneNode(true);
  const article = $(".item", tpl);
  article.classList.add(item.effective_verdict || item.verdict);
  $("h3", tpl).textContent = item.merchant || item.filename;
  $(".meta", tpl).textContent = `${item.filename} · ${titleize(item.category)} · ${money(item.amount)} · confidence ${Math.round((item.confidence || 0) * 100)}%`;
  $(".verdict", tpl).textContent = titleize(item.effective_verdict || item.verdict);
  $(".reasoning", tpl).textContent = item.reasoning;
  $(".receipt-text", tpl).textContent = item.extracted_text || "";
  const citations = $(".citations", tpl);
  (item.citations || []).forEach(c => {
    const div = document.createElement("div");
    div.className = "citation";
    div.innerHTML = `<strong>${escapeHtml(c.label)}</strong><span>${escapeHtml(c.quote)}</span>`;
    citations.appendChild(div);
  });
  const form = $(".override", tpl);
  form.dataset.itemId = item.id;
  form.verdict.value = item.effective_verdict || item.verdict;
  form.addEventListener("submit", saveOverride);
  const note = $(".override-note", tpl);
  if (item.override_verdict) {
    note.textContent = `Override: ${titleize(item.override_verdict)} by ${item.override_by} at ${item.override_at}. Comment: ${item.override_comment}`;
  }
  return tpl;
}

async function saveOverride(event) {
  event.preventDefault();
  const form = event.currentTarget;
  const payload = {
    verdict: form.verdict.value,
    comment: form.comment.value,
    reviewer: "finance-reviewer"
  };
  await api(`/api/items/${form.dataset.itemId}/override`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload)
  });
  form.comment.value = "";
  await loadHistory();
  const current = $("#history-detail").dataset.submissionId;
  if (current) {
    const sub = await api(`/api/submissions/${current}`);
    renderSubmission(sub, $("#history-detail"));
  }
}

async function loadHistory() {
  const params = new URLSearchParams();
  if ($("#history-employee").value) params.set("employee_id", $("#history-employee").value);
  if ($("#history-status").value) params.set("status", $("#history-status").value);
  const rows = await api(`/api/submissions?${params}`);
  const list = $("#history-list");
  list.innerHTML = "";
  rows.forEach(row => {
    const btn = document.createElement("button");
    btn.className = "history-card";
    btn.innerHTML = `<strong>${escapeHtml(row.employee_name)}</strong><span>${escapeHtml(row.purpose || "")}</span><span>${escapeHtml(row.trip_dates || "")}</span><span>${money(row.total_amount)} · ${titleize(row.status)}</span>`;
    btn.addEventListener("click", async () => {
      const sub = await api(`/api/submissions/${row.id}`);
      $("#history-detail").dataset.submissionId = row.id;
      renderSubmission(sub, $("#history-detail"));
    });
    list.appendChild(btn);
  });
}

function escapeHtml(str) {
  return String(str ?? "").replace(/[&<>"']/g, ch => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#039;"
  }[ch]));
}

document.addEventListener("DOMContentLoaded", async () => {
  $$("nav button").forEach(btn => btn.addEventListener("click", () => setTab(btn.dataset.tab)));
  $("#employee-select").addEventListener("change", hydrateEmployeeFields);
  $("#history-employee").addEventListener("change", loadHistory);
  $("#history-status").addEventListener("change", loadHistory);

  $("#employee-form").addEventListener("submit", async event => {
    event.preventDefault();
    const form = event.currentTarget;
    const payload = Object.fromEntries(new FormData(form).entries());
    payload.grade = Number(payload.grade || 1);
    await api("/api/employees", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload)
    });
    form.reset();
    await loadEmployees();
  });

  $("#submission-form").addEventListener("submit", async event => {
    event.preventDefault();
    const form = event.currentTarget;
    const data = new FormData(form);
    const files = $("#receipts").files;
    if (!files.length) {
      alert("Upload at least one receipt.");
      return;
    }
    const sub = await api("/api/submissions", { method: "POST", body: data });
    renderSubmission(sub, $("#current-review"));
    await loadHistory();
  });

  $("#ask-form").addEventListener("submit", async event => {
    event.preventDefault();
    const answer = await api("/api/ask", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ question: $("#question").value })
    });
    $("#answer").innerHTML = `
      <p>${escapeHtml(answer.answer)}</p>
      ${(answer.citations || []).map(c => `<div class="citation"><strong>${escapeHtml(c.label)}</strong><span>${escapeHtml(c.quote)}</span></div>`).join("")}
    `;
  });

  await loadEmployees();
  await loadHistory();
});

// IT Helpdesk frontend. Login is required (Keycloak, OAuth 2.0 Authorization Code + PKCE).
// Agents and admins must set up MFA (TOTP) on first login.
import Keycloak from "https://cdn.jsdelivr.net/npm/keycloak-js@26.1.0/+esm";

const cfg = window.APP_CONFIG;
const kc = new Keycloak({ url: cfg.keycloakUrl, realm: cfg.realm, clientId: cfg.clientId });
const $ = (id) => document.getElementById(id);
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const when = (t) => (t ? new Date(t).toLocaleString() : "-");
const pill = (p) => `<span class="pill ${esc(p)}">${esc(p)}</span>`;
let isStaff = false;
let currentTicket = null;
const chatHistory = [];

function toast(text) {
  $("toast").textContent = text;
  $("toast").hidden = false;
  setTimeout(() => ($("toast").hidden = true), 3000);
}

async function api(path, options = {}) {
  await kc.updateToken(30);
  const res = await fetch(`/api/${path}`, {
    ...options,
    headers: { "Content-Type": "application/json", Authorization: `Bearer ${kc.token}`, ...(options.headers || {}) },
  });
  if (res.status === 204) return null;
  const body = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(typeof body.detail === "string" ? body.detail : `Request failed (${res.status})`);
  return body;
}

// ------------------------------------------------------------------ navigation
function show(view) {
  document.querySelectorAll("main > section").forEach((s) => (s.hidden = s.id !== `view-${view}`));
  document.querySelectorAll("nav button").forEach((b) => b.classList.toggle("active", b.dataset.view === view));
  if (view === "tickets") loadTickets();
  if (view === "sla") loadSla();
}
document.querySelectorAll("nav button").forEach((b) => b.addEventListener("click", () => show(b.dataset.view)));
$("back").addEventListener("click", () => show("tickets"));

// ------------------------------------------------------------------ ticket list
async function loadTickets() {
  const params = new URLSearchParams({ status: $("f-status").value, priority: $("f-priority").value, mine: $("f-mine").checked });
  const tickets = await api(`tickets/tickets?${params}`);
  $("ticket-table").innerHTML = `<tr><th>#</th><th>Priority</th><th>Title</th><th>Category</th><th>Status</th>
      ${isStaff ? "<th>Requester</th><th>Assignee</th>" : ""}<th>Resolve by</th></tr>` +
    tickets.map((t) => `<tr class="clickable" data-id="${t.ticket_id}">
      <td>${t.ticket_id}</td><td>${pill(t.priority)}</td><td>${esc(t.title)}</td><td>${esc(t.category)}</td>
      <td>${esc(t.status)}</td>${isStaff ? `<td>${esc(t.requester_name)}</td><td>${esc(t.assignee_name || "-")}</td>` : ""}
      <td class="${t.resolve_breached ? "breach" : ""}">${when(t.resolve_due_at)}${t.resolve_breached ? " (breached)" : ""}</td></tr>`).join("") ||
    "<tr><td>No tickets yet.</td></tr>";
}
$("filters").addEventListener("submit", (e) => { e.preventDefault(); loadTickets(); });
$("ticket-table").addEventListener("click", (e) => {
  const row = e.target.closest("tr[data-id]");
  if (row) openTicket(Number(row.dataset.id));
});

// ------------------------------------------------------------------ new ticket (+ KB suggestions as you type)
let kbTimer;
$("t-title").addEventListener("input", () => {
  clearTimeout(kbTimer);
  kbTimer = setTimeout(async () => {
    const q = $("t-title").value.trim();
    if (q.length < 4) return ($("kb-hints").innerHTML = "");
    const articles = await api(`chat/kb?q=${encodeURIComponent(q)}`);
    $("kb-hints").innerHTML = articles.length
      ? `<p class="muted">These articles might solve it right away:</p>` + articles.slice(0, 3).map((a) =>
          `<details class="card"><summary>${esc(a.title)}</summary><p>${esc(a.body)}</p></details>`).join("")
      : "";
  }, 400);
});
$("new-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  try {
    const t = await api("tickets/tickets", { method: "POST", body: JSON.stringify({ title: $("t-title").value, description: $("t-desc").value }) });
    $("new-form").reset();
    $("kb-hints").innerHTML = "";
    toast(`Ticket #${t.ticket_id} created as ${t.priority} ${t.category}`);
    openTicket(t.ticket_id);
  } catch (err) { toast(err.message); }
});

// ------------------------------------------------------------------ ticket detail
async function openTicket(id) {
  const t = await api(`tickets/tickets/${id}`);
  currentTicket = t;
  show("detail");
  $("detail").innerHTML = `
    <h2>#${t.ticket_id} ${esc(t.title)} ${pill(t.priority)}</h2>
    <p class="muted">${esc(t.category)} &middot; ${esc(t.status)} &middot; raised by ${esc(t.requester_name)} on ${when(t.created_at)}
      &middot; assignee: ${esc(t.assignee_name || "unassigned")}</p>
    <p class="muted">First response due ${when(t.response_due_at)} ${t.response_breached ? '<span class="breach">(breached)</span>' : ""}
      &middot; Resolution due ${when(t.resolve_due_at)} ${t.resolve_breached ? '<span class="breach">(breached)</span>' : ""}</p>
    <div class="card">${esc(t.description)}</div>
    ${t.ai_suggestion ? `<div class="card suggestion"><b>Suggested fix (${t.triaged_by === "ai" ? "AI" : "knowledge base"}):</b><br>${esc(t.ai_suggestion)}</div>` : ""}
    ${t.events ? `<details><summary>Audit trail (${t.events.length})</summary><ul>${t.events.map((ev) =>
      `<li>${when(ev.created_at)}: ${esc(ev.actor)} ${esc(ev.action)} ${esc(ev.detail)}</li>`).join("")}</ul></details>` : ""}`;
  $("comments").innerHTML = t.comments.map((c) => `<div class="card comment ${c.internal ? "internal" : ""}">
      <b>${esc(c.author_name)}</b> <span class="muted">${when(c.created_at)}${c.internal ? " &middot; internal note" : ""}</span>
      <p>${esc(c.body)}</p></div>`).join("") || '<p class="muted">No replies yet.</p>';
  $("user-actions").hidden = isStaff || t.status !== "RESOLVED";
}

$("comment-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  try {
    await api(`tickets/tickets/${currentTicket.ticket_id}/comments`, { method: "POST", body: JSON.stringify({ body: $("c-body").value, internal: $("c-internal").checked }) });
    $("comment-form").reset();
    openTicket(currentTicket.ticket_id);
  } catch (err) { toast(err.message); }
});

async function patchTicket(update) {
  try {
    await api(`tickets/tickets/${currentTicket.ticket_id}`, { method: "PATCH", body: JSON.stringify(update) });
    toast("Ticket updated");
    openTicket(currentTicket.ticket_id);
  } catch (err) { toast(err.message); }
}
$("update-form").addEventListener("submit", (e) => {
  e.preventDefault();
  const update = { assign_to_me: $("u-assign").checked };
  if ($("u-status").value) update.status = $("u-status").value;
  if ($("u-priority").value) update.priority = $("u-priority").value;
  $("update-form").reset();
  patchTicket(update);
});
$("confirm-fix").addEventListener("click", () => patchTicket({ status: "CLOSED" }));
$("reopen").addEventListener("click", () => patchTicket({ status: "IN_PROGRESS" }));

// ------------------------------------------------------------------ help assistant
function addMsg(role, text) {
  const div = document.createElement("div");
  div.className = `msg ${role}`;
  div.textContent = text;
  $("chat-log").appendChild(div);
}
$("chat-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const message = $("chat-input").value.trim();
  $("chat-input").value = "";
  addMsg("user", message);
  try {
    const res = await api("chat/chat", { method: "POST", body: JSON.stringify({ message, history: chatHistory.slice(-8) }) });
    addMsg("bot", res.reply + (res.sources.length ? `\n\nSources: ${res.sources.join(", ")}` : ""));
    chatHistory.push({ role: "user", content: message }, { role: "assistant", content: res.reply });
  } catch (err) { addMsg("bot", `Sorry, something went wrong: ${err.message}`); }
});

// ------------------------------------------------------------------ SLA dashboard (agents/admins)
async function loadSla() {
  const s = await api("sla/sla/summary");
  $("sla-summary").innerHTML = `<div class="stats">
      <div class="stat"><span class="muted">Overall compliance</span><b>${s.overall_compliance_pct}%</b></div>
      <div class="stat"><span class="muted">Tickets</span><b>${s.total}</b></div>
      ${s.by_priority.map((p) => `<div class="stat">${pill(p.priority)} <span class="muted">target ${p.targets_hours.resolve} h</span>
        <b>${p.compliance_pct}%</b><span class="muted">${p.total} tickets, ${p.open} open, avg ${p.avg_resolve_hours ?? "-"} h, ${p.ai_triaged} AI-triaged</span></div>`).join("")}
    </div>`;
  const breaches = await api("sla/sla/breaches");
  $("sla-breaches").innerHTML = "<tr><th>#</th><th>Priority</th><th>Title</th><th>Status</th><th>Assignee</th><th>Missed</th></tr>" +
    breaches.map((b) => `<tr class="clickable" data-id="${b.ticket_id}"><td>${b.ticket_id}</td><td>${pill(b.priority)}</td><td>${esc(b.title)}</td>
      <td>${esc(b.status)}</td><td>${esc(b.assignee_name || "-")}</td>
      <td>${[b.response_breached && "first response", b.resolve_breached && "resolution"].filter(Boolean).join(", ")}</td></tr>`).join("");
}
$("sla-breaches").addEventListener("click", (e) => {
  const row = e.target.closest("tr[data-id]");
  if (row) openTicket(Number(row.dataset.id));
});

// ------------------------------------------------------------------ notifications
async function loadNotifications() {
  const items = await api("notifications/notifications");
  const unread = items.filter((n) => !n.read).length;
  $("unread").textContent = unread ? `(${unread})` : "";
  $("notifications").innerHTML = items.map((n) => `<div class="${n.read ? "" : "unread"}" data-id="${n.notification_id}" data-ticket="${n.ticket_id ?? ""}">
      ${esc(n.message)}<br><span class="muted">${when(n.created_at)}</span></div>`).join("") || '<p class="muted">No notifications.</p>';
}
$("bell").addEventListener("click", () => ($("notifications").hidden = !$("notifications").hidden));
$("notifications").addEventListener("click", async (e) => {
  const item = e.target.closest("div[data-id]");
  if (!item) return;
  await api(`notifications/notifications/${item.dataset.id}/read`, { method: "POST" });
  $("notifications").hidden = true;
  if (item.dataset.ticket) openTicket(Number(item.dataset.ticket));
  loadNotifications();
});

// ------------------------------------------------------------------ start-up
$("logout").addEventListener("click", () => kc.logout({ redirectUri: window.location.origin }));

async function start() {
  await kc.init({ onLoad: "login-required", pkceMethod: "S256", checkLoginIframe: false });
  isStaff = kc.hasRealmRole("agent") || kc.hasRealmRole("admin");
  document.querySelectorAll(".staff-only").forEach((el) => (el.hidden = !isStaff));
  $("user-name").textContent = `${kc.tokenParsed.preferred_username}${isStaff ? " (agent)" : ""}`;
  show("tickets");
  loadNotifications();
  setInterval(loadNotifications, 30000);
}
start().catch((err) => toast(`Could not start: ${err.message || err}`));

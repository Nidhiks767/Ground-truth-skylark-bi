// Skylark BI Agent — frontend logic.
// Talks to the FastAPI backend (main.py) over fetch(). No build step, no
// framework — kept intentionally simple and readable for a demo/assignment
// scale project. Conversation state lives in memory only (resets on
// refresh), matching the same session-scoped behavior as the Streamlit
// version.

const API = ""; // same-origin; set to e.g. "http://localhost:8000" if serving frontend separately during dev

let conversation = []; // [{role, content}]
const CHART_COLORS = { amber: "#F5A623", teal: "#3FA7A0", grid: "rgba(148,168,200,0.12)", text: "#8B98AC" };

const TOOL_LABELS = {
  get_deals_summary: "Deals board (summary)",
  get_deals_rows: "Deals board (row lookup)",
  get_work_orders_summary: "Work Orders board (summary)",
  get_work_orders_rows: "Work Orders board (row lookup)",
  get_deal_execution_status: "Cross-board lookup (one deal)",
  get_deals_missing_work_orders: "Cross-board join (won deals without a work order)",
};

// ---------- fetch with a hard timeout + progressive status updates ----------
// Free-tier LLM/API calls can genuinely take a while. Rather than hang with
// no feedback (which reads as "broken"), this (a) updates a status callback
// on a schedule so the user sees it's still working, not frozen, and (b)
// aborts with a clear error after HARD_TIMEOUT_MS instead of waiting forever.
const HARD_TIMEOUT_MS = 45000;

async function fetchWithProgress(url, options, onStatus) {
  const controller = new AbortController();
  const timeoutId = setTimeout(() => controller.abort(), HARD_TIMEOUT_MS);

  const statusSchedule = [
    [0, "Checking monday.com…"],
    [6000, "Still working — free-tier AI can queue under load…"],
    [18000, "Almost there — finishing up the analysis…"],
  ];
  const timers = statusSchedule.map(([delay, msg]) =>
    setTimeout(() => onStatus && onStatus(msg), delay)
  );

  try {
    const res = await fetch(url, { ...options, signal: controller.signal });
    return res;
  } catch (err) {
    if (err.name === "AbortError") {
      throw new Error("This is taking longer than expected (45s+). The free-tier service may be under heavy load right now — try again in a moment.");
    }
    throw err;
  } finally {
    clearTimeout(timeoutId);
    timers.forEach(clearTimeout);
  }
}

// ---------- Tabs ----------
const tabBtns = { chat: document.getElementById("tab-btn-chat"), dashboard: document.getElementById("tab-btn-dashboard") };
const panels = { chat: document.getElementById("panel-chat"), dashboard: document.getElementById("panel-dashboard") };
let dashboardLoaded = false;

function showTab(name) {
  for (const key of Object.keys(tabBtns)) {
    tabBtns[key].classList.toggle("active", key === name);
    tabBtns[key].setAttribute("aria-selected", key === name ? "true" : "false");
    panels[key].classList.toggle("active", key === name);
  }
  if (name === "dashboard" && !dashboardLoaded) {
    loadDashboard();
  }
}
tabBtns.chat.addEventListener("click", () => showTab("chat"));
tabBtns.dashboard.addEventListener("click", () => showTab("dashboard"));

// ---------- KPI strip ----------
async function loadKpis() {
  const strip = document.getElementById("kpi-strip");
  try {
    const res = await fetchWithProgress(`${API}/api/kpis`, {}, (msg) => {
      strip.innerHTML = `<div class="telemetry-loading">${escapeHtml(msg)}</div>`;
    });
    if (!res.ok) throw new Error((await res.json()).detail || res.statusText);
    const k = await res.json();
    const cards = [
      ["Active Deals", k.total_deals],
      ["Deals Won", k.won_deals],
      ["Known Pipeline Value", `Rs. ${Number(k.total_known_pipeline_value).toLocaleString("en-IN")}`],
      ["Work Orders", k.total_work_orders],
      ["Data Completeness", k.data_completeness_pct != null ? `${k.data_completeness_pct}%` : "—"],
    ];
    strip.innerHTML = cards.map(([label, value]) => `
      <div class="telemetry-card">
        <div class="telemetry-label">${label}</div>
        <div class="telemetry-value">${value}</div>
      </div>`).join("");
  } catch (err) {
    strip.innerHTML = `<div class="telemetry-loading">Couldn't reach monday.com: ${escapeHtml(err.message)} <button class="mc-btn" id="retry-kpis" style="display:inline-block;width:auto;margin-left:8px;">Retry</button></div>`;
    document.getElementById("retry-kpis")?.addEventListener("click", loadKpis);
  }
}

// ---------- Dashboard charts ----------
async function loadDashboard() {
  const grid = document.getElementById("dash-grid");
  try {
    const res = await fetchWithProgress(`${API}/api/dashboard`, {}, (msg) => {
      grid.innerHTML = `<div class="telemetry-loading">${escapeHtml(msg)}</div>`;
    });
    if (!res.ok) throw new Error((await res.json()).detail || res.statusText);
    const c = await res.json();
    dashboardLoaded = true;

    grid.innerHTML = `
      <div class="dash-card"><h3>Deals by stage</h3><canvas id="chart-stage"></canvas></div>
      <div class="dash-card"><h3>Known deal value by sector (Rs.)</h3><canvas id="chart-sector"></canvas></div>
      <div class="dash-card"><h3>Work orders by execution status</h3><canvas id="chart-exec"></canvas></div>
      <div class="dash-card"><h3>Work orders by billing status</h3><canvas id="chart-billing"></canvas></div>
    `;
    drawBarChart("chart-stage", c.deals_by_stage, CHART_COLORS.amber);
    drawBarChart("chart-sector", c.deal_value_by_sector, CHART_COLORS.teal);
    drawBarChart("chart-exec", c.work_orders_by_status, CHART_COLORS.amber);
    drawBarChart("chart-billing", c.work_orders_by_billing, CHART_COLORS.teal);
  } catch (err) {
    grid.innerHTML = `<div class="telemetry-loading">Couldn't reach monday.com: ${escapeHtml(err.message)} <button class="mc-btn" id="retry-dash" style="display:inline-block;width:auto;margin-left:8px;">Retry</button></div>`;
    document.getElementById("retry-dash")?.addEventListener("click", loadDashboard);
  }
}

function drawBarChart(canvasId, rows, color) {
  const ctx = document.getElementById(canvasId).getContext("2d");
  new Chart(ctx, {
    type: "bar",
    data: {
      labels: rows.map(r => r.label),
      datasets: [{ data: rows.map(r => r.value), backgroundColor: color, borderRadius: 3 }],
    },
    options: {
      indexAxis: "y",
      plugins: { legend: { display: false } },
      scales: {
        x: { grid: { color: CHART_COLORS.grid }, ticks: { color: CHART_COLORS.text, font: { family: "IBM Plex Mono", size: 10 } } },
        y: { grid: { display: false }, ticks: { color: CHART_COLORS.text, font: { family: "Inter", size: 11 } } },
      },
    },
  });
}

// ---------- Chat ----------
const chatLog = document.getElementById("chat-log");
const chatForm = document.getElementById("chat-form");
const chatInput = document.getElementById("chat-input");

function escapeHtml(s) {
  const div = document.createElement("div");
  div.textContent = s;
  return div.innerHTML;
}

function renderMarkdown(text) {
  const raw = marked.parse(text, { breaks: true });
  return DOMPurify.sanitize(raw);
}

function appendMessage(role, content, tools) {
  const empty = chatLog.querySelector(".chat-empty");
  if (empty) empty.remove();

  const div = document.createElement("div");
  div.className = `msg ${role}`;
  div.innerHTML = role === "assistant" ? renderMarkdown(content) : escapeHtml(content);

  if (tools && tools.length) {
    const trace = document.createElement("div");
    trace.className = "tool-trace";
    trace.textContent = "queried live: " + tools.map(t => TOOL_LABELS[t] || t).join(", ");
    div.appendChild(trace);
  }
  chatLog.appendChild(div);
  chatLog.scrollTop = chatLog.scrollHeight;
  return div;
}

async function sendMessage(text) {
  if (!text.trim()) return;
  chatInput.value = "";
  appendMessage("user", text);
  conversation.push({ role: "user", content: text });
  await runChatRequest();
}

async function runChatRequest() {
  const pending = appendMessage("assistant", "Checking monday.com…");
  pending.classList.add("pending");

  const sendBtn = chatForm.querySelector(".chat-send");
  sendBtn.disabled = true;

  try {
    const res = await fetchWithProgress(
      `${API}/api/chat`,
      {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ conversation }),
      },
      (msg) => { pending.textContent = msg; }
    );
    if (!res.ok) throw new Error((await res.json()).detail || res.statusText);
    const data = await res.json();

    pending.remove();
    appendMessage("assistant", data.reply, data.tools_called);
    conversation = data.conversation;
  } catch (err) {
    pending.remove();
    const errDiv = appendMessage("assistant", `Couldn't complete that request: ${err.message}`);
    const retry = document.createElement("button");
    retry.className = "mc-btn";
    retry.style.marginTop = "8px";
    retry.textContent = "Retry";
    retry.addEventListener("click", () => {
      errDiv.remove();
      runChatRequest(); // conversation already has the user's message - just retry the same call
    });
    errDiv.appendChild(retry);
  } finally {
    sendBtn.disabled = false;
  }
}

chatForm.addEventListener("submit", (e) => {
  e.preventDefault();
  sendMessage(chatInput.value);
});

document.querySelectorAll(".mc-chip").forEach(btn => {
  btn.addEventListener("click", () => sendMessage(btn.textContent));
});

document.getElementById("btn-leadership").addEventListener("click", () => {
  sendMessage(
    "Prepare a leadership update covering: overall pipeline health (by stage and sector), " +
    "operational/billing status from work orders, and a short list of data-quality caveats " +
    "I should be aware of. Format it so I can paste it into an email or doc."
  );
});

document.getElementById("btn-clear").addEventListener("click", () => {
  conversation = [];
  chatLog.innerHTML = '<div class="chat-empty"><p>No transmissions yet. Ask a business question, or pick one from the panel.</p></div>';
});

// ---------- Init ----------
loadKpis();

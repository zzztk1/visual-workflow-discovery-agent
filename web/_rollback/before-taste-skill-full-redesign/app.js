const navButtons = [...document.querySelectorAll(".nav-btn")];
const pages = new Map([...document.querySelectorAll(".page")].map((page) => [page.id.replace("page-", ""), page]));
const pageTitle = document.querySelector("#pageTitle");
const discoverBtn = document.querySelector("#discoverBtn");
const statusText = document.querySelector("#statusText");
const runId = document.querySelector("#runId");
const sourceMode = document.querySelector("#sourceMode");
const candidateCount = document.querySelector("#candidateCount");
const auditStatus = document.querySelector("#auditStatus");
const traceList = document.querySelector("#traceList");
const queryList = document.querySelector("#queryList");
const candidateGrid = document.querySelector("#candidateGrid");
const evidenceList = document.querySelector("#evidenceList");
const metricsBox = document.querySelector("#metricsBox");
const outputLinks = document.querySelector("#outputLinks");
const selectedBox = document.querySelector("#selectedBox");
const riskBox = document.querySelector("#riskBox");
const reportPreview = document.querySelector("#reportPreview");

let latestDiscovery = null;
let latestDeepDive = null;

const titles = {
  config: "任务配置",
  discovery: "搜索发现",
  candidates: "候选工作流",
  evidence: "公开证据",
  deepdive: "深度拆解",
  metrics: "指标与 Trace",
  report: "报告导出",
};

const knownQueries = [
  "restaurant social media workflow photos menu poster",
  "restaurant food photography menu poster marketing workflow",
  "餐饮 商家 菜品图片 海报 外卖 店铺 装修 流程",
  "restaurant menu photo update workflow merchant guide",
  "restaurant customer complaint refund workflow",
  "restaurant inventory procurement workflow",
  "AI restaurant menu photo poster generator workflow",
];

function esc(text) {
  return String(text ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;");
}

function switchPage(name) {
  navButtons.forEach((btn) => btn.classList.toggle("active", btn.dataset.page === name));
  pages.forEach((page, key) => page.classList.toggle("active", key === name));
  pageTitle.textContent = titles[name] || name;
}

navButtons.forEach((btn) => btn.addEventListener("click", () => switchPage(btn.dataset.page)));

function configQuery(extra = {}) {
  return new URLSearchParams({
    industry: document.querySelector("#industry").value,
    goal: document.querySelector("#goal").value,
    live_search: document.querySelector("#liveSearch").checked ? "true" : "false",
    request_id: `web-${Date.now()}`,
    ...extra,
  });
}

function setRunMeta(payload) {
  runId.textContent = payload.run_id || "未运行";
  sourceMode.textContent = payload.source_mode || "unknown";
  candidateCount.textContent = payload.candidates ? String(payload.candidates.length) : "-";
  if (!payload.selected?.name) {
    auditStatus.textContent = "待锁定";
  } else if (payload.causal_audit?.passed) {
    auditStatus.textContent = "通过";
  } else {
    auditStatus.textContent = "需复核";
  }
}

function renderTrace(trace) {
  traceList.innerHTML = (trace || [])
    .map(
      (item, index) => `
        <div class="trace-item">
          <strong>${index + 1}. ${esc(item.node)}</strong>
          <span>${esc(item.status)} · ${esc(item.ts || "")}</span>
        </div>
      `
    )
    .join("");
}

function renderQueries(trace) {
  const queryNode = (trace || []).find((item) => item.node === "query_planner");
  const count = queryNode?.output_summary?.queries || "";
  queryList.innerHTML = `
    <div class="query-item">Query Planner 输出：${esc(count)}</div>
    ${knownQueries.map((query) => `<div class="query-item">${esc(query)}</div>`).join("")}
  `;
}

function renderCandidates(candidates, selectedName = "") {
  candidateGrid.innerHTML = (candidates || [])
    .map((item, index) => {
      const selected = item.name === selectedName ? "selected" : "";
      const steps = (item.steps || []).slice(0, 6).map((step) => `<li>${esc(step)}</li>`).join("");
      const evidenceLabel = item.evidence_insufficient ? "证据不足，已降分" : "证据满足";
      const chips = (item.inefficiencies || [])
        .slice(0, 4)
        .map((value) => `<span class="chip">${esc(value)}</span>`)
        .join("");
      return `
        <article class="candidate-card ${selected}">
          <div class="score">${esc(item.score)}<small>/100</small></div>
          <h3>${index + 1}. ${esc(item.name)}</h3>
          <p class="muted">${esc(item.workflow_type)} · 证据 ${esc(item.evidence_count)} 条 · ${evidenceLabel}</p>
          <div class="chip-row">${chips}</div>
          <ol>${steps}</ol>
          <button data-lock-index="${index}">${selected ? "已锁定" : "锁定并深挖"}</button>
        </article>
      `;
    })
    .join("");
  [...candidateGrid.querySelectorAll("button[data-lock-index]")].forEach((btn) => {
    btn.addEventListener("click", () => deepDive(Number(btn.dataset.lockIndex)));
  });
}

function renderEvidence(evidence, selected = null) {
  const selectedIds = new Set(selected?.evidence_ids || []);
  const rows = selectedIds.size ? evidence.filter((item) => selectedIds.has(item.id)) : evidence;
  evidenceList.innerHTML = (rows || [])
    .map(
      (item) => `
        <div class="evidence-item">
          <a href="${esc(item.url)}" target="_blank" rel="noreferrer">${esc(item.title)}</a>
          <p>${esc(item.proof_point)}</p>
          <p class="muted">${esc(item.workflow)} · ${esc(item.source_type)} · ${esc((item.inefficiencies || []).join(" / "))}</p>
        </div>
      `
    )
    .join("");
}

function renderMetrics(metrics, audit) {
  const totals = metrics?.totals || {};
  const items = [
    ["总耗时 ms", totals.latency_ms],
    ["估算 token", totals.total_tokens],
    ["估算成本 USD", totals.estimated_cost_usd],
    ["工具调用", totals.tool_calls],
    ["失败重试", totals.retry_count],
    ["错误次数", totals.error_count],
    ["因果审计", audit?.passed === undefined ? "待生成" : audit.passed ? "通过" : "未通过"],
    ["串题泄漏", audit?.no_static_visual_template_leak === false ? "发现" : "未发现"],
  ];
  metricsBox.innerHTML = items
    .map(([label, value]) => `<div class="metric"><span>${esc(label)}</span><strong>${esc(value ?? "-")}</strong></div>`)
    .join("");
}

function renderOutputs(payload) {
  const links = {
    ...(payload.links || {}),
    product_solution: `/outputs/${payload.run_id}/product_solution.json`,
    causal_audit: `/outputs/${payload.run_id}/causal_audit.json`,
    painpoints: `/outputs/${payload.run_id}/painpoints.json`,
    agent_interventions: `/outputs/${payload.run_id}/agent_interventions.json`,
  };
  outputLinks.innerHTML = Object.entries(links)
    .map(([name, href]) => `<a href="${esc(href)}" target="_blank">${esc(name)}</a>`)
    .join("");
}

function list(items, field = null) {
  return `<ol>${(items || []).map((item) => `<li>${esc(field ? item[field] : item)}</li>`).join("")}</ol>`;
}

function renderSelected(payload) {
  const selected = payload.selected || {};
  if (!selected.name) {
    selectedBox.innerHTML = `<p class="muted">还没有锁定候选。请先到“候选工作流”页选择一个工作流。</p>`;
    riskBox.textContent = "选择候选后生成";
    return;
  }
  const solution = payload.product_solution || {};
  const audit = payload.causal_audit || {};
  selectedBox.innerHTML = `
    <h3>${esc(selected.name)}</h3>
    <p>workflow_type: <strong>${esc(selected.workflow_type)}</strong></p>
    <p>human_lock: ${esc((selected.human_lock || {}).mode)}</p>
    <p>最终评分：<strong>${esc(selected.score)}</strong> · source_mode: ${esc(payload.source_mode)}</p>

    <h3>原始人工流程</h3>
    ${list((payload.original_mmd || "").split("\n").filter((line) => line.includes('s') && line.includes('["')).map((line) => line.replace(/^.*\["/, "").replace(/"\].*$/, "")))}

    <h3>痛点与低效点</h3>
    ${list((payload.painpoints || []).map((p) => `${p.type}: ${p.where}`))}

    <h3>Agent 改造流程</h3>
    ${list(payload.agent_interventions || [], "agent_step")}

    <h3>产品化方案</h3>
    <p><strong>${esc(solution.product_name || "")}</strong></p>
    <p>${esc(solution.positioning || "")}</p>
    ${list(solution.core_features || [])}

    <h3>因果审计</h3>
    <pre class="compact-pre">${esc(JSON.stringify(audit, null, 2))}</pre>
  `;
  riskBox.textContent = payload.risk_review || "无风险审核";
}

function renderDiscovery(payload) {
  latestDiscovery = payload;
  setRunMeta(payload);
  renderTrace(payload.trace);
  renderQueries(payload.trace);
  renderCandidates(payload.candidates);
  renderEvidence(payload.evidence);
  renderMetrics(payload.metrics, payload.causal_audit);
  renderOutputs(payload);
  selectedBox.innerHTML = `<p class="muted">Discovery 只生成候选，不生成最终方案。请选择候选后再深挖。</p>`;
  riskBox.textContent = "选择候选后生成";
  reportPreview.textContent = payload.final_report || "Discovery 已完成，请选择候选进行深度拆解。";
}

function renderDeepDive(payload) {
  latestDeepDive = payload;
  setRunMeta(payload);
  renderTrace(payload.trace);
  renderQueries(payload.trace);
  renderCandidates(payload.candidates, payload.selected?.name);
  renderEvidence(payload.evidence, payload.selected);
  renderMetrics(payload.metrics, payload.causal_audit);
  renderOutputs(payload);
  renderSelected(payload);
  reportPreview.textContent = payload.final_report;
}

async function discover() {
  discoverBtn.disabled = true;
  statusText.textContent = "搜索发现中";
  try {
    const res = await fetch(`/api/discovery?${configQuery().toString()}`);
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const payload = await res.json();
    renderDiscovery(payload);
    statusText.textContent = "发现完成，等待选择候选";
    switchPage("candidates");
  } catch (error) {
    statusText.textContent = `发现失败：${error.message}`;
  } finally {
    discoverBtn.disabled = false;
  }
}

async function deepDive(index) {
  statusText.textContent = `候选 ${index + 1} 深度拆解中`;
  try {
    const res = await fetch(`/api/deep-dive?${configQuery({ lock_index: String(index) }).toString()}`);
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const payload = await res.json();
    renderDeepDive(payload);
    statusText.textContent = "深度拆解完成，可录屏讲解";
    switchPage("deepdive");
  } catch (error) {
    statusText.textContent = `深挖失败：${error.message}`;
  }
}

discoverBtn.addEventListener("click", discover);

fetch("/api/health")
  .then((res) => res.json())
  .then((health) => {
    statusText.textContent = `服务就绪：${health.status}`;
  })
  .catch(() => {
    statusText.textContent = "服务未就绪";
  });

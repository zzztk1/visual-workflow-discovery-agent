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
  "餐饮门店社媒物料制作流程",
  "餐厅菜品图片、菜单图、海报制作流程",
  "外卖商家店铺装修与商品图片维护流程",
  "餐厅菜单信息更新与平台同步流程",
  "餐厅客诉、退款与补偿处理流程",
  "餐厅库存盘点、采购补货与到货验收流程",
  "餐饮 AI 图片工具的真实使用边界与风险",
];

const nodeLabels = {
  brief_intake: "任务解析",
  query_planner: "查询规划",
  search_executor: "公开搜索",
  evidence_extractor: "证据抽取",
  candidate_builder: "候选生成",
  candidate_scorer: "候选评分",
  human_lock: "人工锁定",
  workflow_decomposer: "流程拆解",
  painpoint_analyzer: "痛点分析",
  intervention_designer: "Agent 改造",
  product_solution_generator: "产品方案",
  risk_reviewer: "风险审核",
  causal_auditor: "因果审计",
  export_report: "报告导出",
};

function esc(text) {
  return String(text ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;");
}

function statusLabel(status) {
  return status === "success" ? "成功" : status === "failed" ? "失败" : status || "未知";
}

function sourceModeLabel(mode) {
  const labels = {
    fallback_evidence_pool: "公开证据池",
    live_search_plus_fallback_evidence_pool: "实时搜索 + 证据池",
    live_search_attempted_fallback_evidence_pool: "实时搜索失败，使用证据池",
  };
  return labels[mode] || mode || "未知";
}

function list(items, className = "") {
  return `<ol class="${esc(className)}">${(items || []).map((item) => `<li>${esc(item)}</li>`).join("")}</ol>`;
}

function tags(items) {
  return `<div class="chip-row">${(items || []).slice(0, 5).map((value) => `<span class="chip">${esc(value)}</span>`).join("")}</div>`;
}

function extractMmdSteps(mmd) {
  return (mmd || "")
    .split("\n")
    .filter((line) => line.includes('["') && /^  s\d+\[/.test(line))
    .map((line) => line.replace(/^.*\["/, "").replace(/"\].*$/, ""));
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
    llm_enabled: document.querySelector("#llmEnabled").checked ? "true" : "false",
    runner: document.querySelector("#runner").value,
    request_id: `web-${Date.now()}`,
    ...extra,
  });
}

function setRunMeta(payload) {
  runId.textContent = payload.run_id || "未运行";
  const engine = payload.execution_engine === "langgraph_stategraph" ? "LangGraph" : "状态机";
  const llm = payload.llm_mode === "stepfun_real_api" ? "大模型" : "规则兜底";
  sourceMode.textContent = `${sourceModeLabel(payload.source_mode)} · ${engine} · ${llm}`;
  candidateCount.textContent = payload.candidates ? String(payload.candidates.length) : "-";
  auditStatus.classList.remove("status-good", "status-warn", "status-idle");
  if (!payload.selected?.name) {
    auditStatus.textContent = "待锁定";
    auditStatus.classList.add("status-idle");
  } else if (payload.causal_audit?.passed) {
    auditStatus.textContent = "通过";
    auditStatus.classList.add("status-good");
  } else {
    auditStatus.textContent = "需复核";
    auditStatus.classList.add("status-warn");
  }
}

function renderTrace(trace) {
  traceList.innerHTML = (trace || [])
    .map(
      (item, index) => `
        <div class="trace-item">
          <strong>${index + 1}. ${esc(nodeLabels[item.node] || item.node)}</strong>
          <span>${esc(statusLabel(item.status))} · 节点 ${esc(item.node)} · ${esc(item.ts || "")}</span>
        </div>
      `
    )
    .join("");
}

function renderQueries(trace) {
  const queryNode = (trace || []).find((item) => item.node === "query_planner");
  const count = queryNode?.output_summary?.queries || "";
  queryList.innerHTML = `
    <div class="query-item">查询规划已生成：${esc(count)}</div>
    ${knownQueries.map((query) => `<div class="query-item">${esc(query)}</div>`).join("")}
  `;
}

function renderCandidates(candidates, selectedName = "") {
  candidateGrid.innerHTML = (candidates || [])
    .map((item, index) => {
      const selected = item.name === selectedName ? "selected" : "";
      const steps = (item.steps || []).slice(0, 6).map((step) => `<li>${esc(step)}</li>`).join("");
      const evidenceLabel = item.evidence_insufficient ? "证据不足，已降分" : "证据满足";
      return `
        <article class="candidate-card ${selected}">
          <div class="candidate-head">
            <div class="score">${esc(item.score)}<small>/100</small></div>
            <span class="type-pill">${esc(item.workflow_type)}</span>
          </div>
          <h3>${index + 1}. ${esc(item.name)}</h3>
          <p class="muted">证据 ${esc(item.evidence_count)} 条 · ${evidenceLabel}</p>
          ${tags(item.inefficiencies)}
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
    ["token 消耗", totals.total_tokens],
    ["模型成本 USD", totals.estimated_cost_usd],
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

function renderRiskCards(risks) {
  if (!risks || !risks.length) {
    riskBox.innerHTML = `<div class="empty-state">选择候选后生成风险审核。</div>`;
    return;
  }
  riskBox.innerHTML = risks
    .map(
      (risk) => `
        <article class="risk-card ${esc(risk.level || "medium")}">
          <div>
            <span class="risk-level">${esc(risk.level || "medium")}</span>
            <h4>${esc(risk.risk)}</h4>
          </div>
          <p>${esc(risk.mitigation)}</p>
          <small>人工确认：${risk.requires_human_confirmation ? "需要" : "不需要"}</small>
        </article>
      `
    )
    .join("");
}

function renderSelected(payload) {
  const selected = payload.selected || {};
  if (!selected.name) {
    selectedBox.innerHTML = `<p class="empty-state">还没有锁定候选。请先到“候选工作流”页选择一个工作流。</p>`;
    renderRiskCards([]);
    return;
  }

  const solution = payload.product_solution || {};
  const audit = payload.causal_audit || {};
  const originalSteps = extractMmdSteps(payload.original_mmd);
  const interventions = payload.agent_interventions || [];
  const painpoints = payload.painpoints || [];

  selectedBox.innerHTML = `
    <div class="result-hero">
      <div>
        <p class="eyebrow">已锁定工作流</p>
        <h3>${esc(selected.name)}</h3>
        <p class="muted">评分 ${esc(selected.score)} · ${esc(sourceModeLabel(payload.source_mode))} · ${esc(selected.workflow_type)}</p>
      </div>
      <span class="audit-pill ${audit.passed ? "pass" : "wait"}">${audit.passed ? "因果审计通过" : "等待审计"}</span>
    </div>

    <div class="result-section">
      <h4>原始人工流程</h4>
      ${list(originalSteps, "formal-list")}
    </div>

    <div class="result-section">
      <h4>痛点与低效点</h4>
      <div class="insight-grid">
        ${painpoints
          .map(
            (p) => `
              <article class="insight-card">
                <strong>${esc(p.type)}</strong>
                <p>${esc(p.where)}</p>
                <small>${esc(p.impact || "")}</small>
              </article>
            `
          )
          .join("")}
      </div>
    </div>

    <div class="result-section">
      <h4>Agent 改造流程</h4>
      ${list(interventions.map((item) => item.agent_step), "formal-list")}
    </div>

    <div class="result-section product-summary">
      <h4>产品化方案</h4>
      <h3>${esc(solution.product_name || "")}</h3>
      <p>${esc(solution.positioning || "")}</p>
      ${tags(solution.core_features || [])}
    </div>
  `;

  renderRiskCards(payload.risk_review_items || []);
}

function renderFormalReport(payload) {
  const selected = payload.selected || {};
  const solution = payload.product_solution || {};
  const totals = payload.metrics?.totals || {};
  if (!selected.name) {
    reportPreview.innerHTML = `<div class="empty-state">运行搜索发现后，再选择候选进行深度拆解。</div>`;
    return;
  }

  const evidenceRows = (payload.evidence || [])
    .filter((item) => (selected.evidence_ids || []).includes(item.id))
    .slice(0, 5);

  reportPreview.innerHTML = `
    <article class="report-document">
      <header>
        <p class="eyebrow">方案报告</p>
        <h2>${esc(selected.name)}</h2>
        <p>${esc(solution.positioning || "围绕锁定工作流生成 Agent 介入后的产品化方案。")}</p>
      </header>

      <section>
        <h3>公开证据</h3>
        <div class="report-evidence">
          ${evidenceRows
            .map((item) => `<a href="${esc(item.url)}" target="_blank" rel="noreferrer">${esc(item.title)}</a>`)
            .join("")}
        </div>
      </section>

      <section>
        <h3>原始流程</h3>
        ${list(extractMmdSteps(payload.original_mmd), "formal-list")}
      </section>

      <section>
        <h3>Agent 改造流程</h3>
        ${list((payload.agent_interventions || []).map((item) => item.agent_step), "formal-list")}
      </section>

      <section>
        <h3>产品方案</h3>
        <div class="report-columns">
          <div>
            <h4>核心功能</h4>
            ${list(solution.core_features || [], "formal-list")}
          </div>
          <div>
            <h4>人工复核点</h4>
            ${list(solution.human_in_the_loop || [], "formal-list")}
          </div>
        </div>
      </section>

      <section>
        <h3>7 天 MVP 验证</h3>
        ${list(solution.mvp_7_day_plan || [], "formal-list")}
      </section>

      <section>
        <h3>成本与可观测</h3>
        <div class="report-metrics">
          <span>总耗时：${esc(totals.latency_ms ?? "-")} ms</span>
          <span>token 消耗：${esc(totals.total_tokens ?? "-")}</span>
          <span>工具调用：${esc(totals.tool_calls ?? "-")}</span>
          <span>因果审计：${payload.causal_audit?.passed ? "通过" : "待复核"}</span>
        </div>
      </section>
    </article>
  `;
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
  selectedBox.innerHTML = `<p class="empty-state">搜索发现只生成候选，不生成最终方案。请选择候选后再深挖。</p>`;
  renderRiskCards([]);
  renderFormalReport(payload);
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
  renderFormalReport(payload);
}

async function discover() {
  discoverBtn.disabled = true;
  statusText.textContent = "搜索发现中";
  try {
    const res = await fetch(`${apiBase}/api/discovery?${configQuery().toString()}`);
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
    const res = await fetch(`${apiBase}/api/deep-dive?${configQuery({ lock_index: String(index) }).toString()}`);
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const payload = await res.json();
    renderDeepDive(payload);
    statusText.textContent = "深度拆解完成，可查看方案与报告";
    switchPage("deepdive");
  } catch (error) {
    statusText.textContent = `深挖失败：${error.message}`;
  }
}

discoverBtn.addEventListener("click", discover);

const apiBase = window.location.protocol === "file:" ? "http://127.0.0.1:7860" : "";

fetch(`${apiBase}/api/health`)
  .then((res) => res.json())
  .then((health) => {
    statusText.textContent = `服务就绪：${health.status}`;
  })
  .catch(() => {
    statusText.textContent = "服务未就绪";
  });

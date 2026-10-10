const $ = (selector, root = document) => root.querySelector(selector);
const $$ = (selector, root = document) => [...root.querySelectorAll(selector)];
const savedPageSize = key => {
  try { const value = Number(localStorage.getItem(`liveops-page-size-${key}`)); return [10, 20, 50, 100].includes(value) ? value : 20; }
  catch { return 20; }
};
const state = { view: "overview", date: new Date().toLocaleDateString("en-CA"), schedules: [], reports: [], runs: [], columns: [], settings: {}, secondPrecisionSupported: false, pagination: { reports: { page: 1, pageSize: savedPageSize("reports"), total: 0 }, runs: { page: 1, pageSize: savedPageSize("runs"), total: 0 } }, toastTimer: null, refreshing: false };
const renderedHtml = new WeakMap();
let openDatePicker = null;
let serviceChipText = "本地服务已运行";
let rosterChipText = "";
let chipIndex = 0;
function renderConnectionChip() {
  const label = document.getElementById("connection-chip-text");
  if (label) label.textContent = state.view === "schedules" && chipIndex && rosterChipText ? rosterChipText : serviceChipText;
}
function resizeRosterFrame() {
  if (state.view !== "schedules") return;
  const frame = document.getElementById("roster-frame");
  if (frame) frame.style.height = `${Math.max(260, window.innerHeight - frame.getBoundingClientRect().top - 8)}px`;
}
window.addEventListener("message", event => {
  const frame = document.getElementById("roster-frame");
  if (event.origin !== location.origin || event.source !== frame?.contentWindow || event.data?.type !== "liveops:roster-status") return;
  rosterChipText = String(event.data.text || "");
  renderConnectionChip();
});
window.addEventListener("resize", () => requestAnimationFrame(resizeRosterFrame));
setInterval(() => { if (state.view === "schedules" && rosterChipText) { chipIndex = 1 - chipIndex; renderConnectionChip(); } }, 3200);

async function api(path, options = {}) {
  const response = await fetch(path, { headers: { "Content-Type": "application/json", ...(options.headers || {}) }, ...options });
  const payload = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(payload.detail || `请求失败 (${response.status})`);
  return payload;
}
function esc(value) { return String(value ?? "").replace(/[&<>"']/g, ch => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[ch]); }
function numericValue(value) { if (value == null || value === "") return null; const number = Number(value); return Number.isFinite(number) ? number : null; }
function money(value, digits = 2) { const number = numericValue(value); return number == null ? "—" : number.toLocaleString("zh-CN", { minimumFractionDigits: digits, maximumFractionDigits: digits }); }
function integer(value) { const number = numericValue(value); return number == null ? "—" : number.toLocaleString("zh-CN", { maximumFractionDigits: 0 }); }
function percent(value) { const number = numericValue(value); return number == null ? "—" : `${(number * 100).toFixed(2)}%`; }
function missingLabels(items) {
  const names = new Map(state.columns.map(col => [col.key, col.label]));
  return (items || []).map(item => {
    if (names.has(item)) return `${names.get(item)}（${item}）`;
    if (item.includes("Locator.screenshot") && item.includes("Timeout")) return "转化漏斗截图超时（截图缺失，详情见逐步运行日志）";
    return item;
  }).join("\n") || "无";
}
function stamp(value) { if (!value) return "—"; const iso = /(?:Z|[+-]\d{2}:\d{2})$/i.test(value) ? value : `${value}Z`; return new Date(iso).toLocaleString("zh-CN", { timeZone: "Asia/Shanghai", year: "numeric", month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", second: "2-digit", hour12: false }); }
function dateLabel(value) { return value ? value.replaceAll("-", ".") : ""; }
function renderHTML(node, html) { if (renderedHtml.get(node) === html) return; renderedHtml.set(node, html); node.innerHTML = html; }
function toast(message, error = false) {
  const node = $("#toast"); node.textContent = message; node.style.background = error ? "#b83f4d" : "#18243a"; node.classList.add("show");
  clearTimeout(state.toastTimer); state.toastTimer = setTimeout(() => node.classList.remove("show"), 3300);
}
function showError(error) { console.error(error); toast(error.message || String(error), true); }
function setView(view) {
  state.view = view;
  if (view === "schedules") window.scrollTo(0, 0);
  document.body.classList.toggle("roster-active", view === "schedules");
  $$(".view").forEach(node => node.classList.toggle("active", node.id === `view-${view}`));
  $$(".nav-item").forEach(node => node.classList.toggle("active", node.dataset.view === view));
  const titles = { overview: "数据总览", schedules: "排班管理", reports: "数据明细", runs: "采集记录", collectors: "采集脚本", settings: "系统配置" };
  $("#page-title").textContent = titles[view] || "数据总览";
  chipIndex = 0; renderConnectionChip();
  if (view === "collectors") loadCollectors().catch(showError);
  if (view === "settings") loadSettings().catch(showError);
  if (view === "reports") loadReportView().catch(showError);
  if (view === "runs") loadRunsView().catch(showError);
  if (view === "schedules") {
    requestAnimationFrame(resizeRosterFrame);
    document.getElementById("roster-frame")?.contentWindow?.postMessage({ type: "liveops:roster-open" }, location.origin);
  }
}
function syncStatusDetail(status, integration) {
  const value = String(status || "");
  const key = value.split(":", 1)[0];
  if (key === "not_configured") {
    const detail = value.includes(":") ? value.slice(value.indexOf(":") + 1).trim() : "";
    if (detail) return detail;
    return integration === "bitable"
      ? "请在系统配置填写多维表格 app_token 和 table_id，并配置飞书 App ID、App Secret。"
      : "请在系统配置填写飞书 App ID、App Secret 和群聊 Chat ID。";
  }
  if (key === "failed" || key === "partial") return value.includes(":") ? value.slice(value.indexOf(":") + 1).trim() : value;
  if (key === "archived" || key === "local_only") return value.includes(":") ? value.slice(value.indexOf(":") + 1).trim() : "本地数据库保留完整历史";
  return "";
}
function statusLabel(status, integration = "") {
  const text = { succeeded: "完成", partial: "部分完成", failed: "失败", queued: "队列中", running: "执行中", pending: "待同步", sent: "已发送", synced: "已同步", archived: "仅本地保留", local_only: "仅本地保留", not_configured: "未配置" };
  const key = String(status || "").split(":", 1)[0];
  const detail = syncStatusDetail(status, integration) || String(status || "");
  return `<span class="run-state state-${esc(key)}"${detail ? ` title="${esc(detail)}"` : ""}>${esc(text[key] || status || "—")}</span>`;
}
function empty(text) { return `<div class="empty-state">${esc(text)}</div>`; }
function paginationControl(key, total) {
  const item = state.pagination[key], pages = Math.max(1, Math.ceil(total / item.pageSize));
  item.total = total;
  if (item.page > pages) item.page = pages;
  return `<div class="pagination-control"><span class="pagination-total">共 ${integer(total)} 条</span><button class="pagination-button" data-pagination-page="${key}" data-page="${item.page - 1}" ${item.page <= 1 ? "disabled" : ""}>上一页</button><span class="pagination-current">${item.page} / ${pages}</span><button class="pagination-button" data-pagination-page="${key}" data-page="${item.page + 1}" ${item.page >= pages ? "disabled" : ""}>下一页</button><label class="pagination-jump">跳至<input type="number" min="1" max="${pages}" value="${item.page}" data-pagination-jump-input="${key}">页</label><button class="pagination-button" data-pagination-jump="${key}">跳转</button><label class="pagination-size"><select data-pagination-size="${key}">${[10, 20, 50, 100].map(size => `<option value="${size}" ${item.pageSize === size ? "selected" : ""}>${size} 条/页</option>`).join("")}</select></label></div>`;
}
function renderKpis(data) {
  const totals = data.totals || {};
  const cards = [
    ["时段 GMV", money(totals.gmv), `已采集 ${data.report_count || 0} / ${data.schedule_count || 0} 个时段`],
    ["整体成交额", money(totals.overall), "GMV + 千川券"],
    ["整体 ROI", totals.roi == null ? "—" : Number(totals.roi).toFixed(2), `投放消耗 ${money(totals.spend)} 元`],
    ["累计场观", integer(totals.viewers), "按排班时段汇总"],
  ];
  renderHTML($("#kpi-grid"), cards.map(([label, value, sub]) => `<article class="kpi-card"><div class="kpi-top"><span>${label}</span></div><div class="kpi-value">${value}</div><div class="kpi-sub">${sub}</div></article>`).join(""));
}
function renderChart(series = []) {
  const node = $("#sales-chart"); $("#chart-date").textContent = dateLabel(state.date);
  const values = series.filter(item => item.gmv != null || item.overall != null);
  if (!values.length) { node.onpointerover = node.onpointerout = node.onpointerleave = node.onfocusin = node.onfocusout = node.onclick = node.onkeydown = null; renderHTML(node, empty("当前日期还没有已采集数据")); return; }
  const width = Math.max(360, Math.round(node.clientWidth || 680)), height = 260, left = 48, right = 12, top = 16, bottom = 46;
  const chartW = width - left - right, chartH = height - top - bottom;
  const amountOf = (item, key) => numericValue(item[key]) ?? 0;
  const maximum = Math.max(1, ...values.flatMap(item => [amountOf(item, "gmv"), amountOf(item, "overall")]));
  const band = chartW / values.length, barW = Math.min(22, Math.max(8, band * .24));
  let svg = `<svg viewBox="0 0 ${width} ${height}" role="group" aria-label="各时段 GMV 与整体成交额柱状图">`;
  for (let i = 0; i < 4; i++) { const y = top + chartH * i / 3; const amount = maximum * (1 - i / 3); svg += `<line x1="${left}" y1="${y}" x2="${width - right}" y2="${y}" stroke="#edf0f5"/><text x="${left - 7}" y="${y + 4}" font-size="10" text-anchor="end" fill="#8996ad">${amount >= 10000 ? `${(amount / 10000).toFixed(1)}w` : Math.round(amount)}</text>`; }
  values.forEach((item, index) => {
    const slotX = left + band * index, x = slotX + band / 2;
    const gmv = amountOf(item, "gmv"), overall = amountOf(item, "overall");
    const h1 = gmv / maximum * chartH, h2 = overall / maximum * chartH;
    const label = String(item.label || `时段 ${index + 1}`);
    const ariaLabel = esc(`${label}，GMV ${money(item.gmv)} 元，整体成交额 ${money(item.overall)} 元`);
    svg += `<g class="chart-mark" data-chart-index="${index}" tabindex="0" role="button" aria-pressed="false" aria-label="${ariaLabel}"><rect class="chart-hit-area" x="${slotX}" y="${top}" width="${band}" height="${chartH}"/><rect class="chart-bar chart-bar-gmv" x="${x - barW - 1}" y="${top + chartH - h1}" width="${barW}" height="${h1}" rx="3"/><rect class="chart-bar chart-bar-overall" x="${x + 1}" y="${top + chartH - h2}" width="${barW}" height="${h2}" rx="3"/>`;
    const parts = String(item.label || "").split(" ");
    svg += `<text class="chart-x-label" x="${x}" y="${height - 20}" font-size="10" text-anchor="middle">${esc(parts[0] || label)}</text><text class="chart-x-label" x="${x}" y="${height - 6}" font-size="10" text-anchor="middle">${esc(parts.slice(1).join(" ") || "")}</text></g>`;
  });
  svg += "</svg>";
  renderHTML(node, `<div class="chart-plot">${svg}<div class="chart-tooltip" role="status" aria-live="polite" aria-hidden="true"></div></div><div class="chart-legend"><span><i class="chart-dot-gmv"></i>GMV</span><span><i class="chart-dot-overall"></i>整体（GMV+券）</span></div>`);
  const tooltip = node.querySelector(".chart-tooltip"), plot = node.querySelector(".chart-plot");
  let pinnedMark = node.querySelector(".chart-mark.is-selected"), activeMark = null;
  const hideTooltip = () => { tooltip.classList.remove("is-visible"); tooltip.setAttribute("aria-hidden", "true"); };
  const showTooltip = mark => {
    const item = values[Number(mark.dataset.chartIndex)];
    if (!item) return;
    const label = String(item.label || `时段 ${Number(mark.dataset.chartIndex) + 1}`);
    tooltip.innerHTML = `<strong class="chart-tooltip-title">${esc(label)}</strong><span class="chart-tooltip-row"><i class="chart-dot-gmv"></i><span>GMV</span><b>${money(item.gmv)}</b><small>元</small></span><span class="chart-tooltip-row"><i class="chart-dot-overall"></i><span>整体成交额</span><b>${money(item.overall)}</b><small>元</small></span>`;
    const markRect = mark.getBoundingClientRect(), plotRect = plot.getBoundingClientRect();
    const halfTooltip = tooltip.offsetWidth / 2;
    const center = markRect.left + markRect.width / 2 - plotRect.left;
    tooltip.style.left = `${Math.max(halfTooltip + 8, Math.min(plot.clientWidth - halfTooltip - 8, center))}px`;
    tooltip.classList.add("is-visible"); tooltip.setAttribute("aria-hidden", "false");
  };
  const chartMark = event => event.target.closest?.(".chart-mark") || null;
  node.onpointerover = event => { const mark = chartMark(event); if (mark && mark !== activeMark) { activeMark = mark; showTooltip(mark); } };
  node.onpointerout = event => {
    const mark = chartMark(event), nextMark = event.relatedTarget?.closest?.(".chart-mark") || null;
    if (!mark || nextMark === mark) return;
    if (activeMark === mark) { activeMark = null; if (pinnedMark) showTooltip(pinnedMark); else hideTooltip(); }
  };
  node.onpointerleave = () => { activeMark = null; if (pinnedMark) showTooltip(pinnedMark); else hideTooltip(); };
  node.onfocusin = event => { const mark = chartMark(event); if (mark) { activeMark = mark; showTooltip(mark); } };
  node.onfocusout = event => { if (!node.contains(event.relatedTarget)) { activeMark = null; if (pinnedMark) showTooltip(pinnedMark); else hideTooltip(); } };
  node.onclick = event => {
    const mark = chartMark(event); if (!mark) return;
    node.querySelectorAll(".chart-mark.is-selected").forEach(selected => { selected.classList.remove("is-selected"); selected.setAttribute("aria-pressed", "false"); });
    if (pinnedMark === mark) { pinnedMark = null; hideTooltip(); }
    else { pinnedMark = mark; mark.classList.add("is-selected"); mark.setAttribute("aria-pressed", "true"); showTooltip(mark); }
  };
  node.onkeydown = event => {
    if (event.key === "Escape" && pinnedMark) { pinnedMark.classList.remove("is-selected"); pinnedMark.setAttribute("aria-pressed", "false"); pinnedMark = null; hideTooltip(); return; }
    const mark = chartMark(event);
    if (mark && (event.key === "Enter" || event.key === " ")) { event.preventDefault(); mark.click(); }
  };
}
function renderRuns(runs, target) {
  const rows = runs.slice(0, 6);
  if (!rows.length) { renderHTML(target, empty("暂无运行记录")); return; }
  renderHTML(target, rows.map(run => `<button class="run-item run-button" data-run-id="${esc(run.id)}" aria-label="查看${run.kind === "baseline" ? "千川起始基线" : run.kind === "interruption" ? "中断片段采集" : "主播时段报告"}详情"><span class="run-icon">${run.kind === "baseline" ? "◷" : "▣"}</span><span class="run-copy"><span class="run-name">${esc(run.kind === "baseline" ? "千川起始基线" : run.kind === "interruption" ? "中断片段采集" : "主播时段报告")}</span><span class="run-time">${esc(stamp(run.started_at || run.created_at))} · ${esc(run.trigger === "scheduler" ? "自动采集" : "人工采集")}</span></span><span class="run-status">${statusLabel(run.status)}</span></button>`).join(""));
}
function scheduleTable(items) {
  if (!items.length) return empty("该日期还没有排班。进入排班管理，在日期卡片中添加班次。");
  return `<table class="data-table"><thead><tr><th>日期 / 时段</th><th>主播</th><th>投手</th><th>时长</th><th>采集状态</th><th>基线</th></tr></thead><tbody>${items.map(row => `<tr><td>${esc(dateLabel(row.work_date))}<br><span class="panel-note">${esc(row.start_time)}–${esc(row.end_time)}</span></td><td class="host-cell">${esc(row.host_name)}</td><td>${esc(row.promoter || row.ops || "—")}</td><td>${money(row.duration_hours, 2)} h</td><td>${row.report_id ? `<span class="pill has-report">报告已生成</span>` : "<span class=\"pill\">等待采集</span>"} ${row.enabled ? "<span class=\"pill enabled\">启用</span>" : "<span class=\"pill\">停用</span>"}</td><td>${row.has_baseline ? `<span class="pill enabled">已记录</span>` : "<span class=\"pill\">待开始</span>"}</td></tr>`).join("")}</tbody></table>`;
}
function metricValue(key, value) {
  if (value == null) return "—";
  if (["product_click_conversion", "watch_conversion", "interaction_rate", "new_follower_rate"].includes(key)) return percent(value);
  if (["paid_users", "product_click_users", "viewers", "new_followers", "exposure", "daily_exposure"].includes(key)) return integer(value);
  if (key === "duration_hours") return money(value, 2);
  return money(value, 2);
}
function reportFieldValue(row, column) {
  let value = row.metrics?.[column.key];
  if (column.key === "work_date") value = row.work_date;
  else if (column.key === "time_range") value = `${row.start_time || ""}–${row.end_time || ""}`;
  else if (column.key === "host_name") value = row.host_name;
  else if (column.key === "promoter") value = row.promoter;
  if (value == null || value === "") return "—";
  if (column.kind === "date") return dateLabel(String(value));
  if (column.kind === "text") return String(value);
  if (column.kind === "percent") return percent(value);
  if (column.kind === "integer") return integer(value);
  return money(value, 2);
}
function reportTable(items) {
  if (!items.length) return empty("当前没有报表数据。到排班管理中先添加主播时段。");
  const columns = state.columns.length ? state.columns : [];
  const visible = columns.length ? columns : Object.keys(items[0].metrics || {}).map(key => ({ key, label: key }));
  return `<table class="data-table"><thead><tr>${visible.map(col => `<th>${esc(col.label)}</th>`).join("")}<th>截图</th><th>同步状态</th><th>详情</th></tr></thead><tbody>${items.map(row => `<tr>${visible.map(col => { const raw = col.key === "work_date" ? row.work_date : col.key === "time_range" ? row.start_time && row.end_time ? `${row.start_time}-${row.end_time}` : row.metrics?.[col.key] : col.key === "host_name" ? row.host_name : col.key === "promoter" ? row.promoter : row.metrics?.[col.key]; const cell = reportFieldValue(row, col); return `<td class="${(raw == null || raw === "") && col.key !== "daily_exposure" ? "cell-warning" : ""}">${esc(cell)}</td>`; }).join("")}<td>${(row.screenshots || []).length} 张</td><td>${statusLabel(row.feishu_status, "feishu")} ${statusLabel(row.bitable_status, "bitable")}</td><td><button class="small-button" data-report-id="${row.id}">查看</button></td></tr>`).join("")}</tbody></table>`;
}
function runsTable(items) {
  if (!items.length) return empty("暂无运行记录。");
  return `<table class="data-table"><thead><tr><th>开始时间</th><th>完成时间</th><th>任务</th><th>来源</th><th>状态</th><th>说明</th><th>详情</th></tr></thead><tbody>${items.map(run => `<tr><td>${esc(stamp(run.started_at || run.created_at))}</td><td>${esc(stamp(run.finished_at))}</td><td>${run.kind === "baseline" ? "千川累计基线" : run.kind === "interruption" ? "中断片段采集" : "时段数据采集"}</td><td>${run.trigger === "scheduler" ? "自动调度" : "人工触发"}</td><td>${statusLabel(run.status)}</td><td>${esc(run.message || (run.missing_fields || []).join("、") || "—")}</td><td><button class="small-button" data-run-id="${esc(run.id)}">查看</button></td></tr>`).join("")}</tbody></table>`;
}
async function loadStatus() {
  const result = await api("/api/status");
  serviceChipText = "本地服务已运行"; renderConnectionChip();
  $("#browser-mode").textContent = result.browser_mode === "native" ? "Playwright 原生浏览器" : "Cloak profile 已配置";
  $("#app-status-text").textContent = result.collector_modules_seeded ? `服务正常 · ${result.running_count} 个任务运行中` : "服务正常";
  $("#shop-name").textContent = state.settings.shop_name || "店铺未配置";
}
async function loadDashboard() {
  const [data, scheduleData, runs] = await Promise.all([api(`/api/dashboard?work_date=${state.date}`), api(`/api/schedules?work_date=${state.date}`), api("/api/runs?limit=15")]);
  state.schedules = scheduleData.items || []; state.runs = runs.items || [];
  renderKpis(data); renderChart(data.series || []); renderRuns(state.runs, $("#recent-runs"));
  renderHTML($("#overview-schedules"), scheduleTable(state.schedules));
}
async function loadReportView() {
  const page = state.pagination.reports;
  const [result, columns] = await Promise.all([api(`/api/reports?work_date=${state.date}&page=${page.page}&page_size=${page.pageSize}`), api("/api/report-columns")]);
  const rows = result.items || [];
  const total = Number.isFinite(result.total) ? result.total : rows.length;
  const pages = Math.max(1, Math.ceil(total / page.pageSize));
  if (page.page > pages) { page.page = pages; return loadReportView(); }
  state.reports = Number.isFinite(result.total) ? rows : rows.slice((page.page - 1) * page.pageSize, page.page * page.pageSize);
  state.columns = columns.items || [];
  page.total = total;
  $("#report-count").textContent = `${integer(page.total)} 条记录`;
  renderHTML($("#report-table"), reportTable(state.reports));
  window.dispatchEvent(new CustomEvent("liveops:pagination-updated", { detail: { key: "reports", current: page.page, pageSize: page.pageSize, total: page.total } }));
}
async function loadRunsView() {
  const page = state.pagination.runs;
  const result = await api(`/api/runs?page=${page.page}&page_size=${page.pageSize}&limit=500`);
  const rows = result.items || [];
  const total = Number.isFinite(result.total) ? result.total : rows.length;
  const pages = Math.max(1, Math.ceil(total / page.pageSize));
  if (page.page > pages) { page.page = pages; return loadRunsView(); }
  state.runs = Number.isFinite(result.total) ? rows : rows.slice((page.page - 1) * page.pageSize, page.page * page.pageSize);
  page.total = total;
  $("#run-count").textContent = `${integer(page.total)} 条记录`;
  renderHTML($("#runs-table"), runsTable(state.runs));
  window.dispatchEvent(new CustomEvent("liveops:pagination-updated", { detail: { key: "runs", current: page.page, pageSize: page.pageSize, total: page.total } }));
}
async function refreshVisibleView() {
  if (state.view === "overview") return loadDashboard();
  if (state.view === "schedules") { document.getElementById("roster-frame")?.contentWindow?.postMessage({ type: "liveops:roster-refresh" }, location.origin); return; }
  if (state.view === "reports") return loadReportView();
  if (state.view === "runs") return loadRunsView();
}
async function refreshAll() {
  if (state.refreshing) return;
  state.refreshing = true;
  try { await loadStatus(); await refreshVisibleView(); }
  catch (error) { showError(error); }
  finally { state.refreshing = false; }
}
async function showReport(id) {
  try {
    const { item } = await api(`/api/reports/${id}`); $("#detail-title").textContent = `${item.host_name} · ${item.start_time}–${item.end_time}`;
    const metrics = state.columns.map(col => {
      const label = ["qianchuan_coupon", "overall_amount", "spend", "exposure", "overall_roi", "cpm"].includes(col.key)
        ? `${col.label}（${col.key}）` : col.label;
      return `<div class="metric-chip"><small>${esc(label)}</small><strong>${esc(reportFieldValue(item, col))}</strong></div>`;
    }).join("");
    const images = (item.screenshots || []).map(s => `<a class="screenshot-card" href="${esc(s.url)}" target="_blank" rel="noreferrer"><img src="${esc(s.url)}" alt="${esc(s.name)}"><span>${esc(s.name)} · ${esc(s.mode)}</span></a>`).join("");
    const raw = item.raw || {};
    const segments = raw.segments || [];
    const notes = [...(raw.source?.quality_notes || []), ...(raw.source?.quality_issues || [])];
    const segmentDetails = segments.length ? `<div class="detail-block"><h4>直播场次与班次归属</h4><p class="panel-note">${raw.is_final === false ? "采集中快照，班次结束后需要重新结算。" : raw.measurement_mode === "manual_handoffs" ? "按实际采集快照交接，仅合并手动保存的中断片段。" : "按公司排班时间汇总各场片段。"} 实际已播 ${money(item.metrics?.actual_duration_hours)} h；报表单小时成交额仍按排班时长计算。</p><table class="data-table"><thead><tr><th>场次 ID</th><th>本班次取数区间</th><th>快照采集时间</th><th>用户支付金额</th></tr></thead><tbody>${segments.map(s => `<tr><td>${esc(s.room?.live_room_id)}</td><td>${esc(stamp(s.source?.segment_start))} — ${esc(stamp(s.source?.segment_end))}</td><td>${esc(stamp(s.captured_at))}</td><td>${esc(money(s.source?.gmv))}</td></tr>`).join("")}</tbody></table>${notes.length ? `<pre>${esc([...new Set(notes)].join("\n"))}</pre>` : ""}</div>` : "";
    const syncDetail = (label, key, status) => {
      const detail = syncStatusDetail(status, key);
      return `<div class="sync-status-item"><strong>${label}</strong>${statusLabel(status, key)}${detail ? `<pre>${esc(detail)}</pre>` : ""}</div>`;
    };
     renderHTML($("#detail-content"), `<div class="metric-grid">${metrics}</div>${segmentDetails}<div class="detail-block"><h4>同步状态与原因</h4><div class="sync-status-list">${syncDetail("飞书群消息", "feishu", item.feishu_status)}${syncDetail("多维表格", "bitable", item.bitable_status)}</div></div><div class="detail-block"><h4>截图（${(item.screenshots || []).length}）</h4><div class="screenshot-grid">${images || empty("没有截图")}</div></div><div class="detail-block"><h4>缺失字段 / 页面错误</h4><pre>${esc(missingLabels(item.missing_fields))}</pre></div><details class="detail-block"><summary>原始接口响应（${(raw.responses || []).length} 个）</summary><pre>${esc(JSON.stringify({ source: raw.source, page_errors: raw.page_errors, responses: raw.responses }, null, 2))}</pre></details>`);
    $("#detail-content").scrollTop = 0;
    $("#detail-dialog").showModal();
  } catch (error) { showError(error); }
}
async function showRun(id) {
  try {
    const { item } = await api(`/api/runs/${encodeURIComponent(id)}`); $("#detail-title").textContent = `${item.kind === "baseline" ? "千川累计基线" : "采集运行"} · ${String(item.status).toUpperCase()}`;
    const steps = (item.result?.steps || []).map((step, index) => `<li class="step-item step-${esc(step.status || "info")}"><span class="step-index">${index + 1}</span><div><time>${esc(stamp(step.at))}</time><strong>${esc(step.stage)}</strong><p>${esc(step.description)}</p></div></li>`).join("");
    const result = { ...(item.result || {}) }; delete result.steps;
    renderHTML($("#detail-content"), `<div class="detail-block"><h4>开始与完成</h4><pre>开始：${esc(stamp(item.started_at || item.created_at))}\n完成：${esc(stamp(item.finished_at))}</pre></div><div class="detail-block"><h4>逐步运行日志</h4><ol class="step-list">${steps || "暂无步骤日志"}</ol></div><div class="detail-block"><h4>运行说明</h4><pre>${esc(item.message || "任务正在执行…")}</pre></div><div class="detail-block"><h4>缺失字段</h4><pre>${esc(missingLabels(item.missing_fields))}</pre></div><details class="detail-block"><summary>结果数据</summary><pre>${esc(JSON.stringify(result, null, 2))}</pre></details>`);
    $("#detail-content").scrollTop = 0;
    $("#detail-dialog").showModal();
  } catch (error) { showError(error); }
}
function localDate(value) {
  const match = /^(\d{4})-(\d{2})-(\d{2})$/.exec(value || "");
  return match ? new Date(Number(match[1]), Number(match[2]) - 1, Number(match[3])) : null;
}
function dateValue(value) {
  return `${value.getFullYear()}-${String(value.getMonth() + 1).padStart(2, "0")}-${String(value.getDate()).padStart(2, "0")}`;
}
function setPickerValue(id, value) {
  const input = document.getElementById(id);
  if (!input) return;
  input.value = value || "";
  window.dispatchEvent(new Event("liveops:widgets-updated"));
  const trigger = document.querySelector(`[data-date-picker-trigger="${id}"]`);
  if (trigger) {
    const label = trigger.querySelector(".date-picker-label");
    if (label) label.textContent = input.value || "选择日期";
    trigger.setAttribute("aria-label", input.value ? `日期 ${input.value}` : "选择日期");
  }
}
function closeDatePicker() {
  openDatePicker?.panel.remove();
  openDatePicker = null;
}
function renderDatePickerCalendar() {
  if (!openDatePicker) return;
  const { input, month, panel } = openDatePicker;
  const first = new Date(month.getFullYear(), month.getMonth(), 1);
  const start = new Date(first);
  start.setDate(1 - ((first.getDay() + 6) % 7));
  const minDate = input.min ? localDate(input.min) : null;
  const maxDate = input.max ? localDate(input.max) : null;
  const weekdays = ["一", "二", "三", "四", "五", "六", "日"];
  const days = Array.from({ length: 42 }, (_, index) => {
    const day = new Date(start); day.setDate(start.getDate() + index);
    const value = dateValue(day);
    const outside = day.getMonth() !== month.getMonth();
    const disabled = Boolean((minDate && day < minDate) || (maxDate && day > maxDate));
    const selected = value === input.value;
    const today = value === dateValue(new Date());
    return `<button type="button" class="calendar-day${outside ? " outside" : ""}${selected ? " selected" : ""}${today ? " today" : ""}" data-calendar-date="${value}" ${disabled ? "disabled" : ""}>${day.getDate()}</button>`;
  }).join("");
  const previous = new Date(month.getFullYear(), month.getMonth() - 1, 1);
  const next = new Date(month.getFullYear(), month.getMonth() + 1, 1);
  const minMonth = minDate ? new Date(minDate.getFullYear(), minDate.getMonth(), 1) : null;
  const maxMonth = maxDate ? new Date(maxDate.getFullYear(), maxDate.getMonth(), 1) : null;
  const previousDisabled = Boolean(minMonth && previous < minMonth);
  const nextDisabled = Boolean(maxMonth && next > maxMonth);
  panel.innerHTML = `<div class="calendar-header"><button type="button" data-calendar-nav="-1" aria-label="上个月" ${previousDisabled ? "disabled" : ""}>‹</button><strong>${month.getFullYear()}年 ${month.getMonth() + 1}月</strong><button type="button" data-calendar-nav="1" aria-label="下个月" ${nextDisabled ? "disabled" : ""}>›</button></div><div class="calendar-weekdays">${weekdays.map(day => `<span>${day}</span>`).join("")}</div><div class="calendar-days">${days}</div>`;
}
function showDatePicker(trigger) {
  const input = document.getElementById(trigger.dataset.datePickerTrigger);
  if (!input) return;
  closeDatePicker();
  const selected = localDate(input.value) || new Date();
  const panel = document.createElement("div");
  panel.className = "date-picker-popover";
  panel.dataset.calendarPanel = "true";
  (trigger.closest("dialog") || document.body).append(panel);
  openDatePicker = { input, month: new Date(selected.getFullYear(), selected.getMonth(), 1), panel };
  renderDatePickerCalendar();
  const rect = trigger.getBoundingClientRect();
  const width = 276, height = panel.offsetHeight || 320;
  const left = Math.max(8, Math.min(rect.left, window.innerWidth - width - 8));
  const below = rect.bottom + height + 8 <= window.innerHeight;
  const top = below ? rect.bottom + 6 : Math.max(8, rect.top - height - 6);
  panel.style.left = `${left}px`; panel.style.top = `${top}px`;
}
function setDate(value) {
  if (!value) return;
  state.date = value;
  ["overview-date", "reports-date"].forEach(id => setPickerValue(id, value));
}
async function loadCollectors() {
  const { items } = await api("/api/collectors");
  $("#collector-cards").innerHTML = items.map(item => `<article class="collector-card" data-module="${esc(item.code)}"><div class="collector-top"><div><div class="collector-title">${esc(item.name)}</div><div class="collector-sub">${esc(item.page_kind)} · ${esc(item.notes || "")}</div></div><span class="collector-tag">${esc(item.section_label || "页面截图")}</span></div><label>截图方式<select data-module-field="screenshot_mode"><option value="element" ${item.screenshot_mode === "element" ? "selected" : ""}>指定 DOM 元素</option><option value="full_page" ${item.screenshot_mode === "full_page" ? "selected" : ""}>整页截图</option></select></label><label>栏目名称 / 区域标签<input data-module-field="section_label" value="${esc(item.section_label || "")}"></label><label>截图选择器（XPath 或 CSS）<input data-module-field="selector" value="${esc(item.selector || "")}" placeholder="可留空"></label><div class="collector-foot"><label class="toggle"><input data-module-field="enabled" type="checkbox" ${item.enabled ? "checked" : ""}><span class="toggle-track"></span>启用采集</label><button class="button button-secondary" data-module-save="${esc(item.code)}">保存步骤</button></div></article>`).join("");
}
async function saveCollector(button) {
  const card = button.closest(".collector-card"), values = {};
  $$('[data-module-field]', card).forEach(field => { const key = field.dataset.moduleField; values[key] = field.type === "checkbox" ? field.checked : field.value; });
  try { await api(`/api/collectors/${encodeURIComponent(button.dataset.moduleSave)}`, { method: "PATCH", body: JSON.stringify(values) }); toast("采集步骤已保存"); await loadCollectors(); }
  catch (error) { showError(error); }
}
  const settingKeys = ["browser_mode", "cloak_profile_name", "cloak_profile_id", "native_user_data_dir", "native_chromium_path", "native_context_options", "shop_name", "live_room_id", "live_author_id", "qianchuan_account_id", "feishu_app_id", "feishu_app_secret", "feishu_chat_id", "feishu_alert_open_id", "bitable_app_token", "bitable_table_id", "bitable_sync_key_field", "bitable_field_mapping", "report_message_template", "settle_delay_seconds", "auto_catchup_minutes"];
async function loadSettings() {
  const { settings } = await api("/api/settings"); state.settings = settings;
  settingKeys.forEach(key => { const input = document.getElementById(key); if (input && key !== "feishu_app_secret") input.value = settings[key] ?? ""; });
  $("#feishu_app_secret").placeholder = settings.feishu_app_secret_configured ? settings.feishu_app_secret_masked : "未配置";
  $("#secret-configured").textContent = settings.feishu_app_secret_configured ? `App Secret 已加密保存：${settings.feishu_app_secret_masked}；留空会继续使用已保存的密钥。` : "尚未配置 App Secret。";
  $("#feishu-state").textContent = settings.feishu_app_secret_configured ? "密钥已保存" : "未配置";
}
async function saveSettings() {
  const values = {};
  settingKeys.forEach(key => { const input = document.getElementById(key); if (input) values[key] = input.value; });
  try { const result = await api("/api/settings", { method: "PUT", body: JSON.stringify({ values }) }); state.settings = result.settings; toast("配置已加密保存"); await loadSettings(); await loadStatus(); }
  catch (error) { showError(error); }
}
async function checkFeishu(send = false) {
  const button = send ? $("#test-feishu") : $("#check-feishu"); button.disabled = true;
  try { const result = await api(`/api/integrations/feishu/${send ? "send-test" : "check"}`, { method: "POST" }); $("#feishu-state").textContent = send ? "测试消息已发" : "凭证有效"; toast(send ? "测试消息已发送到群聊" : `应用凭证有效${result.chat_id_configured ? "，群聊已配置" : "，尚未填写 Chat ID"}`); }
  catch (error) { $("#feishu-state").textContent = "检查失败"; showError(error); } finally { button.disabled = false; }
}
async function checkBitable() {
  const button = $("#check-bitable"); button.disabled = true;
  try { const result = await api("/api/integrations/bitable/check", { method: "POST" }); $("#bitable-state").textContent = "连接正常"; toast(`多维表格连接正常 · ${result.field_count} 个字段`); }
  catch (error) { $("#bitable-state").textContent = "检查失败"; showError(error); } finally { button.disabled = false; }
}
async function loadBitableFields() {
  try { const { items } = await api("/api/bitable/fields"); $("#bitable-fields").innerHTML = items.map(field => `<span class="field-chip">${esc(field.field_name)} <small>(${esc(field.type)})</small></span>`).join("") || "没有读取到表头"; toast(`已读取 ${items.length} 个字段`); }
  catch (error) { showError(error); }
}
async function exportCsv() {
  const button = $("#export-csv"); button.disabled = true;
  try {
    const first = await api(`/api/reports?work_date=${state.date}&page=1&page_size=100`);
    const reports = [...(first.items || [])];
    if (Number.isFinite(first.total)) {
      const pages = Math.ceil((first.total || 0) / 100);
      for (let page = 2; page <= pages; page++) {
        const result = await api(`/api/reports?work_date=${state.date}&page=${page}&page_size=100`);
        reports.push(...(result.items || []));
      }
    }
    if (!reports.length) { toast("没有可导出的报表", true); return; }
    const columns = state.columns;
    const rows = [columns.map(col => col.label), ...reports.map(row => columns.map(col => {
      let value = col.key === "work_date" ? row.work_date : col.key === "time_range" ? `${row.start_time}-${row.end_time}` : col.key === "host_name" || col.key === "promoter" ? row[col.key] : row.metrics?.[col.key];
      if (col.key === "work_date") value = dateLabel(value);
      if (col.kind === "percent") value = value == null ? "" : percent(value);
      return value ?? "";
    }))];
    const csv = "\ufeff" + rows.map(row => row.map(value => `"${String(value).replaceAll('"', '""')}"`).join(",")).join("\r\n");
    const link = document.createElement("a"); link.href = URL.createObjectURL(new Blob([csv], { type: "text/csv;charset=utf-8" })); link.download = `直播数据_${state.date}.csv`; link.click(); URL.revokeObjectURL(link.href);
  } catch (error) { showError(error); }
  finally { button.disabled = false; }
}
function changePage(key, page) {
  const item = state.pagination[key];
  const pages = Math.max(1, Math.ceil(item.total / item.pageSize));
  item.page = Math.max(1, Math.min(pages, page));
  if (key === "reports") loadReportView().catch(showError);
  if (key === "runs") loadRunsView().catch(showError);
}
window.liveopsPaginationChange = (key, page, pageSize) => {
  const item = state.pagination[key];
  if (pageSize !== item.pageSize) {
    item.pageSize = pageSize;
    try { localStorage.setItem(`liveops-page-size-${key}`, String(pageSize)); } catch { /* Current visit only. */ }
  }
  changePage(key, page);
};
document.addEventListener("click", event => {
  const calendarDay = event.target.closest("[data-calendar-date]");
  if (calendarDay && openDatePicker) {
    const input = openDatePicker.input;
    setPickerValue(input.id, calendarDay.dataset.calendarDate);
    input.dispatchEvent(new Event("change", { bubbles: true }));
    closeDatePicker();
    return;
  }
  const calendarNav = event.target.closest("[data-calendar-nav]");
  if (calendarNav && openDatePicker) {
    const month = openDatePicker.month;
    openDatePicker.month = new Date(month.getFullYear(), month.getMonth() + Number(calendarNav.dataset.calendarNav), 1);
    renderDatePickerCalendar();
    return;
  }
  const dateTrigger = event.target.closest("[data-date-picker-trigger]");
  if (dateTrigger) {
    if (openDatePicker?.input.id === dateTrigger.dataset.datePickerTrigger) closeDatePicker();
    else showDatePicker(dateTrigger);
    return;
  }
  if (openDatePicker && !event.target.closest("[data-calendar-panel]")) closeDatePicker();
  const pageButton = event.target.closest("[data-pagination-page]");
  if (pageButton) { changePage(pageButton.dataset.paginationPage, Number(pageButton.dataset.page)); return; }
  const pageJump = event.target.closest("[data-pagination-jump]");
  if (pageJump) {
    const input = document.querySelector(`[data-pagination-jump-input="${pageJump.dataset.paginationJump}"]`);
    const targetPage = Number(input?.value);
    if (Number.isFinite(targetPage)) changePage(pageJump.dataset.paginationJump, targetPage);
    return;
  }
  const nav = event.target.closest("[data-view], [data-nav]");
  if (nav) { setView(nav.dataset.view || nav.dataset.nav); return; }
  const report = event.target.closest("[data-report-id]"); if (report) { showReport(report.dataset.reportId); return; }
  const run = event.target.closest("[data-run-id]"); if (run) { showRun(run.dataset.runId); return; }
  const moduleSave = event.target.closest("[data-module-save]"); if (moduleSave) { saveCollector(moduleSave); return; }
  if (event.target.closest("[data-close-dialog]")) { event.target.closest("dialog")?.close(); }
});
document.addEventListener("change", event => {
  const size = event.target.closest("[data-pagination-size]");
  if (size) {
    const key = size.dataset.paginationSize;
    const item = state.pagination[key];
    item.pageSize = Number(size.value);
    item.page = 1;
    try { localStorage.setItem(`liveops-page-size-${key}`, String(item.pageSize)); } catch { /* page size remains active for this visit */ }
    if (key === "reports") loadReportView().catch(showError);
    if (key === "runs") loadRunsView().catch(showError);
  }
});
document.addEventListener("keydown", event => {
  if (event.key === "Escape") closeDatePicker();
  if (event.key === "Enter" && event.target.matches("[data-pagination-jump-input]")) {
    const key = event.target.dataset.paginationJumpInput;
    const targetPage = Number(event.target.value);
    if (Number.isFinite(targetPage)) changePage(key, targetPage);
  }
});
$("#go-schedules").addEventListener("click", () => setView("schedules"));
$("#refresh-button").addEventListener("click", refreshAll); $("#refresh-collectors").addEventListener("click", () => loadCollectors().catch(showError));
$("#save-settings").addEventListener("click", saveSettings); $("#check-feishu").addEventListener("click", () => checkFeishu(false)); $("#test-feishu").addEventListener("click", () => checkFeishu(true)); $("#check-bitable").addEventListener("click", checkBitable); $("#load-fields").addEventListener("click", loadBitableFields); $("#export-csv").addEventListener("click", exportCsv);
[["#overview-date", "overview"], ["#reports-date", "reports"]].forEach(([selector, view]) => $(selector).addEventListener("change", async event => { setPickerValue(event.target.id, event.target.value); setDate(event.target.value); if (view === "reports") state.pagination.reports.page = 1; try { await refreshVisibleView(); } catch (error) { showError(error); } }));
$("#detail-dialog").addEventListener("close", closeDatePicker);
window.addEventListener("resize", closeDatePicker);
async function start() {
  setDate(state.date);
  try { const [{ items: columns }, { settings }, status] = await Promise.all([api("/api/report-columns"), api("/api/settings"), api("/api/status")]); state.columns = columns; state.settings = settings; state.secondPrecisionSupported = status.second_precision_supported === true; $("#shop-name").textContent = settings.shop_name || "店铺未配置"; $("#browser-mode").textContent = status.browser_mode === "native" ? "Playwright 原生浏览器" : "Cloak profile 已配置"; await refreshVisibleView(); }
  catch (error) { showError(error); $("#app-status-text").textContent = "服务连接失败"; }
  setInterval(() => { if (document.hidden || $('dialog[open]')) return; if (state.view === "schedules") loadStatus().catch(showError); else refreshAll(); }, 7000);
}
start();

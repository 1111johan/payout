const MARKETPLACES = ["BE", "DE", "ES", "FR", "IT", "NL", "PL", "SE"];

const state = {
  apiKey: sessionStorage.getItem("amazonPayoutApiKey") || "",
  status: null,
  schedules: new Map(),
  financeRecords: [],
  batchBusy: false,
};

const elements = {
  authPanel: document.querySelector("#authPanel"),
  authForm: document.querySelector("#authForm"),
  apiKeyInput: document.querySelector("#apiKeyInput"),
  workspace: document.querySelector("#workspace"),
  modeBadge: document.querySelector("#modeBadge"),
  dryRunBadge: document.querySelector("#dryRunBadge"),
  serviceStatus: document.querySelector("#serviceStatus"),
  credentialStatus: document.querySelector("#credentialStatus"),
  schedulerStatus: document.querySelector("#schedulerStatus"),
  timezoneStatus: document.querySelector("#timezoneStatus"),
  connectionStatus: document.querySelector("#connectionStatus"),
  statusTimestamp: document.querySelector("#statusTimestamp"),
  productionWarning: document.querySelector("#productionWarning"),
  testCredentialsButton: document.querySelector("#testCredentialsButton"),
  selectAllMarketplaces: document.querySelector("#selectAllMarketplaces"),
  selectedMarketplaceCount: document.querySelector("#selectedMarketplaceCount"),
  marketplaceChecklist: document.querySelector("#marketplaceChecklist"),
  paymentMethodsButton: document.querySelector("#paymentMethodsButton"),
  previewButton: document.querySelector("#previewButton"),
  operationResult: document.querySelector("#operationResult"),
  paymentMethodsBody: document.querySelector("#paymentMethodsBody"),
  batchResultsBody: document.querySelector("#batchResultsBody"),
  financeSyncStatus: document.querySelector("#financeSyncStatus"),
  syncFinanceButton: document.querySelector("#syncFinanceButton"),
  exportFinanceButton: document.querySelector("#exportFinanceButton"),
  refreshFinanceButton: document.querySelector("#refreshFinanceButton"),
  financeDays: document.querySelector("#financeDays"),
  financeCurrency: document.querySelector("#financeCurrency"),
  financeTransferStatus: document.querySelector("#financeTransferStatus"),
  financeSummary: document.querySelector("#financeSummary"),
  financeCharts: document.querySelector("#financeCharts"),
  financeBody: document.querySelector("#financeBody"),
  scheduleTimezone: document.querySelector("#scheduleTimezone"),
  schedulesBody: document.querySelector("#schedulesBody"),
  refreshSchedulesButton: document.querySelector("#refreshSchedulesButton"),
  historyBody: document.querySelector("#historyBody"),
  refreshHistoryButton: document.querySelector("#refreshHistoryButton"),
  toast: document.querySelector("#toast"),
};

let toastTimer = null;

function showToast(message) {
  elements.toast.textContent = message;
  elements.toast.hidden = false;
  window.clearTimeout(toastTimer);
  toastTimer = window.setTimeout(() => { elements.toast.hidden = true; }, 4200);
}

function setBusy(button, busy) {
  button.disabled = busy;
  button.setAttribute("aria-busy", String(busy));
}

function delay(milliseconds) {
  return new Promise((resolve) => window.setTimeout(resolve, milliseconds));
}

function selectedMarketplaces() {
  return [...elements.marketplaceChecklist.querySelectorAll('input[type="checkbox"]:checked')].map((input) => input.value);
}

function updateMarketplaceSelection() {
  const selected = selectedMarketplaces();
  elements.selectAllMarketplaces.checked = selected.length === MARKETPLACES.length;
  elements.selectAllMarketplaces.indeterminate = selected.length > 0 && selected.length < MARKETPLACES.length;
  elements.selectedMarketplaceCount.textContent = `已选择 ${selected.length} 个站点`;
  elements.paymentMethodsButton.disabled = selected.length === 0 || state.batchBusy;
  elements.previewButton.disabled = selected.length === 0 || state.batchBusy;
  sessionStorage.setItem("amazonPayoutMarketplaces", JSON.stringify(selected));
}

function primaryMarketplace() {
  return selectedMarketplaces()[0] || "DE";
}

async function api(path, options = {}) {
  const headers = new Headers(options.headers || {});
  headers.set("X-API-Key", state.apiKey);
  if (options.body && !headers.has("Content-Type")) headers.set("Content-Type", "application/json");
  const response = await fetch(path, { ...options, headers });
  let payload;
  try {
    payload = await response.json();
  } catch {
    payload = { error: { message: `HTTP ${response.status}` } };
  }
  if (!response.ok) {
    if (response.status === 401) disconnect();
    const error = new Error(payload.error?.message || `HTTP ${response.status}`);
    error.code = payload.error?.code;
    error.details = payload.error?.details;
    throw error;
  }
  return payload;
}

function disconnect() {
  state.apiKey = "";
  sessionStorage.removeItem("amazonPayoutApiKey");
  elements.workspace.hidden = true;
  elements.authPanel.hidden = false;
  elements.modeBadge.className = "badge neutral";
  elements.modeBadge.textContent = "未连接";
  elements.dryRunBadge.className = "badge neutral";
  elements.dryRunBadge.textContent = "状态未知";
}

function setBadge(element, text, tone) {
  element.textContent = text;
  element.className = `badge ${tone}`;
}

function formatDate(value) {
  if (!value) return "-";
  return new Intl.DateTimeFormat("zh-CN", {
    year: "numeric", month: "2-digit", day: "2-digit",
    hour: "2-digit", minute: "2-digit", second: "2-digit",
  }).format(new Date(value));
}

function formatAmount(amount) {
  if (!amount?.currency || amount.value === null || amount.value === undefined) return "-";
  const numeric = Number(amount.value);
  if (!Number.isFinite(numeric)) return `${amount.currency} ${amount.value}`;
  try {
    return new Intl.NumberFormat("zh-CN", { style: "currency", currency: amount.currency }).format(numeric);
  } catch {
    return `${amount.currency} ${numeric.toFixed(2)}`;
  }
}

function renderStatus(payload) {
  state.status = payload;
  setBadge(elements.modeBadge, payload.mode === "production" ? "生产环境" : "静态沙盒", payload.mode === "production" ? "warn" : "good");
  setBadge(elements.dryRunBadge, payload.dryRun ? "预演模式" : "真实提交", payload.dryRun ? "good" : "bad");
  elements.serviceStatus.textContent = payload.service === "ok" ? "正常" : "异常";
  elements.credentialStatus.textContent = payload.credentialsComplete ? "配置完整" : "配置不完整";
  elements.schedulerStatus.textContent = payload.schedulerRunning ? "运行中" : (payload.schedulerEnabled ? "未运行" : "已关闭");
  elements.timezoneStatus.textContent = payload.timezone;
  elements.scheduleTimezone.textContent = `时区：${payload.timezone}`;
  elements.productionWarning.hidden = payload.mode !== "production";
  const test = payload.lastConnectionTest;
  elements.connectionStatus.textContent = test ? `${test.status === "ok" ? "成功" : "失败"} · ${test.code || test.spApi || "SP-API"} · ${formatDate(test.updatedAt)}` : "尚未测试";
  elements.statusTimestamp.textContent = `刷新于 ${formatDate(new Date().toISOString())}`;
  elements.previewButton.textContent = payload.dryRun ? "批量执行预演" : "批量提交提现";
}

async function loadStatus() {
  const payload = await api("/v1/status");
  renderStatus(payload);
}

function renderPaymentMethods(results) {
  const rows = [];
  results.forEach((result) => {
    const methods = result.payload?.data?.paymentMethods || [];
    if (!methods.length) {
      const row = document.createElement("tr");
      [result.marketplace, result.error ? `${result.error.code || "ERROR"}: ${result.error.message}` : "没有返回收款方式", "-", "-", "-"].forEach((value) => {
        const cell = document.createElement("td");
        cell.textContent = value;
        row.appendChild(cell);
      });
      rows.push(row);
      return;
    }
    methods.forEach((method) => {
      const row = document.createElement("tr");
      [result.marketplace, method.paymentMethodType || "-", method.countryCode || "-", method.assignmentType === "DEFAULT_DEPOSIT_METHOD" ? "是" : "-", method.tail || "-"].forEach((value) => {
        const cell = document.createElement("td");
        cell.textContent = value;
        row.appendChild(cell);
      });
      rows.push(row);
    });
  });
  elements.paymentMethodsBody.replaceChildren(...rows);
}

function renderBatchResults(results) {
  if (!results.length) {
    elements.batchResultsBody.innerHTML = '<tr><td colspan="4" class="empty-cell">暂无批量执行结果</td></tr>';
    return;
  }
  elements.batchResultsBody.replaceChildren(...results.map((result) => {
    const row = document.createElement("tr");
    const successful = !result.error;
    const values = [result.marketplace, result.action, successful ? result.status : "失败", successful ? (result.detail || "-") : `${result.error.code || "ERROR"}: ${result.error.message}`];
    values.forEach((value, index) => {
      const cell = document.createElement("td");
      cell.textContent = value;
      if (index === 2) cell.className = `status-text ${successful ? "status-SUBMITTED" : "status-FAILED"}`;
      row.appendChild(cell);
    });
    return row;
  }));
}

async function queryPaymentMethods() {
  const marketplaces = selectedMarketplaces();
  if (!marketplaces.length) return;
  state.batchBusy = true;
  setBusy(elements.paymentMethodsButton, true);
  setBusy(elements.previewButton, true);
  const results = [];
  for (let index = 0; index < marketplaces.length; index += 1) {
    const marketplace = marketplaces[index];
    elements.operationResult.className = "result-strip";
    elements.operationResult.textContent = `正在查询 ${marketplace}（${index + 1}/${marketplaces.length}）`;
    try {
      const payload = await api(`/v1/payment-methods?marketplace=${encodeURIComponent(marketplace)}&type=BANK_ACCOUNT`);
      results.push({ marketplace, payload });
    } catch (error) {
      results.push({ marketplace, error });
    }
    if (index < marketplaces.length - 1) await delay(2100);
  }
  renderPaymentMethods(results);
  const failures = results.filter((result) => result.error).length;
  elements.operationResult.className = failures ? "result-strip bad" : "result-strip good";
  elements.operationResult.textContent = `收款方式查询完成：成功 ${results.length - failures}，失败 ${failures}`;
  setBusy(elements.paymentMethodsButton, false);
  setBusy(elements.previewButton, false);
  state.batchBusy = false;
  updateMarketplaceSelection();
}

function idempotencyKey(marketplace) {
  return `manual-${marketplace}-${new Date().toISOString().replace(/[^0-9]/g, "")}`;
}

async function executePayout() {
  const marketplaces = selectedMarketplaces();
  if (!marketplaces.length) return;
  const live = state.status && !state.status.dryRun;
  if (live && !window.confirm(`确认向 Amazon 依次提交 ${marketplaces.join("、")} 站提现请求？每个站点都会转出全部符合条件的余额，整个过程可能需要数分钟。`)) return;
  state.batchBusy = true;
  setBusy(elements.previewButton, true);
  setBusy(elements.paymentMethodsButton, true);
  const results = [];
  for (let index = 0; index < marketplaces.length; index += 1) {
    const marketplace = marketplaces[index];
    elements.operationResult.className = "result-strip";
    elements.operationResult.textContent = `正在${live ? "提交" : "预演"} ${marketplace}（${index + 1}/${marketplaces.length}）`;
    const headers = { "Idempotency-Key": idempotencyKey(marketplace) };
    if (live) headers["X-Payout-Confirmation"] = `CONFIRM:${marketplace}`;
    try {
      const payload = await api("/v1/payouts", {
        method: "POST",
        headers,
        body: JSON.stringify({ marketplace, accountType: "Standard Orders" }),
      });
      results.push({
        marketplace,
        action: live ? "真实提交" : "预演",
        status: payload.status === "PREVIEW_ONLY" ? "预演完成" : "已提交",
        detail: payload.amazon?.payoutReferenceId || "未调用 Amazon 提现 POST",
      });
    } catch (error) {
      results.push({ marketplace, action: live ? "真实提交" : "预演", error });
    }
    renderBatchResults(results);
    await loadHistory();
    if (live && index < marketplaces.length - 1) {
      elements.operationResult.textContent = `${marketplace} 已处理，等待 Amazon 限流间隔后继续`;
      await delay(65_000);
    }
  }
  const failures = results.filter((result) => result.error).length;
  elements.operationResult.className = failures ? "result-strip bad" : "result-strip good";
  elements.operationResult.textContent = `${live ? "真实提交" : "预演"}完成：成功 ${results.length - failures}，失败 ${failures}`;
  setBusy(elements.previewButton, false);
  setBusy(elements.paymentMethodsButton, false);
  state.batchBusy = false;
  updateMarketplaceSelection();
}

function renderFinanceSummary(payload) {
  const totals = payload.totals || [];
  const lastSync = payload.lastSync || state.status?.lastFinanceSync;
  elements.financeSyncStatus.textContent = lastSync
    ? `${lastSync.status === "ok" ? "同步成功" : "同步失败"} · ${lastSync.received ?? "-"} 条 · ${formatDate(lastSync.updatedAt)}`
    : "尚未同步 Amazon 财务记录";
  if (!totals.length) {
    elements.financeSummary.innerHTML = '<div class="empty-summary">当前期间没有财务记录</div>';
    return;
  }
  elements.financeSummary.replaceChildren(...totals.map((total) => {
    const block = document.createElement("div");
    block.className = "finance-currency-summary";
    block.innerHTML = `
      <h3>${total.currency}</h3>
      <div class="finance-values">
        <div><span>成功转账</span><strong>${formatAmount({currency: total.currency, value: total.succeeded})}</strong></div>
        <div><span>其他状态</span><strong>${formatAmount({currency: total.currency, value: total.pending})}</strong></div>
        <div><span>付款组</span><strong>${total.groupCount}</strong></div>
      </div>
    `;
    return block;
  }));
}

function renderFinanceRecords(items) {
  if (!items.length) {
    elements.financeBody.innerHTML = '<tr><td colspan="7" class="empty-cell">当前筛选条件没有提现台账</td></tr>';
    return;
  }
  elements.financeBody.replaceChildren(...items.map((item) => {
    const row = document.createElement("tr");
    const values = [
      formatDate(item.fundTransferDate || item.groupStart),
      item.marketplace || "-",
      item.transferStatus || item.processingStatus || "-",
      formatAmount(item.originalAmount),
      formatAmount(item.convertedAmount),
      item.accountTail || "-",
      item.traceId || item.groupId,
    ];
    values.forEach((value, index) => {
      const cell = document.createElement("td");
      cell.textContent = value;
      if (index === 2) cell.className = "status-text";
      row.appendChild(cell);
    });
    return row;
  }));
}

function drawMonthlyChart(canvas, labels, values, currency) {
  const width = Math.max(canvas.clientWidth, 280);
  const height = 220;
  const scale = window.devicePixelRatio || 1;
  canvas.width = Math.round(width * scale);
  canvas.height = Math.round(height * scale);
  const context = canvas.getContext("2d");
  context.scale(scale, scale);
  context.clearRect(0, 0, width, height);
  const margin = { top: 18, right: 12, bottom: 36, left: 48 };
  const plotWidth = width - margin.left - margin.right;
  const plotHeight = height - margin.top - margin.bottom;
  const maximum = Math.max(0, ...values);
  const minimum = Math.min(0, ...values);
  const range = maximum - minimum || 1;
  const y = (value) => margin.top + ((maximum - value) / range) * plotHeight;
  const zeroY = y(0);

  context.strokeStyle = "#cbd2d9";
  context.lineWidth = 1;
  context.beginPath();
  context.moveTo(margin.left, zeroY);
  context.lineTo(width - margin.right, zeroY);
  context.stroke();

  const slot = plotWidth / Math.max(labels.length, 1);
  const barWidth = Math.min(42, slot * 0.58);
  context.font = "11px -apple-system, BlinkMacSystemFont, Segoe UI, sans-serif";
  context.textAlign = "center";
  labels.forEach((label, index) => {
    const value = values[index];
    const centerX = margin.left + slot * index + slot / 2;
    const valueY = y(value);
    context.fillStyle = value >= 0 ? "#248053" : "#b63b3b";
    context.fillRect(centerX - barWidth / 2, Math.min(valueY, zeroY), barWidth, Math.max(Math.abs(zeroY - valueY), 1));
    context.fillStyle = "#66717d";
    context.fillText(label.slice(5), centerX, height - 13);
  });

  context.textAlign = "right";
  context.fillStyle = "#66717d";
  context.fillText(`${currency} ${maximum.toFixed(2)}`, margin.left - 6, margin.top + 4);
  if (minimum < 0) context.fillText(`${currency} ${minimum.toFixed(2)}`, margin.left - 6, margin.top + plotHeight);
}

function renderFinanceCharts(records) {
  if (!records.length) {
    elements.financeCharts.innerHTML = '<div class="empty-summary">当前筛选条件没有趋势数据</div>';
    return;
  }
  const grouped = new Map();
  records.forEach((record) => {
    const amount = record.originalAmount;
    const date = record.fundTransferDate || record.groupStart;
    if (!amount?.currency || !date) return;
    const month = date.slice(0, 7);
    if (!grouped.has(amount.currency)) grouped.set(amount.currency, { months: new Map(), succeeded: 0, other: 0 });
    const bucket = grouped.get(amount.currency);
    const value = Number(amount.value || 0);
    bucket.months.set(month, (bucket.months.get(month) || 0) + value);
    if ((record.transferStatus || "").toLowerCase() === "succeeded") bucket.succeeded += Math.abs(value);
    else bucket.other += Math.abs(value);
  });
  const panels = [];
  const charts = [];
  [...grouped.entries()].sort(([left], [right]) => left.localeCompare(right)).forEach(([currency, data]) => {
    const labels = [...data.months.keys()].sort();
    const values = labels.map((label) => data.months.get(label));
    const total = data.succeeded + data.other || 1;
    const panel = document.createElement("div");
    panel.className = "chart-panel";
    const heading = document.createElement("h3");
    heading.textContent = `${currency} 月度净额`;
    const description = document.createElement("p");
    description.textContent = `${labels.length} 个月 · 绿色为正值，红色为负值`;
    const canvas = document.createElement("canvas");
    canvas.setAttribute("role", "img");
    canvas.setAttribute("aria-label", `${currency} 月度提现净额图`);
    const statusBar = document.createElement("div");
    statusBar.className = "status-bar";
    const successBar = document.createElement("span");
    successBar.className = "status-bar-success";
    successBar.style.width = `${(data.succeeded / total) * 100}%`;
    const otherBar = document.createElement("span");
    otherBar.className = "status-bar-other";
    otherBar.style.width = `${(data.other / total) * 100}%`;
    statusBar.append(successBar, otherBar);
    panel.append(heading, description, canvas, statusBar);
    panels.push(panel);
    charts.push({ canvas, labels, values, currency });
  });
  elements.financeCharts.replaceChildren(...panels);
  window.requestAnimationFrame(() => charts.forEach((chart) => drawMonthlyChart(chart.canvas, chart.labels, chart.values, chart.currency)));
}

function financeQuery(includeFilters = true) {
  const params = new URLSearchParams({ days: elements.financeDays.value, limit: "500" });
  if (includeFilters && elements.financeCurrency.value) params.set("currency", elements.financeCurrency.value);
  if (includeFilters && elements.financeTransferStatus.value) params.set("status", elements.financeTransferStatus.value);
  return params.toString();
}

async function loadFinance() {
  const [summary, records] = await Promise.all([
    api(`/v1/finance/summary?${financeQuery(false)}`),
    api(`/v1/finance/records?${financeQuery(true)}`),
  ]);
  state.financeRecords = records.items || [];
  renderFinanceSummary(summary);
  renderFinanceRecords(state.financeRecords);
  renderFinanceCharts(state.financeRecords);
}

async function syncFinance() {
  setBusy(elements.syncFinanceButton, true);
  try {
    const payload = await api("/v1/finance/sync", {
      method: "POST",
      body: JSON.stringify({ days: Number(elements.financeDays.value) }),
    });
    showToast(`Amazon 财务记录同步完成：${payload.saved} 条`);
    await Promise.all([loadFinance(), loadHistory(), loadStatus()]);
  } catch (error) {
    showToast(`${error.code || "ERROR"}: ${error.message}`);
  } finally {
    setBusy(elements.syncFinanceButton, false);
  }
}

async function exportFinance() {
  setBusy(elements.exportFinanceButton, true);
  try {
    const response = await fetch(`/v1/finance/export.csv?days=${encodeURIComponent(elements.financeDays.value)}`, {
      headers: { "X-API-Key": state.apiKey },
    });
    if (!response.ok) throw new Error(`CSV 导出失败：HTTP ${response.status}`);
    const blob = await response.blob();
    const url = URL.createObjectURL(blob);
    const link = document.createElement("a");
    link.href = url;
    link.download = `amazon-payout-ledger-${new Date().toISOString().slice(0, 10)}.csv`;
    document.body.appendChild(link);
    link.click();
    link.remove();
    URL.revokeObjectURL(url);
    showToast("财务台账已导出");
  } catch (error) {
    showToast(error.message);
  } finally {
    setBusy(elements.exportFinanceButton, false);
  }
}

function scheduleRow(marketplace, schedule) {
  const row = document.createElement("tr");
  row.dataset.marketplace = marketplace;
  row.innerHTML = `
    <td><strong>${marketplace}</strong></td>
    <td><input class="schedule-enabled" type="checkbox" aria-label="启用 ${marketplace} 每日任务" ${schedule?.enabled ? "checked" : ""}></td>
    <td><input class="schedule-time" type="time" value="${schedule?.runAt || "09:00"}" aria-label="${marketplace} 执行时间"></td>
    <td class="schedule-next">${schedule?.nextRunAt ? formatDate(schedule.nextRunAt) : "-"}</td>
    <td><div class="row-actions"><button type="button" class="button small secondary schedule-save">保存</button><button type="button" class="button small danger schedule-delete">关闭</button></div></td>
  `;
  row.querySelector(".schedule-save").addEventListener("click", () => saveSchedule(row));
  row.querySelector(".schedule-delete").addEventListener("click", () => deleteSchedule(row));
  return row;
}

function renderSchedules(items) {
  state.schedules = new Map(items.map((item) => [item.marketplace, item]));
  elements.schedulesBody.replaceChildren(...MARKETPLACES.map((marketplace) => scheduleRow(marketplace, state.schedules.get(marketplace))));
}

async function loadSchedules() {
  const payload = await api("/v1/schedules");
  renderSchedules(payload.items || []);
}

async function saveSchedule(row) {
  const button = row.querySelector(".schedule-save");
  setBusy(button, true);
  try {
    const marketplace = row.dataset.marketplace;
    const payload = await api(`/v1/schedules/${marketplace}`, {
      method: "PUT",
      body: JSON.stringify({
        enabled: row.querySelector(".schedule-enabled").checked,
        runAt: row.querySelector(".schedule-time").value,
      }),
    });
    row.querySelector(".schedule-next").textContent = formatDate(payload.nextRunAt);
    showToast(`${marketplace} 每日任务已保存`);
  } catch (error) {
    showToast(`${error.code || "ERROR"}: ${error.message}`);
  } finally {
    setBusy(button, false);
  }
}

async function deleteSchedule(row) {
  const button = row.querySelector(".schedule-delete");
  setBusy(button, true);
  try {
    const marketplace = row.dataset.marketplace;
    await api(`/v1/schedules/${marketplace}`, { method: "DELETE" });
    row.querySelector(".schedule-enabled").checked = false;
    row.querySelector(".schedule-next").textContent = "-";
    showToast(`${marketplace} 每日任务已关闭`);
  } catch (error) {
    showToast(`${error.code || "ERROR"}: ${error.message}`);
  } finally {
    setBusy(button, false);
  }
}

const STATUS_LABELS = {
  PREVIEW_ONLY: "预演",
  SUBMITTED: "已提交",
  FAILED: "失败",
  SKIPPED: "跳过",
  UNKNOWN: "结果未知",
  PENDING: "处理中",
};

function renderHistory(items) {
  if (!items.length) {
    elements.historyBody.innerHTML = '<tr><td colspan="5" class="empty-cell">暂无执行记录</td></tr>';
    return;
  }
  elements.historyBody.replaceChildren(...items.map((item) => {
    const row = document.createElement("tr");
    const amount = item.convertedAmount || item.amount;
    const reference = item.payoutReferenceId || item.amazonRequestId || item.idempotencyKey;
    const detail = item.errorCode ? `${item.errorCode}: ${item.errorMessage || "-"}` : `${amount ? `${formatAmount(amount)} · ` : ""}${reference}`;
    const values = [formatDate(item.startedAt), item.marketplace, item.trigger === "auto" ? "定时" : "手动", STATUS_LABELS[item.status] || item.status, detail];
    values.forEach((value, index) => {
      const cell = document.createElement("td");
      cell.textContent = value || "-";
      if (index === 3) cell.className = `status-text status-${item.status}`;
      row.appendChild(cell);
    });
    return row;
  }));
}

async function loadHistory() {
  const payload = await api("/v1/payouts/history?limit=100");
  renderHistory(payload.items || []);
}

async function testCredentials() {
  setBusy(elements.testCredentialsButton, true);
  try {
    const payload = await api("/v1/credentials/test", {
      method: "POST",
      body: JSON.stringify({ marketplace: primaryMarketplace() }),
    });
    showToast(`Amazon 连接成功：${payload.mode}`);
  } catch (error) {
    showToast(`${error.code || "ERROR"}: ${error.message}`);
  } finally {
    await loadStatus().catch(() => {});
    setBusy(elements.testCredentialsButton, false);
  }
}

async function connect() {
  await loadStatus();
  sessionStorage.setItem("amazonPayoutApiKey", state.apiKey);
  elements.authPanel.hidden = true;
  elements.workspace.hidden = false;
  await Promise.all([loadSchedules(), loadHistory(), loadFinance()]);
}

elements.authForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  state.apiKey = elements.apiKeyInput.value.trim();
  try {
    await connect();
  } catch (error) {
    disconnect();
    showToast(error.message);
  }
});

let savedMarketplaceSelection = MARKETPLACES;
try {
  const parsedSelection = JSON.parse(sessionStorage.getItem("amazonPayoutMarketplaces") || "null");
  if (Array.isArray(parsedSelection)) savedMarketplaceSelection = parsedSelection.filter((item) => MARKETPLACES.includes(item));
} catch {
  savedMarketplaceSelection = MARKETPLACES;
}

MARKETPLACES.forEach((marketplace) => {
  const label = document.createElement("label");
  label.className = "marketplace-option";
  const checkbox = document.createElement("input");
  checkbox.type = "checkbox";
  checkbox.value = marketplace;
  checkbox.checked = savedMarketplaceSelection.includes(marketplace);
  checkbox.addEventListener("change", updateMarketplaceSelection);
  const text = document.createElement("span");
  text.textContent = marketplace;
  label.append(checkbox, text);
  elements.marketplaceChecklist.appendChild(label);
});

elements.selectAllMarketplaces.addEventListener("change", () => {
  elements.marketplaceChecklist.querySelectorAll('input[type="checkbox"]').forEach((checkbox) => {
    checkbox.checked = elements.selectAllMarketplaces.checked;
  });
  updateMarketplaceSelection();
});
updateMarketplaceSelection();

elements.paymentMethodsButton.addEventListener("click", queryPaymentMethods);
elements.previewButton.addEventListener("click", executePayout);
elements.testCredentialsButton.addEventListener("click", testCredentials);
elements.syncFinanceButton.addEventListener("click", syncFinance);
elements.exportFinanceButton.addEventListener("click", exportFinance);
elements.refreshFinanceButton.addEventListener("click", () => loadFinance().catch((error) => showToast(error.message)));
elements.refreshSchedulesButton.addEventListener("click", () => loadSchedules().catch((error) => showToast(error.message)));
elements.refreshHistoryButton.addEventListener("click", () => loadHistory().catch((error) => showToast(error.message)));

if (state.apiKey) {
  elements.apiKeyInput.value = state.apiKey;
  connect().catch(() => disconnect());
}

window.setInterval(() => {
  if (!state.apiKey || elements.workspace.hidden) return;
  Promise.all([loadStatus(), loadHistory()]).catch(() => {});
}, 30_000);

window.setInterval(() => {
  if (!state.apiKey || elements.workspace.hidden) return;
  loadFinance().catch(() => {});
}, 300_000);

let chartResizeTimer = null;
window.addEventListener("resize", () => {
  window.clearTimeout(chartResizeTimer);
  chartResizeTimer = window.setTimeout(() => renderFinanceCharts(state.financeRecords), 120);
});

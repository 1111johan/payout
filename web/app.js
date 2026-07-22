const MARKETPLACES = ["BE", "DE", "ES", "FR", "IT", "NL", "PL", "SE"];
const ZINIAO_MARKETPLACES = ["US", "UK", "CA"];
const ALL_MARKETPLACES = [...ZINIAO_MARKETPLACES, ...MARKETPLACES];
const SCHEDULE_MARKETPLACES = ALL_MARKETPLACES;

const state = {
  apiKey: sessionStorage.getItem("amazonPayoutApiKey") || "",
  connected: false,
  status: null,
  schedules: new Map(),
  financeRecords: [],
  batchBusy: false,
  allSitesBusy: false,
  allSites: new Map(),
  historyItems: [],
  ziniaoRunning: new Map(),
  ziniaoStores: [],
  ziniaoRunningItems: [],
  ziniaoBalances: new Map(),
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
  ziniaoSummary: document.querySelector("#ziniaoSummary"),
  ziniaoEnabledStatus: document.querySelector("#ziniaoEnabledStatus"),
  ziniaoConfigStatus: document.querySelector("#ziniaoConfigStatus"),
  ziniaoInstallStatus: document.querySelector("#ziniaoInstallStatus"),
  ziniaoServiceStatus: document.querySelector("#ziniaoServiceStatus"),
  ziniaoStartClientButton: document.querySelector("#ziniaoStartClientButton"),
  ziniaoUpdateCoreButton: document.querySelector("#ziniaoUpdateCoreButton"),
  ziniaoRefreshButton: document.querySelector("#ziniaoRefreshButton"),
  ziniaoBatchPayoutButton: document.querySelector("#ziniaoBatchPayoutButton"),
  ziniaoExitButton: document.querySelector("#ziniaoExitButton"),
  ziniaoResult: document.querySelector("#ziniaoResult"),
  ziniaoStoresBody: document.querySelector("#ziniaoStoresBody"),
  allSitesRefreshButton: document.querySelector("#allSitesRefreshButton"),
  allSitesPayoutButton: document.querySelector("#allSitesPayoutButton"),
  allSitesResult: document.querySelector("#allSitesResult"),
  allSitesBody: document.querySelector("#allSitesBody"),
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
  scheduleSelectAll: document.querySelector("#scheduleSelectAll"),
  scheduleBulkTime: document.querySelector("#scheduleBulkTime"),
  scheduleBulkSaveButton: document.querySelector("#scheduleBulkSaveButton"),
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
  elements.paymentMethodsButton.disabled = selected.length === 0 || state.batchBusy || state.allSitesBusy;
  elements.previewButton.disabled = selected.length === 0 || state.batchBusy || state.allSitesBusy;
  sessionStorage.setItem("amazonPayoutMarketplaces", JSON.stringify(selected));
}

function primaryMarketplace() {
  return selectedMarketplaces()[0] || "DE";
}

async function api(path, options = {}) {
  const headers = new Headers(options.headers || {});
  if (state.apiKey) headers.set("X-API-Key", state.apiKey);
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
  state.connected = false;
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
  elements.allSitesPayoutButton.textContent = payload.dryRun ? "预演所有可用站点" : "申请所有可用站点";
}

async function loadStatus() {
  const payload = await api("/v1/status");
  renderStatus(payload);
}

function renderZiniaoStatus(payload) {
  const enabled = Boolean(payload.enabled);
  const configured = Boolean(payload.configured);
  const installed = Boolean(payload.clientInstalled);
  const reachable = Boolean(payload.serviceReachable);
  elements.ziniaoEnabledStatus.textContent = enabled ? "已启用" : "已关闭";
  elements.ziniaoConfigStatus.textContent = configured ? "配置完整" : "待填写企业账号";
  elements.ziniaoInstallStatus.textContent = installed ? `已安装 · ${payload.clientVersion || "-"}` : "未找到客户端";
  elements.ziniaoServiceStatus.textContent = reachable ? `运行中 · ${payload.servicePort}` : "未运行";
  elements.ziniaoSummary.textContent = configured
    ? "通过本机 WebDriver 控制紫鸟店铺环境"
    : "请在 .env 中填写紫鸟企业名、自动化用户名和密码";
  elements.ziniaoStartClientButton.disabled = !enabled || !installed || reachable;
  elements.ziniaoUpdateCoreButton.disabled = !enabled || !configured || !reachable;
  elements.ziniaoRefreshButton.disabled = !enabled || !configured || !reachable;
  elements.ziniaoBatchPayoutButton.disabled = state.allSitesBusy || !enabled || !configured || !reachable || !state.status?.ziniaoPayoutEnabled;
  elements.ziniaoExitButton.disabled = !enabled || !reachable;
}

async function loadZiniaoStatus() {
  const payload = await api("/v1/ziniao/status");
  renderZiniaoStatus(payload);
  return payload;
}

function ziniaoStoreKey(store) {
  return `${store.controlType}:${store.controlId}`;
}

function ziniaoBalanceKey(store, marketplace) {
  return `${ziniaoStoreKey(store)}:${marketplace}`;
}

function ziniaoStoreKeys(store) {
  return [
    ziniaoStoreKey(store),
    store.browserId ? `browserId:${store.browserId}` : null,
    store.browserOauth ? `browserOauth:${store.browserOauth}` : null,
  ].filter(Boolean);
}

function ziniaoMarketplace(store) {
  const source = `${store.platformName || ""} ${store.browserName || ""}`.toUpperCase();
  if (source.includes("美国") || source.includes("UNITED STATES") || /(^|\W)US(\W|$)/.test(source)) return "US";
  if (source.includes("英国") || source.includes("UNITED KINGDOM") || /(^|\W)(UK|GB)(\W|$)/.test(source)) return "UK";
  if (source.includes("加拿大") || source.includes("CANADA") || /(^|\W)CA(\W|$)/.test(source)) return "CA";
  return "";
}

function isAmazonZiniaoStore(store) {
  const source = `${store.platformName || ""} ${store.browserName || ""}`.toUpperCase();
  return source.includes("亚马逊") || source.includes("AMAZON") || ZINIAO_MARKETPLACES.includes(ziniaoMarketplace(store));
}

function ziniaoStoreForMarketplace(stores, marketplace) {
  const available = stores.filter((store) => !store.isExpired);
  return available.find((store) => ziniaoMarketplace(store) === marketplace)
    || available.find((store) => isAmazonZiniaoStore(store));
}

function ziniaoStoreRows(stores) {
  const marketplaceRows = ZINIAO_MARKETPLACES
    .map((marketplace) => ({ marketplace, store: ziniaoStoreForMarketplace(stores, marketplace) }))
    .filter((item) => item.store);
  const assignedStores = new Set(marketplaceRows.map((item) => item.store));
  const otherRows = stores
    .filter((store) => !assignedStores.has(store))
    .map((store) => ({ marketplace: ziniaoMarketplace(store), store }));
  return [...marketplaceRows, ...otherRows];
}

function allSiteDefault(marketplace) {
  return {
    marketplace,
    channel: MARKETPLACES.includes(marketplace) ? "Transfers API" : "紫鸟 Seller Central",
    target: "-",
    funds: MARKETPLACES.includes(marketplace) ? "Amazon 提交时确定" : "待读取",
    eligibility: "待读取",
    eligibilityTone: "",
    requestable: false,
    runResult: "-",
  };
}

function updateAllSite(marketplace, changes) {
  const current = state.allSites.get(marketplace) || allSiteDefault(marketplace);
  state.allSites.set(marketplace, { ...current, ...changes });
  renderAllSites();
}

function latestMarketplaceResult(marketplace) {
  const item = state.historyItems.find((historyItem) => historyItem.marketplace === marketplace);
  if (!item) return "-";
  const label = STATUS_LABELS[item.status] || item.status;
  return `${label} · ${formatDate(item.startedAt)}`;
}

function renderAllSites() {
  elements.allSitesBody.replaceChildren(...ALL_MARKETPLACES.map((marketplace) => {
    const item = state.allSites.get(marketplace) || allSiteDefault(marketplace);
    const row = document.createElement("tr");
    const values = [
      marketplace,
      item.channel,
      item.target,
      item.funds,
      item.eligibility,
      latestMarketplaceResult(marketplace),
      item.runResult,
    ];
    values.forEach((value, index) => {
      const cell = document.createElement("td");
      cell.textContent = value || "-";
      if (index === 4 && item.eligibilityTone) cell.className = `status-text status-${item.eligibilityTone}`;
      if (index === 6 && item.runTone) cell.className = `status-text status-${item.runTone}`;
      row.appendChild(cell);
    });
    return row;
  }));
}

function setAllSitesBusy(busy) {
  state.allSitesBusy = busy;
  elements.allSitesRefreshButton.disabled = busy;
  elements.allSitesPayoutButton.disabled = busy || !state.status;
  elements.paymentMethodsButton.disabled = busy || state.batchBusy || selectedMarketplaces().length === 0;
  elements.previewButton.disabled = busy || state.batchBusy || selectedMarketplaces().length === 0;
  if (state.status?.ziniao) renderZiniaoStatus(state.status.ziniao);
}

function renderZiniaoStores(stores, runningStores = []) {
  state.ziniaoStores = stores;
  state.ziniaoRunningItems = runningStores;
  state.ziniaoRunning = new Map();
  runningStores.forEach((store) => ziniaoStoreKeys(store).forEach((key) => state.ziniaoRunning.set(key, store)));
  if (!stores.length) {
    elements.ziniaoStoresBody.innerHTML = '<tr><td colspan="7" class="empty-cell">当前自动化账号没有可控制店铺</td></tr>';
    return;
  }
  const renderedEnvironmentControls = new Set();
  elements.ziniaoStoresBody.replaceChildren(...ziniaoStoreRows(stores).map(({ store, marketplace }) => {
    const row = document.createElement("tr");
    const running = ziniaoStoreKeys(store).map((key) => state.ziniaoRunning.get(key)).find(Boolean);
    const balance = marketplace ? state.ziniaoBalances.get(ziniaoBalanceKey(store, marketplace)) : null;
    const values = [
      store.browserName || "-",
      marketplace ? `Amazon - ${marketplace}` : (store.platformName || store.platformId || "-"),
      marketplace || store.siteId || "-",
      store.isExpired ? "代理已过期" : "正常",
      running?.debuggingPort || "-",
      balance ? ziniaoAccountsFunds(balance) : "-",
    ];
    values.forEach((value, index) => {
      const cell = document.createElement("td");
      cell.textContent = value;
      if (index === 3) cell.className = store.isExpired ? "status-text status-FAILED" : "status-text status-SUBMITTED";
      row.appendChild(cell);
    });
    const actionCell = document.createElement("td");
    const actions = document.createElement("div");
    actions.className = "row-actions";
    const environmentKey = ziniaoStoreKey(store);
    if (!renderedEnvironmentControls.has(environmentKey)) {
      renderedEnvironmentControls.add(environmentKey);
      const environmentButton = document.createElement("button");
      environmentButton.type = "button";
      environmentButton.className = `button small ${running ? "danger" : "secondary"}`;
      environmentButton.textContent = running ? "停止" : "启动";
      environmentButton.disabled = Boolean(store.isExpired);
      environmentButton.addEventListener("click", () => controlZiniaoStore(store, running, environmentButton));
      actions.appendChild(environmentButton);
    }
    if (running && marketplace) {
      const balanceButton = document.createElement("button");
      balanceButton.type = "button";
      balanceButton.className = "button small secondary";
      balanceButton.textContent = "读取余额";
      balanceButton.addEventListener("click", () => readZiniaoBalance(store, running, marketplace, balanceButton));
      const payoutButton = document.createElement("button");
      payoutButton.type = "button";
      payoutButton.className = "button small primary";
      payoutButton.textContent = "申请付款";
      const requestableAccounts = ziniaoRequestableAccounts(balance);
      payoutButton.disabled = !state.status?.ziniaoPayoutEnabled || Boolean(balance && !requestableAccounts.length);
      payoutButton.addEventListener("click", () => prepareZiniaoPayout(store, running, marketplace, payoutButton));
      actions.append(balanceButton, payoutButton);
    }
    actionCell.appendChild(actions);
    row.appendChild(actionCell);
    return row;
  }));
}

function ziniaoControlPayload(store, marketplace) {
  return {
    controlType: store.controlType,
    controlId: store.controlId,
    marketplace,
  };
}

function ziniaoRequestableAccounts(balance) {
  return (balance?.accounts || []).filter((account) => account.canRequest);
}

function ziniaoAccountLabel(accountType) {
  return {
    "Standard Orders": "标准订单",
    "Invoice Payment Orders": "发票支付订单",
    "Deferred Transactions": "延迟交易",
  }[accountType] || accountType;
}

function ziniaoAccountsFunds(balance) {
  const accounts = balance?.accounts || [];
  if (!accounts.length) return balance?.totalAvailable || "-";
  return accounts.map((account) => `${ziniaoAccountLabel(account.accountType)} ${account.amount}`).join("；");
}

function ziniaoAccountsStatus(balance) {
  const accounts = balance?.accounts || [];
  if (!accounts.length) return "没有读取到账户余额";
  return accounts
    .map((account) => `${ziniaoAccountLabel(account.accountType)} ${account.amount}（${account.canRequest ? "可申请" : "跳过"}）`)
    .join("；");
}

async function loadZiniaoBalanceForControl(control, marketplace) {
  const params = new URLSearchParams(ziniaoControlPayload(control, marketplace));
  return api(`/v1/ziniao/amazon/balance?${params.toString()}`);
}

async function prepareZiniaoAccounts(control, marketplace, balance) {
  const preparedItems = [];
  const errors = [];
  for (const account of ziniaoRequestableAccounts(balance)) {
    try {
      const prepared = await api("/v1/ziniao/amazon/prepare", {
        method: "POST",
        body: JSON.stringify({
          ...ziniaoControlPayload(control, marketplace),
          accountIndex: account.index,
        }),
      });
      preparedItems.push({ account, prepared });
    } catch (error) {
      errors.push({ account, error });
    }
  }
  return { preparedItems, errors };
}

async function readZiniaoBalance(store, running, marketplace, button) {
  setBusy(button, true);
  try {
    const payload = await loadZiniaoBalanceForControl(running || store, marketplace);
    state.ziniaoBalances.set(ziniaoBalanceKey(store, marketplace), payload);
    renderZiniaoStores(state.ziniaoStores, state.ziniaoRunningItems);
    elements.ziniaoResult.className = "result-strip good";
    elements.ziniaoResult.textContent = `${store.browserName}：${ziniaoAccountsStatus(payload)}`;
  } catch (error) {
    elements.ziniaoResult.className = "result-strip bad";
    elements.ziniaoResult.textContent = `${error.code || "ERROR"}: ${error.message}`;
  } finally {
    setBusy(button, false);
  }
}

async function prepareZiniaoPayout(store, running, marketplace, button) {
  setBusy(button, true);
  try {
    const control = running || store;
    const balance = await loadZiniaoBalanceForControl(control, marketplace);
    state.ziniaoBalances.set(ziniaoBalanceKey(store, marketplace), balance);
    const { preparedItems, errors } = await prepareZiniaoAccounts(control, marketplace, balance);
    if (!preparedItems.length) {
      elements.ziniaoResult.className = "result-strip bad";
      elements.ziniaoResult.textContent = errors.length
        ? `${marketplace} 准备失败：${errors.map((item) => `${ziniaoAccountLabel(item.account.accountType)} ${item.error.code || "ERROR"}`).join("；")}`
        : `${marketplace} 当前没有可申请付款的账户`;
      return;
    }
    const summary = preparedItems
      .map(({ prepared }) => `${ziniaoAccountLabel(prepared.accountType)} · ${prepared.amount} · 收款账户尾号 ${prepared.accountTail}`)
      .join("\n");
    const confirmed = window.confirm(
      `确认向 Amazon ${marketplace} 站真实请求以下付款？\n\n${summary}\n\n不可申请或零余额账户已自动跳过。该操作会转移真实资金，提交后不要重复点击。`,
    );
    if (!confirmed) {
      elements.ziniaoResult.className = "result-strip";
      elements.ziniaoResult.textContent = `${store.browserName} 付款申请已取消`;
      return;
    }
    const results = [];
    for (const item of preparedItems) {
      try {
        const result = await api("/v1/ziniao/amazon/submit", {
          method: "POST",
          headers: { "X-Ziniao-Payout-Confirmation": `CONFIRM:${item.prepared.token}` },
          body: JSON.stringify({ ...ziniaoControlPayload(control, marketplace), token: item.prepared.token }),
        });
        results.push({ accountType: item.prepared.accountType, result });
      } catch (error) {
        results.push({ accountType: item.prepared.accountType, error });
      }
    }
    const failures = results.filter((item) => item.error || item.result?.status !== "submitted");
    elements.ziniaoResult.className = failures.length ? "result-strip bad" : "result-strip good";
    elements.ziniaoResult.textContent = `${store.browserName}：${results.map((item) => `${ziniaoAccountLabel(item.accountType)} ${item.error?.code || item.result.status}`).join("；")}`;
    renderZiniaoStores(state.ziniaoStores, state.ziniaoRunningItems);
  } catch (error) {
    elements.ziniaoResult.className = "result-strip bad";
    elements.ziniaoResult.textContent = `${error.code || "ERROR"}: ${error.message}`;
  } finally {
    setBusy(button, false);
  }
}

async function waitForZiniaoStore(store, attempts = 20) {
  for (let attempt = 0; attempt < attempts; attempt += 1) {
    const runningPayload = await api("/v1/ziniao/running");
    state.ziniaoRunningItems = runningPayload.items || [];
    state.ziniaoRunning = new Map();
    state.ziniaoRunningItems.forEach((item) => ziniaoStoreKeys(item).forEach((key) => state.ziniaoRunning.set(key, item)));
    const running = ziniaoStoreKeys(store).map((key) => state.ziniaoRunning.get(key)).find(Boolean);
    if (running?.debuggingPort) return running;
    if (attempt + 1 < attempts) await delay(1000);
  }
  const error = new Error("紫鸟店铺已启动，但未在限定时间内返回调试端口");
  error.code = "ZINIAO_STORE_START_TIMEOUT";
  throw error;
}

function configuredZiniaoStore(marketplace) {
  return ziniaoStoreForMarketplace(state.ziniaoStores, marketplace);
}

function runningZiniaoStore(store) {
  return ziniaoStoreKeys(store).map((key) => state.ziniaoRunning.get(key)).find(Boolean);
}

async function ensureZiniaoStoreRunning(store) {
  const running = runningZiniaoStore(store);
  if (running?.debuggingPort) return running;
  await api("/v1/ziniao/stores/start", {
    method: "POST",
    body: JSON.stringify({ controlType: store.controlType, controlId: store.controlId }),
  });
  return waitForZiniaoStore(store);
}

async function batchZiniaoPayout() {
  setBusy(elements.ziniaoBatchPayoutButton, true);
  const targetMarketplaces = ZINIAO_MARKETPLACES;
  const targets = targetMarketplaces.map((marketplace) => ({ marketplace, store: configuredZiniaoStore(marketplace) }));
  const availableTargets = targets.filter((item) => item.store);
  const missing = targets.filter((item) => !item.store).map((item) => item.marketplace);
  const preparedItems = [];
  const skippedItems = [];
  try {
    if (!availableTargets.length) {
      elements.ziniaoResult.className = "result-strip bad";
      elements.ziniaoResult.textContent = "紫鸟自动化账号中没有 US、UK 或 CA 店铺";
      return;
    }
    for (let index = 0; index < availableTargets.length; index += 1) {
      const { store, marketplace } = availableTargets[index];
      elements.ziniaoResult.className = "result-strip";
      elements.ziniaoResult.textContent = `正在准备 ${marketplace} · ${store.browserName}（${index + 1}/${availableTargets.length}）`;
      let running = runningZiniaoStore(store);
      try {
        if (!running?.debuggingPort) running = await ensureZiniaoStoreRunning(store);
        const control = running || store;
        const balance = await loadZiniaoBalanceForControl(control, marketplace);
        state.ziniaoBalances.set(ziniaoBalanceKey(store, marketplace), balance);
        const prepared = await prepareZiniaoAccounts(control, marketplace, balance);
        prepared.preparedItems.forEach((item) => {
          preparedItems.push({ store, running: control, marketplace, ...item });
        });
        prepared.errors.forEach((item) => {
          skippedItems.push({
            marketplace,
            store: store.browserName,
            accountType: item.account.accountType,
            code: item.error.code || "ERROR",
            message: item.error.message,
          });
        });
        if (!prepared.preparedItems.length && !prepared.errors.length) {
          skippedItems.push({ marketplace, store: store.browserName, code: "NO_REQUESTABLE_ACCOUNTS", message: "没有可申请账户" });
        }
      } catch (error) {
        skippedItems.push({ marketplace, store: store.browserName, code: error.code || "ERROR", message: error.message });
      }
    }
    if (!preparedItems.length) {
      elements.ziniaoResult.className = "result-strip bad";
      const skippedSummary = skippedItems.map((item) => `${item.marketplace}: ${item.code}`).join("；");
      const missingSummary = missing.length ? `紫鸟中未找到：${missing.join("、")}` : "";
      const details = [skippedSummary, missingSummary].filter(Boolean).join("；");
      elements.ziniaoResult.textContent = `批量检查完成，没有可申请付款的站点。${details ? ` ${details}` : ""}`;
      await loadZiniaoStores().catch(() => {});
      return;
    }
    const summary = preparedItems
      .map((item) => `${item.marketplace} · ${ziniaoAccountLabel(item.prepared.accountType)} · ${item.prepared.amount} · 账户尾号 ${item.prepared.accountTail}`)
      .join("\n");
    const confirmed = window.confirm(
      `确认批量向 Amazon 请求真实付款？\n\n${summary}\n\n${missing.length ? `紫鸟中未找到：${missing.join("、")}\n\n` : ""}不可申请或零余额账户已自动跳过，每个账户只提交一次，结果未知时不会自动重试。`,
    );
    if (!confirmed) {
      elements.ziniaoResult.className = "result-strip";
      elements.ziniaoResult.textContent = "批量付款申请已取消";
      return;
    }
    const results = [];
    for (let index = 0; index < preparedItems.length; index += 1) {
      const item = preparedItems[index];
      elements.ziniaoResult.textContent = `正在提交 ${item.marketplace} · ${ziniaoAccountLabel(item.prepared.accountType)}（${index + 1}/${preparedItems.length}）`;
      try {
        const result = await api("/v1/ziniao/amazon/submit", {
          method: "POST",
          headers: { "X-Ziniao-Payout-Confirmation": `CONFIRM:${item.prepared.token}` },
          body: JSON.stringify({
            ...ziniaoControlPayload(item.running, item.marketplace),
            token: item.prepared.token,
          }),
        });
        results.push(`${item.marketplace} ${ziniaoAccountLabel(item.prepared.accountType)}: ${result.status}`);
      } catch (error) {
        results.push(`${item.marketplace} ${ziniaoAccountLabel(item.prepared.accountType)}: ${error.code || "ERROR"}`);
      }
    }
    elements.ziniaoResult.className = results.every((item) => item.endsWith(": submitted")) ? "result-strip good" : "result-strip bad";
    elements.ziniaoResult.textContent = `批量付款处理完成：${results.join("；")}`;
    await loadZiniaoStores().catch(() => {});
  } finally {
    setBusy(elements.ziniaoBatchPayoutButton, false);
  }
}

async function loadZiniaoStores() {
  setBusy(elements.ziniaoRefreshButton, true);
  try {
    const [storesPayload, runningPayload] = await Promise.all([
      api("/v1/ziniao/stores"),
      api("/v1/ziniao/running"),
    ]);
    renderZiniaoStores(storesPayload.items || [], runningPayload.items || []);
    elements.ziniaoResult.className = "result-strip good";
    elements.ziniaoResult.textContent = `紫鸟店铺读取完成：${(storesPayload.items || []).length} 个`;
  } catch (error) {
    elements.ziniaoResult.className = "result-strip bad";
    elements.ziniaoResult.textContent = `${error.code || "ERROR"}: ${error.message}`;
    throw error;
  } finally {
    setBusy(elements.ziniaoRefreshButton, false);
    if (state.allSitesBusy) elements.ziniaoRefreshButton.disabled = true;
  }
}

async function collectAllSiteData() {
  let ziniaoLoadError = null;
  let successCount = 0;
  let unavailableCount = 0;
  try {
    await loadZiniaoStores();
  } catch (error) {
    ziniaoLoadError = error;
  }

  for (let index = 0; index < ALL_MARKETPLACES.length; index += 1) {
    const marketplace = ALL_MARKETPLACES[index];
    elements.allSitesResult.className = "result-strip";
    elements.allSitesResult.textContent = `正在读取 ${marketplace}（${index + 1}/${ALL_MARKETPLACES.length}）`;
    updateAllSite(marketplace, {
      eligibility: "读取中",
      eligibilityTone: "",
      requestable: false,
      runResult: "-",
      runTone: "",
      error: null,
    });

    if (ZINIAO_MARKETPLACES.includes(marketplace)) {
      if (ziniaoLoadError) {
        updateAllSite(marketplace, {
          target: "紫鸟控制端不可用",
          funds: "-",
          eligibility: ziniaoLoadError.code || "读取失败",
          eligibilityTone: "FAILED",
          error: ziniaoLoadError,
        });
        unavailableCount += 1;
        continue;
      }
      const store = configuredZiniaoStore(marketplace);
      if (!store) {
        updateAllSite(marketplace, {
          target: "紫鸟未授权对应店铺",
          funds: "-",
          eligibility: "未配置",
          eligibilityTone: "FAILED",
        });
        unavailableCount += 1;
        continue;
      }
      try {
        const running = await ensureZiniaoStoreRunning(store);
        const params = new URLSearchParams(ziniaoControlPayload(running, marketplace));
        const balance = await api(`/v1/ziniao/amazon/balance?${params.toString()}`);
        const requestableAccounts = ziniaoRequestableAccounts(balance);
        const requestable = Boolean(requestableAccounts.length && state.status?.ziniaoPayoutEnabled);
        state.ziniaoBalances.set(ziniaoBalanceKey(store, marketplace), balance);
        updateAllSite(marketplace, {
          target: store.browserName,
          funds: ziniaoAccountsFunds(balance),
          eligibility: requestable
            ? `${requestableAccounts.length} 个账户可申请`
            : (requestableAccounts.length ? "付款开关关闭" : "当前没有可申请账户"),
          eligibilityTone: requestable ? "SUBMITTED" : "FAILED",
          requestable,
          store,
          running,
          balance,
        });
        if (requestable) successCount += 1;
        else unavailableCount += 1;
      } catch (error) {
        updateAllSite(marketplace, {
          target: store.browserName,
          funds: "-",
          eligibility: error.code || "读取失败",
          eligibilityTone: "FAILED",
          error,
          store,
        });
        unavailableCount += 1;
      }
      continue;
    }

    try {
      const payload = await api(`/v1/payment-methods?marketplace=${encodeURIComponent(marketplace)}&type=BANK_ACCOUNT`);
      const methods = payload.data?.paymentMethods || [];
      const method = methods.find((item) => item.assignmentType === "DEFAULT_DEPOSIT_METHOD") || methods[0];
      const requestable = Boolean(method);
      updateAllSite(marketplace, {
        target: method ? `${method.paymentMethodType || "BANK_ACCOUNT"} · 尾号 ${method.tail || "-"}` : "未返回银行账户",
        funds: "Amazon 提交时确定",
        eligibility: requestable ? "接口可申请" : "收款账户不可用",
        eligibilityTone: requestable ? "SUBMITTED" : "FAILED",
        requestable,
        method,
        paymentMethods: methods,
      });
      if (requestable) successCount += 1;
      else unavailableCount += 1;
    } catch (error) {
      updateAllSite(marketplace, {
        target: "-",
        funds: "-",
        eligibility: error.code || "查询失败",
        eligibilityTone: "FAILED",
        error,
      });
      unavailableCount += 1;
    }
    if (index < ALL_MARKETPLACES.length - 1) await delay(2100);
  }

  elements.allSitesResult.className = unavailableCount ? "result-strip bad" : "result-strip good";
  elements.allSitesResult.textContent = `全站点数据已更新：可处理 ${successCount}，未配置或不可用 ${unavailableCount}`;
  renderZiniaoStores(state.ziniaoStores, state.ziniaoRunningItems);
  return { successCount, unavailableCount };
}

async function refreshAllSiteData() {
  setAllSitesBusy(true);
  try {
    await collectAllSiteData();
  } catch (error) {
    elements.allSitesResult.className = "result-strip bad";
    elements.allSitesResult.textContent = `${error.code || "ERROR"}: ${error.message}`;
  } finally {
    setAllSitesBusy(false);
  }
}

async function applyAllSitePayouts() {
  setAllSitesBusy(true);
  const live = Boolean(state.status && !state.status.dryRun);
  const preparedZiniao = [];
  const results = [];
  try {
    await collectAllSiteData();
    const apiTargets = MARKETPLACES
      .map((marketplace) => state.allSites.get(marketplace))
      .filter((item) => item?.requestable);
    const ziniaoTargets = live
      ? ZINIAO_MARKETPLACES.map((marketplace) => state.allSites.get(marketplace)).filter((item) => item?.requestable)
      : [];

    for (let index = 0; index < ziniaoTargets.length; index += 1) {
      const item = ziniaoTargets[index];
      elements.allSitesResult.className = "result-strip";
      elements.allSitesResult.textContent = `正在准备 ${item.marketplace} 付款确认（${index + 1}/${ziniaoTargets.length}）`;
      try {
        const prepared = await prepareZiniaoAccounts(item.running, item.marketplace, item.balance);
        prepared.preparedItems.forEach((preparedItem) => {
          preparedZiniao.push({ ...item, ...preparedItem });
        });
        prepared.errors.forEach((preparedError) => {
          results.push({
            marketplace: item.marketplace,
            accountType: preparedError.account.accountType,
            error: preparedError.error,
          });
        });
        const count = prepared.preparedItems.length;
        updateAllSite(item.marketplace, {
          runResult: count ? `待确认 · ${count} 个账户` : (prepared.errors[0]?.error.code || "没有可申请账户"),
          runTone: count ? "PENDING" : "FAILED",
        });
      } catch (error) {
        results.push({ marketplace: item.marketplace, error });
        updateAllSite(item.marketplace, { runResult: error.code || "准备失败", runTone: "FAILED" });
      }
    }

    if (!apiTargets.length && !preparedZiniao.length) {
      elements.allSitesResult.className = "result-strip bad";
      elements.allSitesResult.textContent = "当前没有可申请付款的站点";
      return;
    }

    if (live) {
      const summaryLines = [];
      if (apiTargets.length) {
        summaryLines.push(`Transfers API：${apiTargets.map((item) => `${item.marketplace}（${item.target}）`).join("、")}；金额由 Amazon 按当前可用余额确定`);
      }
      preparedZiniao.forEach((item) => {
        summaryLines.push(`${item.marketplace} · ${ziniaoAccountLabel(item.prepared.accountType)} · ${item.prepared.amount} · 收款账户尾号 ${item.prepared.accountTail}`);
      });
      const skipped = ALL_MARKETPLACES.filter((marketplace) => {
        return !apiTargets.some((item) => item.marketplace === marketplace)
          && !preparedZiniao.some((item) => item.marketplace === marketplace);
      });
      const confirmed = window.confirm(
        `确认向所有当前可用站点请求真实付款？\n\n${summaryLines.join("\n")}\n\n${skipped.length ? `本次不处理：${skipped.join("、")}\n\n` : ""}不可申请或零余额账户已自动跳过，提交后结果未知的账户不会自动重试。`,
      );
      if (!confirmed) {
        preparedZiniao.forEach((item) => updateAllSite(item.marketplace, { runResult: "已取消", runTone: "" }));
        elements.allSitesResult.className = "result-strip";
        elements.allSitesResult.textContent = "全站点付款申请已取消";
        return;
      }
    }

    for (let index = 0; index < preparedZiniao.length; index += 1) {
      const item = preparedZiniao[index];
      elements.allSitesResult.textContent = `正在提交 ${item.marketplace} · ${ziniaoAccountLabel(item.prepared.accountType)}（${index + 1}/${preparedZiniao.length}）`;
      try {
        const payload = await api("/v1/ziniao/amazon/submit", {
          method: "POST",
          headers: { "X-Ziniao-Payout-Confirmation": `CONFIRM:${item.prepared.token}` },
          body: JSON.stringify({
            ...ziniaoControlPayload(item.running, item.marketplace),
            token: item.prepared.token,
          }),
        });
        results.push({ marketplace: item.marketplace, accountType: item.prepared.accountType, status: payload.status, payload });
        updateAllSite(item.marketplace, {
          runResult: `${payload.status === "submitted" ? "已提交" : payload.status} · ${payload.amount}`,
          runTone: payload.status === "submitted" ? "SUBMITTED" : "FAILED",
        });
      } catch (error) {
        results.push({ marketplace: item.marketplace, error });
        updateAllSite(item.marketplace, { runResult: error.code || "提交失败", runTone: "FAILED" });
      }
    }

    for (let index = 0; index < apiTargets.length; index += 1) {
      const item = apiTargets[index];
      elements.allSitesResult.textContent = `正在${live ? "提交" : "预演"} ${item.marketplace} Transfers API（${index + 1}/${apiTargets.length}）`;
      const headers = { "Idempotency-Key": idempotencyKey(item.marketplace) };
      if (live) headers["X-Payout-Confirmation"] = `CONFIRM:${item.marketplace}`;
      try {
        const payload = await api("/v1/payouts", {
          method: "POST",
          headers,
          body: JSON.stringify({ marketplace: item.marketplace, accountType: "Standard Orders" }),
        });
        const successful = ["PREVIEW_ONLY", "SUBMITTED"].includes(payload.status);
        results.push({ marketplace: item.marketplace, status: payload.status, payload });
        updateAllSite(item.marketplace, {
          runResult: payload.status === "PREVIEW_ONLY" ? "预演完成" : (payload.status === "SUBMITTED" ? "已提交" : payload.status),
          runTone: successful ? "SUBMITTED" : "FAILED",
        });
      } catch (error) {
        results.push({ marketplace: item.marketplace, error });
        updateAllSite(item.marketplace, { runResult: error.code || "提交失败", runTone: "FAILED" });
      }
      await loadHistory().catch(() => {});
      if (live && index < apiTargets.length - 1) {
        elements.allSitesResult.textContent = `${item.marketplace} 已处理，等待 Amazon 限流间隔后继续`;
        await delay(65_000);
      }
    }

    const successfulStatuses = new Set(["submitted", "SUBMITTED", "PREVIEW_ONLY"]);
    const failures = results.filter((result) => result.error || !successfulStatuses.has(result.status)).length;
    elements.allSitesResult.className = failures ? "result-strip bad" : "result-strip good";
    elements.allSitesResult.textContent = `${live ? "真实付款" : "预演"}处理完成：成功 ${results.length - failures}，失败 ${failures}`;
  } catch (error) {
    elements.allSitesResult.className = "result-strip bad";
    elements.allSitesResult.textContent = `${error.code || "ERROR"}: ${error.message}`;
  } finally {
    setAllSitesBusy(false);
  }
}

async function startZiniaoClient() {
  setBusy(elements.ziniaoStartClientButton, true);
  try {
    const payload = await api("/v1/ziniao/client/start", { method: "POST", body: "{}" });
    elements.ziniaoResult.className = "result-strip good";
    elements.ziniaoResult.textContent = payload.alreadyRunning ? "紫鸟控制端已经运行" : "紫鸟控制端已启动";
    await loadZiniaoStatus();
  } catch (error) {
    elements.ziniaoResult.className = "result-strip bad";
    elements.ziniaoResult.textContent = `${error.code || "ERROR"}: ${error.message}`;
  } finally {
    setBusy(elements.ziniaoStartClientButton, false);
  }
}

async function updateZiniaoCore() {
  setBusy(elements.ziniaoUpdateCoreButton, true);
  try {
    const payload = await api("/v1/ziniao/core/update", { method: "POST", body: "{}" });
    elements.ziniaoResult.className = payload.status === "ready" ? "result-strip good" : "result-strip";
    elements.ziniaoResult.textContent = payload.status === "ready" ? "紫鸟浏览器内核已就绪" : `内核更新中：${payload.message || payload.statusCode}`;
  } catch (error) {
    elements.ziniaoResult.className = "result-strip bad";
    elements.ziniaoResult.textContent = `${error.code || "ERROR"}: ${error.message}`;
  } finally {
    setBusy(elements.ziniaoUpdateCoreButton, false);
  }
}

async function exitZiniaoClient() {
  setBusy(elements.ziniaoExitButton, true);
  try {
    await api("/v1/ziniao/client/exit", { method: "POST", body: "{}" });
    renderZiniaoStores([]);
    elements.ziniaoResult.className = "result-strip";
    elements.ziniaoResult.textContent = "紫鸟控制端已退出";
    await loadZiniaoStatus();
  } catch (error) {
    elements.ziniaoResult.className = "result-strip bad";
    elements.ziniaoResult.textContent = `${error.code || "ERROR"}: ${error.message}`;
  } finally {
    setBusy(elements.ziniaoExitButton, false);
  }
}

async function controlZiniaoStore(store, running, button) {
  setBusy(button, true);
  const action = running ? "stop" : "start";
  try {
    const payload = await api(`/v1/ziniao/stores/${action}`, {
      method: "POST",
      body: JSON.stringify({
        controlType: running?.controlType || store.controlType,
        controlId: running?.controlId || store.controlId,
        duplicate: running?.duplicate || 0,
      }),
    });
    elements.ziniaoResult.className = "result-strip good";
    elements.ziniaoResult.textContent = running
      ? `${store.browserName} 已停止`
      : `${store.browserName} 已启动，调试端口 ${payload.debuggingPort || "-"}`;
    await loadZiniaoStores();
  } catch (error) {
    elements.ziniaoResult.className = "result-strip bad";
    elements.ziniaoResult.textContent = `${error.code || "ERROR"}: ${error.message}`;
  } finally {
    setBusy(button, false);
  }
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
    const headers = new Headers();
    if (state.apiKey) headers.set("X-API-Key", state.apiKey);
    const response = await fetch(`/v1/finance/export.csv?days=${encodeURIComponent(elements.financeDays.value)}`, {
      headers,
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
  row.querySelector(".schedule-enabled").addEventListener("change", updateScheduleBulkSelection);
  row.querySelector(".schedule-save").addEventListener("click", () => saveSchedule(row));
  row.querySelector(".schedule-delete").addEventListener("click", () => deleteSchedule(row));
  return row;
}

function updateScheduleBulkSelection() {
  const checkboxes = [...elements.schedulesBody.querySelectorAll(".schedule-enabled")];
  const selectedCount = checkboxes.filter((checkbox) => checkbox.checked).length;
  elements.scheduleSelectAll.checked = checkboxes.length > 0 && selectedCount === checkboxes.length;
  elements.scheduleSelectAll.indeterminate = selectedCount > 0 && selectedCount < checkboxes.length;
}

function renderSchedules(items) {
  state.schedules = new Map(items.map((item) => [item.marketplace, item]));
  elements.schedulesBody.replaceChildren(...SCHEDULE_MARKETPLACES.map((marketplace) => scheduleRow(marketplace, state.schedules.get(marketplace))));
  const enabledTimes = [...new Set(items.filter((item) => item.enabled).map((item) => item.runAt))];
  if (enabledTimes.length === 1) elements.scheduleBulkTime.value = enabledTimes[0];
  updateScheduleBulkSelection();
}

async function loadSchedules() {
  const payload = await api("/v1/schedules");
  renderSchedules(payload.items || []);
}

async function saveSchedule(row) {
  const button = row.querySelector(".schedule-save");
  setBusy(button, true);
  try {
    const payload = await persistScheduleRow(row);
    row.querySelector(".schedule-next").textContent = formatDate(payload.nextRunAt);
    showToast(`${row.dataset.marketplace} 每日任务已保存`);
  } catch (error) {
    showToast(`${error.code || "ERROR"}: ${error.message}`);
  } finally {
    setBusy(button, false);
  }
}

async function persistScheduleRow(row) {
  return api(`/v1/schedules/${row.dataset.marketplace}`, {
    method: "PUT",
    body: JSON.stringify({
      enabled: row.querySelector(".schedule-enabled").checked,
      runAt: row.querySelector(".schedule-time").value,
    }),
  });
}

async function saveScheduleBatch() {
  const runAt = elements.scheduleBulkTime.value;
  if (!/^([01]\d|2[0-3]):[0-5]\d$/.test(runAt)) {
    showToast("请选择统一执行时间");
    return;
  }
  const rows = [...elements.schedulesBody.querySelectorAll("tr")];
  setBusy(elements.scheduleBulkSaveButton, true);
  rows.forEach((row) => {
    row.querySelectorAll("button, input").forEach((control) => { control.disabled = true; });
  });
  try {
    for (const row of rows) {
      if (row.querySelector(".schedule-enabled").checked) row.querySelector(".schedule-time").value = runAt;
      const payload = await persistScheduleRow(row);
      row.querySelector(".schedule-next").textContent = formatDate(payload.nextRunAt);
    }
    const selectedCount = rows.filter((row) => row.querySelector(".schedule-enabled").checked).length;
    showToast(`已保存 ${selectedCount} 个站点的每日任务`);
  } catch (error) {
    showToast(`${error.code || "ERROR"}: ${error.message}`);
  } finally {
    rows.forEach((row) => {
      row.querySelectorAll("button, input").forEach((control) => { control.disabled = false; });
    });
    setBusy(elements.scheduleBulkSaveButton, false);
    updateScheduleBulkSelection();
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
    updateScheduleBulkSelection();
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
  state.historyItems = payload.items || [];
  renderHistory(state.historyItems);
  renderAllSites();
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
  state.connected = true;
  if (state.apiKey) sessionStorage.setItem("amazonPayoutApiKey", state.apiKey);
  else sessionStorage.removeItem("amazonPayoutApiKey");
  elements.authPanel.hidden = true;
  elements.workspace.hidden = false;
  await Promise.all([loadSchedules(), loadHistory(), loadFinance(), loadZiniaoStatus()]);
  refreshAllSiteData().catch(() => {});
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
elements.ziniaoStartClientButton.addEventListener("click", startZiniaoClient);
elements.ziniaoUpdateCoreButton.addEventListener("click", updateZiniaoCore);
elements.ziniaoRefreshButton.addEventListener("click", () => loadZiniaoStores().catch(() => {}));
elements.ziniaoBatchPayoutButton.addEventListener("click", batchZiniaoPayout);
elements.ziniaoExitButton.addEventListener("click", exitZiniaoClient);
elements.allSitesRefreshButton.addEventListener("click", refreshAllSiteData);
elements.allSitesPayoutButton.addEventListener("click", applyAllSitePayouts);
elements.syncFinanceButton.addEventListener("click", syncFinance);
elements.exportFinanceButton.addEventListener("click", exportFinance);
elements.refreshFinanceButton.addEventListener("click", () => loadFinance().catch((error) => showToast(error.message)));
elements.refreshSchedulesButton.addEventListener("click", () => loadSchedules().catch((error) => showToast(error.message)));
elements.scheduleSelectAll.addEventListener("change", () => {
  elements.schedulesBody.querySelectorAll(".schedule-enabled").forEach((checkbox) => {
    checkbox.checked = elements.scheduleSelectAll.checked;
  });
  updateScheduleBulkSelection();
});
elements.scheduleBulkSaveButton.addEventListener("click", saveScheduleBatch);
elements.refreshHistoryButton.addEventListener("click", () => loadHistory().catch((error) => showToast(error.message)));

if (state.apiKey) elements.apiKeyInput.value = state.apiKey;
connect().catch((error) => {
  disconnect();
  showToast(error.message);
});

renderAllSites();

window.setInterval(() => {
  if (!state.connected || elements.workspace.hidden) return;
  Promise.all([loadStatus(), loadHistory(), loadZiniaoStatus()]).catch(() => {});
}, 30_000);

window.setInterval(() => {
  if (!state.connected || elements.workspace.hidden) return;
  loadFinance().catch(() => {});
}, 300_000);

let chartResizeTimer = null;
window.addEventListener("resize", () => {
  window.clearTimeout(chartResizeTimer);
  chartResizeTimer = window.setTimeout(() => renderFinanceCharts(state.financeRecords), 120);
});

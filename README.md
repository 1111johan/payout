# Amazon Payout Console

这是一个仅供卖家本人使用的本地 Amazon SP-API Transfers 管理工具。它提供网页控制台、凭证测试、支付方式查询、提现预演、每日任务、SQLite 历史记录和生产安全门槛。

当前版本：`v1.0.0`（2026-07-22）。这是首个完整的本地生产工作站版本，详细变更见 [CHANGELOG.md](CHANGELOG.md)。

| 通道 | 站点 | 账户类型 |
| --- | --- | --- |
| Amazon Transfers API | BE、DE、ES、FR、IT、NL、PL、SE | Standard Orders |
| 紫鸟 Seller Central | US、UK、CA | 标准订单、发票支付订单、延迟交易；仅提交按钮可用且余额非零的账户 |

默认配置只监听 `127.0.0.1`、使用静态 Sandbox、开启 `DRY_RUN`，不会发起真实提现。

## Amazon 能力边界

- Transfers API 支持：BE、DE、ES、FR、IT、NL、PL、SE。
- UK/GB、US、CA 不支持通过 Transfers API 主动提现。
- 不能指定提现金额。Amazon 会把符合条件的余额付到 Seller Central 中的默认收款方式。
- 每个站点和 `Standard Orders` 账户类型，24 小时最多发起一次按需付款。
- 静态 Sandbox 返回固定样例，不能证明真实账户、余额或银行账户有效。

## 当前功能

- 本地网页控制台和 API Key 访问保护。
- 安全配置状态，不返回 Client Secret 或 Refresh Token。
- LWA 与 Transfers API 连接测试。
- 支持站点支付方式查询，页面只展示类型、国家、默认标记和尾号。
- 预演模式不调用 Amazon 提现 POST。
- 每日任务、下次运行时间和后台调度器。
- SQLite 任务、历史和应用重启持久化。
- 调用 Amazon 前持久化幂等领取状态。
- 超时或 5xx 标记为 `UNKNOWN`，禁止自动重试。
- 生产提交 24 小时限制和未决请求拦截。
- 生产模式、真实 POST、站点确认和自动站点白名单安全门槛。
- Mac `launchd` 安装与卸载脚本。
- Amazon Finances API 付款组自动同步。
- 提现原币金额、换算到账金额、状态、银行尾号和追踪编号台账。
- 按币种汇总成功转账和其他状态金额。
- 财务台账 CSV 导出。
- 8 个支持站点复选、全选、批量查询和批量预演/提交。
- 全站点工作区统一显示 US、UK、CA 与 8 个 Transfers API 站点的通道、店铺或收款账户、可用资金/金额规则、可申请状态和最近结果。
- “申请所有可用站点”会先刷新 11 个站点；US/UK/CA 读取 Seller Central 的标准订单、发票支付订单和延迟交易余额，其他站点查询默认银行账户。确认后先逐账户提交紫鸟站点，再按 Amazon 限流间隔依次提交 Transfers API 站点。
- “每日任务”支持全部 11 个站点，可全选后统一设置执行时间并批量保存。US/UK/CA 到时通过紫鸟共用店铺环境依次切换站点并执行，其他站点继续使用 Transfers API。
- US/UK/CA 每次确认提交成功后，会按该站本次完成时间自动顺延 24 小时 10 分钟；提交前的登录、站点切换或页面读取失败记为可重试失败，只有最终按钮点击后的不确定结果才会锁定为 `UNKNOWN`。
- 按币种展示月度提现净额趋势和转账状态占比。

## 启动

要求 Python 3.11+，不需要第三方 Python 包。

```bash
git clone https://github.com/1111johan/payout.git
cd payout
cp .env.example .env
# 编辑 .env，至少生成 API_KEY，并按需填写 Amazon 凭证。
python3 -m amazon_payout_api.server
```

打开：

```text
http://127.0.0.1:8080
```

控制台登录使用 `.env` 中的 `API_KEY`。API Key 只保存在浏览器当前会话中。

## Mac 后台运行

安装用户级 LaunchAgent：

```bash
./scripts/install_launchd.sh
```

卸载：

```bash
./scripts/uninstall_launchd.sh
```

后台日志位于 `~/Library/Application Support/AmazonPayoutConsole/logs/`。`launchd` 会在登录后启动服务，并在服务退出后重新启动。

由于 macOS 后台进程不能直接读取 Desktop，安装脚本会把运行副本同步到 `~/Library/Application Support/AmazonPayoutConsole`。以后修改项目代码或 `.env` 后，再执行一次安装脚本即可同步；后台 SQLite 数据会保留在该运行目录中。

## 主要配置

```dotenv
SP_API_MODE=sandbox
DRY_RUN=true
ALLOW_SANDBOX_POST=false

ALLOW_PRODUCTION=false
ALLOW_PAYOUT_POST=false
AUTO_PAYOUT_MARKETPLACES=

TIMEZONE=Asia/Shanghai
SCHEDULER_ENABLED=true
SCHEDULER_POLL_SECONDS=60

FINANCE_SYNC_ENABLED=true
FINANCE_SYNC_INTERVAL_SECONDS=21600
FINANCE_SYNC_DAYS=180

ZINIAO_ENABLED=false
ZINIAO_CLIENT_PATH=
ZINIAO_VERSION=v6
ZINIAO_HOST=127.0.0.1
ZINIAO_PORT=16851
ZINIAO_COMPANY=
ZINIAO_USERNAME=
ZINIAO_PASSWORD=
ZINIAO_WEBDRIVER_PATH=
ZINIAO_AMAZON_PAGE_TIMEOUT_SECONDS=90
ZINIAO_PREPARE_TTL_SECONDS=300
ALLOW_ZINIAO_PAYOUT=false
```

`ALLOW_SANDBOX_POST` 只控制直接调用 Amazon 静态 Sandbox 的模拟 POST。日常开发保持 `DRY_RUN=true` 即可。

紫鸟控制只连接本机 WebDriver HTTP 服务。企业名、自动化用户名和密码仅从 `.env` 读取，不会通过状态接口或网页返回。首次使用前需要在紫鸟开放平台开通 WebDriver 权限，并为自动化成员授权对应店铺。

US/UK/CA 网页付款控制需要与紫鸟内核大版本匹配的 ChromeDriver 和 Selenium。控制台可通过“批量处理 US/UK/CA”自动启动已授权的对应紫鸟店铺，逐站读取标准订单、发票支付订单和延迟交易余额，并只为当前按钮可用且余额非零的账户进入确认页。提交前会逐项显示账户类型、金额和银行尾号，确认后逐账户提交；任何结果未知的请求都禁止自动重试。未出现在紫鸟自动化账号中的站点不会执行，需先在紫鸟开放平台授权给当前自动化成员。

Transfers API 没有“当前可提现余额”查询字段，因此全站点表对 BE、DE、ES、FR、IT、NL、PL、SE 显示“Amazon 提交时确定”。这些站点会在真实请求时由 Amazon 判断余额、最低付款金额和 24 小时间隔；余额不足会返回 `InsufficientPayoutAmount`，不会产生付款。

## API

| 方法 | 路径 | 用途 |
| --- | --- | --- |
| GET | `/health` | 无认证健康检查 |
| GET | `/v1/status` | 安全运行状态 |
| POST | `/v1/credentials/test` | 测试 LWA/SP-API |
| GET | `/v1/marketplaces` | 支持站点 |
| GET | `/v1/payment-methods` | 查询收款方式 |
| POST | `/v1/payouts` | 预演或受控提交 |
| GET | `/v1/schedules` | 每日任务 |
| PUT | `/v1/schedules/{marketplace}` | 保存任务 |
| DELETE | `/v1/schedules/{marketplace}` | 关闭任务 |
| GET | `/v1/payouts/history` | 执行历史 |
| POST | `/v1/finance/sync` | 从 Amazon 同步付款组 |
| GET | `/v1/finance/summary` | 按币种汇总金额和状态 |
| GET | `/v1/finance/records` | 提现台账明细 |
| GET | `/v1/finance/export.csv` | 导出 CSV |
| GET | `/v1/ziniao/status` | 紫鸟本地配置和控制端状态 |
| GET | `/v1/ziniao/stores` | 自动化账号可控制的店铺 |
| GET | `/v1/ziniao/running` | 当前运行中的店铺环境 |
| POST | `/v1/ziniao/client/start` | 启动紫鸟 WebDriver 控制端 |
| POST | `/v1/ziniao/client/exit` | 退出紫鸟控制端 |
| POST | `/v1/ziniao/core/update` | 检查或更新浏览器内核 |
| POST | `/v1/ziniao/stores/start` | 启动指定店铺环境 |
| POST | `/v1/ziniao/stores/stop` | 停止指定店铺环境 |
| GET | `/v1/ziniao/amazon/balance` | 读取 US/UK/CA Seller Central 可用资金 |
| POST | `/v1/ziniao/amazon/prepare` | 进入 Amazon 付款确认页并读取金额和账户尾号 |
| POST | `/v1/ziniao/amazon/submit` | 使用一次性确认令牌提交真实付款请求 |

除 `/health` 和网页资源外，接口必须提供 `X-API-Key`。

## 生产上线步骤

代码不能代替以下 Amazon 账户操作：

1. 在 Amazon 开发者资料中申请并获批 `Finance and Accounting` 或 `Payment Initiation Service Provider` 角色。
2. 在 SP-API 应用注册页面为应用启用该角色。
3. 在卖家账户中重新完成应用自授权或 OAuth 授权，取得新的生产 Refresh Token。
4. 把生产 LWA Client ID、Client Secret 和 Refresh Token 写入 `.env` 的 `PRODUCTION_LWA_*` 字段。
5. 设置 `SP_API_MODE=production`，但保持 `DRY_RUN=true` 和所有真实开关关闭。
6. 在控制台完成连接测试和支持站点的支付方式只读查询。
7. 在 Seller Central 人工核对每个站点的默认收款银行账户。
8. 连续运行至少 7 天预演任务，确认每天每站只有一条记录。
9. 首次人工真实测试时设置 `DRY_RUN=false`、`ALLOW_PRODUCTION=true`、`ALLOW_PAYOUT_POST=true`，但保持 `AUTO_PAYOUT_MARKETPLACES` 为空。
10. 人工提交一个站点并核对 Seller Central 状态和实际入账。
11. 只将一个已验证站点加入 `AUTO_PAYOUT_MARKETPLACES`，例如 `AUTO_PAYOUT_MARKETPLACES=DE`。

如果生产 Transfers API 返回 `403 Unauthorized`，请重新确认应用角色已经获批并启用，然后让卖家账户重新授权应用，取得与该生产应用匹配的 Refresh Token。

## 紧急关闭

任意一项操作都可以阻止后续真实提交：

- 设置 `DRY_RUN=true`。
- 设置 `ALLOW_PAYOUT_POST=false`。
- 清空 `AUTO_PAYOUT_MARKETPLACES`。
- 运行 `./scripts/uninstall_launchd.sh`。
- 在 Seller Central 撤销应用授权。

如果历史中出现 `UNKNOWN`，不要更换幂等键重试。先在 Seller Central 使用记录中的时间和参考信息核对是否已经创建付款。

## 测试

```bash
python3 -m unittest discover -s tests -v
```

自动测试使用本地假客户端，不会连接 Amazon 或发起付款。

## 财务台账说明

财务汇总来自 Amazon Finances API `listFinancialEventGroups`。金额使用十进制定点保存，不使用二进制浮点数计算。

- `OriginalTotal`：付款组原币金额。
- `ConvertedTotal`：Amazon 换算后的到账币种金额，如果 Amazon 返回该字段。
- `FundTransferStatus`：银行转账状态。
- `AccountTail`：收款账户尾号。
- `TraceId`：银行或 Amazon 追踪编号。

Amazon 官方说明最近 48 小时的财务事件可能尚未出现。后台默认每 6 小时同步最近 180 天，也可以在控制台手动同步。

Amazon 的历史财务付款组不包含 marketplace 字段。由本工具发起且成功返回 `payoutReferenceId` 的记录可以关联站点；更早的自动结算记录在“站点”列显示 `-`。

此模块是提现与结算台账，不是完整的利润表、资产负债表、税务申报或订单级会计系统。完整财务报表需要进一步同步 Settlement Reports、订单收入、费用、退款和税费。

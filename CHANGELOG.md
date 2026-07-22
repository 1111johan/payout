# Changelog

## [1.0.0] - 2026-07-22

首个完整的本地生产工作站版本。

### 主要功能

- 提供本地网页工作站，统一展示 11 个 Amazon 站点的付款通道、收款账户、可用状态、执行历史和财务台账。
- BE、DE、ES、FR、IT、NL、PL、SE 通过 Amazon Transfers API 提交 Standard Orders 按需付款。
- US、UK、CA 通过紫鸟 WebDriver 控制 Seller Central，支持标准订单、发票支付订单和延迟交易。
- 紫鸟通道逐账户读取余额，只处理付款按钮可用且余额非零的账户，并在最终提交前显示账户类型、金额和收款账户尾号。
- 支持单站付款、US/UK/CA 批量付款、全部可用站点一键处理和 11 站点每日定时任务。
- US、UK、CA 成功提交后，下一次执行时间按最近一次成功时间顺延至少 24 小时 10 分钟。
- 支持 Amazon Finances API 台账同步、按币种汇总、状态统计和 CSV 导出。

### 生产安全

- 真实提交需要生产模式、生产开关、付款开关、站点白名单和匹配确认头同时满足。
- 每个站点和账户类型使用独立幂等记录；同一定时批次不会因轮询重复执行。
- 最终付款按钮点击后的不确定结果记录为 `UNKNOWN`，禁止自动重试。
- 登录、站点切换、页面读取等提交前失败记录为 `FAILED`，不误报为已提交。
- API 和状态页面不返回 Client Secret、Refresh Token、紫鸟企业密码或 Amazon 登录凭证。
- 默认只监听 `127.0.0.1`；本机访问可直接使用工作站，远程 API 仍要求 API Key。

### 覆盖范围与限制

- Amazon Transfers API 当前只用于 BE、DE、ES、FR、IT、NL、PL、SE 的 `Standard Orders`；这些站点的发票支付订单和延迟交易未通过 API 开放。
- US、UK、CA 不支持 Transfers API 主动付款，本版本使用紫鸟 Seller Central 自动化完成。
- Transfers API 不提供当前可提现余额，也不接受指定金额；实际付款金额和最低额度由 Amazon 决定。
- 紫鸟通道依赖本机紫鸟控制端、已授权店铺、可用调试端口以及与浏览器内核匹配的 ChromeDriver。

### 验证情况

- Python 关键业务测试通过，覆盖多账户读取、发票账户索引保持、零余额延迟账户跳过、定时逐账户执行和批次幂等。
- Python 编译检查、JavaScript 语法检查和本地生产工作站浏览器只读检查通过。
- Windows 上部分旧测试在断言通过后可能因 SQLite 临时文件仍被系统占用而报告 `WinError 32` 清理错误；不影响生产数据库和业务结果。

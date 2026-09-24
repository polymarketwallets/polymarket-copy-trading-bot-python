# Polymarket 跟单机器人（Python）

[![PyPI](https://img.shields.io/pypi/v/pmwallets-copytrade.svg)](https://pypi.org/project/pmwallets-copytrade/) [![License: MIT](https://img.shields.io/badge/License-MIT-green.svg)](LICENSE)

基于 [PMWallets](https://pmwallets.com/zh/copy-trading) 的**开箱即用 Polymarket 跟单机器人**：跟随你订阅的交易者，
用你自己的账户在 Polymarket CLOB 上按你设定的限制跟单。

[`pmwallets-copytrade` on PyPI](https://pypi.org/project/pmwallets-copytrade/) · [English](README.md) · Node.js 版：[polymarket-copy-trading-bot](https://github.com/polymarketwallets/polymarket-copy-trading-bot) ·
SDK：[pmwallets-python](https://github.com/polymarketwallets/pmwallets-python)

## 它做什么

1. **你来挑交易者**：在 [pmwallets.com](https://pmwallets.com/zh) 按胜率置信区间下限、去掉最赚一场后的盈利、
   活跃度与风格筛选，然后订阅。
2. **机器人接收他们的每一笔成交**：走 PMWallets 的 WebSocket，通常在出块后一秒内到达
   （[实测延迟](https://pmwallets.com/zh/latency)）。漏掉的帧通过 `session`/`seq` 发现，并从最后处理的那笔
   开始补发 —— 机器人停机期间发生的也会补上。
3. **在 Polymarket 上用你自己的账户跟单**，严格限制在你设定的范围内。私钥只在你本机，PMWallets 看不到，
   也从不代你下单。

## 快速开始

```bash
# Python ≥ 3.10
pip install pmwallets-copytrade
pmwallets-copytrade init            # 生成 config.yaml
export PMW_API_KEY=pmw_...          # 在 https://pmwallets.com/keys 创建
pmwallets-copytrade run             # 默认 dry-run：记录每个决策，不下任何单
```

dry-run 日志确认没问题后再切到实盘：

```yaml
mode: live
polymarket:
  privateKey: ${POLY_PRIVATE_KEY}        # 用来签名订单
  signatureType: 3                       # 见「为机器人准备一个 Polymarket 账户」
  funderAddress: ${POLY_FUNDER_ADDRESS}  # 你的 Polymarket 主页地址（USDC 在这里）
```

`pmwallets-copytrade status` 显示持仓、今日花费和仍在确认中的订单。如果机器人自己无法确认某笔订单是否成交，
它会保留这笔订单的额度预留（后续买入不会因此突破你的上限），并请你到 polymarket.com 核对后，在机器人停止时运行
`pmwallets-copytrade reconcile <key> --none` 或 `--filled <股数> --usdc <金额>`。每一个决策（包括每次跳过及原因）都追加写入
`pmw-data/decisions.<mode>.jsonl`。

## 决策规则

这些规则来自一个在 Polymarket 上实盘跑过的跟单机器人，每一条都对应一次丢单或一次被拒的订单。

**买入**（交易者买了）
- 跳过：超过 `maxFillAgeSec` 的旧成交（停机后的补发不能拿历史去买入）、低于 `minTargetNotionalUsdc` 的小单、
  你排除掉的角色（挂单/吃单）；
- 每笔交易者交易只跟一次；每个（交易者，结果）一个仓位，最多加仓 `maxBuysPerOutcome` 次；另有
  `maxOpenPositionsPerTarget` / `maxOpenPositions` / `maxDailySpendUsdc` 上限；
- 市场必须开放、接受订单，且不会在 `minSecondsToEndDate` 内结算；
- 最优卖价必须在 `[minPrice, maxPrice]` 内、比交易者成交价高出不超过 `maxSlippage`，盘口深度至少
  `minBookDepthUsdc`；
- 下单：在最优卖价按**固定股数 FOK**（`orderSizeUsdc` / 卖价），按 CLOB 的精度规则取整。故意按股数而不是按
  USDC 预算下单 —— 按预算的市价单会超额成交。

**卖出**（交易者卖了）
- `sellMode: all`：卖掉跟着**这位**交易者在该结果上买入的全部仓位。记在其他交易者名下的同一结果绝不动：
  最多卖「账户持有量 − 其他交易者的账面仓位」；如果算下来为零，就停下来提示你对账；
- 退出指令会落盘，按退避重试（重启后继续），直到仓位清掉。新鲜度限制不适用于卖出：交易者在机器人停机时
  退出了，机器人照样要退；
- 下单：在最优买价 **FAK**（能成交多少算多少）—— 部分退出好过完全退不出，剩下的继续重试。

**安全**
- **默认 dry-run**，实盘必须显式写 `mode: live`；
- **最多执行一次**：一笔成交在发出任何订单**之前**就写进状态文件标记为已决策，崩溃最多漏跟一次，绝不会重复下单。
  一个数据目录只能跑一个机器人，第二个实例会拒绝启动；
- **不漏记成交**：每笔订单发出前先落盘为「待确认」。交易所回报「被取消」的（这类回报曾带着真实的部分成交），
  会按订单号退避反复查询，直到确认成交或确认没有成交 —— 重启后也会继续。根本没收到回报的订单没有订单号可查：
  如果出现形状相符的成交，机器人不会去猜它属于谁（可能是你手动下的，也可能是跟另一位交易者的单），而是交给你
  `reconcile`；
- 每 10 分钟清理一次已结算的市场，让它们不再占用仓位上限。

**推送从哪里来。** PMWallets 的服务器在英国。把机器人部署在英国或欧洲其他地区，收到成交最快；实测延迟公布在
https://pmwallets.com/zh/latency。

**每个账户只有一条推送流，机器人优先。** PMWallets 每个账户只保留一条 WebSocket。用 API key 建立的连接（机器人）
优先：pmwallets.com 上的实时推送页永远不会抢走机器人的流 —— 机器人在线时，推送页会提示并且不连接。同一个账户上的
两个机器人之间仍是后连的顶掉先连的，所以一个账户只跑一个机器人。

## 配置

`pmwallets-copytrade init` 会生成带注释的 `config.yaml`。文件里的 `${NAME}` 会被替换成环境变量 `NAME` 的值；
变量不存在时机器人拒绝启动。非法值在启动时就会报错，不会被悄悄忽略。

### 为机器人准备一个 Polymarket 账户（推荐做法）

给机器人**单独开一个账户**，里面只放你愿意让它去交易的钱。这是我们推荐并写成文档的做法，照着一步步来：

1. **新建一个钱包账户**：在 MetaMask（或 Rabby）里点账户菜单 → *添加账户*。这个账户只用来干这一件事。
2. **在 [polymarket.com](https://polymarket.com) 用这个钱包注册**：在登录弹窗里选择 MetaMask 连接。
   2026-05-04 之后在 polymarket.com 创建的账户都是 **Deposit Wallet**，也就是 `signatureType: 3`。
3. **充一小笔钱**：在 polymarket.com 点 *Deposit*。polymarket.com 上显示的余额就是机器人能用的余额。
4. **记下两个值：**
   - `funderAddress` —— polymarket.com **个人资料菜单**里显示的账户钱包地址（不是 MetaMask 地址）；
   - `privateKey` —— 第 1 步那个 MetaMask 账户的私钥（MetaMask → *账户详情* → *显示私钥*）。
     放在环境变量里，不要写进 `config.yaml`。
5. **填配置**：`signatureType: 3`、`privateKey: ${POLY_PRIVATE_KEY}`、`funderAddress: ${POLY_FUNDER_ADDRESS}`，然后运行
   **`pmwallets-copytrade check`**。它不会下单；必须显示出你的余额，并提示 *account may open positions*。
6. 先用 `mode: dry-run` 跑，决策日志看着没问题了，再改成 `mode: live`，`copy.orderSizeUsdc` 先设小一点。

为什么要单独开账户：机器人拿着的私钥能动这个账户里的所有钱，跟单不应该有机会碰到你没打算拿来冒险的资金。私钥只在你本机。

### 已经有账户？判断它是哪种类型（`polymarket.signatureType`）

类型决定用哪把私钥签名、钱放在哪个地址（[Polymarket 官方说明](https://docs.polymarket.com/trading/wallets-auth)）。
这一项**没有默认值** —— 不填机器人就拒绝实盘，因为按错误的类型签名，所有订单都会被拒。

| `signatureType` | 你的账户属于这种，如果…… | `privateKey` | `funderAddress` |
|---|---|---|---|
| **3** · Deposit Wallet | 是 **2026-05-04 当天或之后**在 polymarket.com 创建的（任何注册方式） | 你登录用的那个钱包的私钥 | 个人资料菜单里的账户钱包地址 |
| **2** · Safe Wallet | 是 **2026-05-04 之前**用 MetaMask、Rabby 等浏览器钱包连接注册的 | 这个浏览器钱包的私钥 | 个人资料菜单里的账户钱包地址（不是 MetaMask 地址） |
| **1** · Proxy Wallet | 是 **2026-05-04 之前**用邮箱或 Google（Magic）注册的 | polymarket.com 允许你导出的 Magic 私钥（在设置里找 *Export Private Key*） | 个人资料菜单里的账户钱包地址 |
| **0** · 普通钱包 | 你直接用自己的地址交易，钱就在这个地址上，不经过 polymarket.com 账户 | 这个地址的私钥 | 同一个地址（可不填） |

普通钱包（类型 0）在第一次交易前，还要自己在链上授权 Polymarket 的交易所合约 —— 机器人不会替你做（参考
Polymarket 的[授权示例](https://github.com/Polymarket/py-clob-client-v2/blob/main/examples/account/approve_allowances.py)）。
在 polymarket.com 上创建的账户（类型 1–3）由 Polymarket 设置好。

不管哪种类型，改成 `mode: live` 之前都先运行 `pmwallets-copytrade check`。如果 polymarket.com 上有钱、`check` 却显示余额
$0，说明类型或地址填错了。Polymarket 还限制部分地区交易；账户只能平仓时，`check` 会提示。

### 全部配置项

| 配置项 | 默认值 | 作用 |
|---|---|---|
| `mode` | `dry-run` | `dry-run` 只记录每个决策，按盘口最优价模拟成交；`live` 真实下单。 |
| `pmwallets.apiKey` | —（必填） | PMWallets API key（`pmw_…`，在 [pmwallets.com/keys](https://pmwallets.com/keys) 创建）。 |
| `pmwallets.baseUrl` | `https://api.pmwallets.com` | PMWallets API 地址。 |
| `polymarket.signatureType` | — | 账户类型 0–3，见上文。没有默认值：`mode: live` 和 `check` 必填。 |
| `polymarket.privateKey` | — | 签名订单用的私钥。实盘必填。 |
| `polymarket.funderAddress` | — | 存放资金的地址。实盘必填（`signatureType` 为 0 时可不填）。 |
| `polymarket.apiKey` / `apiSecret` / `apiPassphrase` | 自动派生 | Polymarket CLOB API 凭据；不填则启动时由 `privateKey` 派生。 |
| `polymarket.clobUrl` | `https://clob.polymarket.com` | Polymarket CLOB 地址。 |
| `targets` | `[]` | 要跟的实体（0x 地址；拥有地址后也可以写榜单代号）。留空 = 跟你账户订阅的所有实体。 |
| `targets[].orderSizeUsdc` | 同 `copy.orderSizeUsdc` | 单个目标的下单金额。 |
| `targets[].maxBuysPerOutcome` | 同 `copy.maxBuysPerOutcome` | 单个目标在同一结果上的最多买入次数。 |
| `copy.orderSizeUsdc` | `10` | 每次跟买花多少 USDC，按最优卖价折算成股数，并向下取整到 CLOB 接受的数量。 |
| `copy.roles` | `[taker, maker]` | 跟目标的吃单成交、挂单成交，或两者都跟。吃单更好跟。 |
| `copy.maxBuysPerOutcome` | `3` | 同一结果最多跟买几次（分批加仓），之后只持有。 |
| `copy.maxOpenPositions` | `20` | 所有目标合计同时持有的结果数（待确认的买单也算）。 |
| `copy.maxOpenPositionsPerTarget` | `5` | 每个目标同时持有的结果数。 |
| `copy.maxFillAgeSec` | `60` | 目标的买入到达机器人时超过这个秒数就不跟（停机后补发的旧成交不能拿来买入）。卖出不受此限制。 |
| `copy.minTargetNotionalUsdc` | `25` | 目标买入金额低于它就忽略。 |
| `copy.minPrice` / `copy.maxPrice` | `0.05` / `0.95` | 最优卖价在这个区间内才买。 |
| `copy.maxSlippage` | `0.03` | 最优卖价比目标成交价高出超过它就跳过（0.03 = 3 美分）。 |
| `copy.minBookDepthUsdc` | `50` | 我们要吃的那一侧盘口不足这么多 USDC 就跳过。 |
| `copy.minSecondsToEndDate` | `600` | 离结算不足这么多秒的市场不买。 |
| `copy.maxSecondsToEndDate` | `0` | 离结算超过这么多秒的市场不买；`0` = 不限。 |
| `copy.sellMode` | `all` | `all`：目标卖出时，卖掉跟随该目标在该结果上买入的仓位。`none`：持有到结算。 |
| `risk.maxDailySpendUsdc` | `200` | 每个 UTC 日跟买的总金额上限（含待确认订单）；`0` = 不限。 |
| `dataDir` | `./pmw-data` | 状态、推送游标、锁文件和决策日志的存放目录。 |

### 命令、环境变量与文件

| | |
|---|---|
| `pmwallets-copytrade init [file]` | 生成示例配置。 |
| `pmwallets-copytrade run [--config file] [--json]` | 启动机器人（`--json`：每个事件一行 JSON 日志）。 |
| `pmwallets-copytrade check [--config file]` | 不下单地检查实盘准备：key、订阅、账户类型、余额、限制。 |
| `pmwallets-copytrade status [--config file]` | 持仓、今日花费、待确认订单、正在重试的退出。 |
| `pmwallets-copytrade reconcile [<key> --none \| <key> --filled <股数> --usdc <金额>]` | 人工确认机器人自己无法核实的订单。需在机器人停止时运行。 |
| `HTTPS_PROXY` / `HTTP_PROXY` / `NO_PROXY` | PMWallets API、WebSocket 和 Polymarket CLOB 都会走代理。 |
| `<dataDir>/state.<mode>.json` | 持仓、已决策的成交、待确认订单、待退出任务、今日花费。 |
| `<dataDir>/stream.<mode>.json` | 推送流重启后从哪里继续。 |
| `<dataDir>/decisions.<mode>.jsonl` | 每一个决策，包括每次跳过及原因。 |
| `<dataDir>/lock.<mode>` | 同一数据目录、同一模式只允许一个机器人。 |

## 开发

```bash
pip install -e '.[dev]'   # or: uv pip install -e . pytest pytest-asyncio
pytest
```

`config.yaml`、状态文件和锁文件与 Node.js 版通用。`testdata/` 是两个实现都必须通过的契约用例 —— 两个仓库里保持一致。

## 相关链接

- [Polymarket 聪明钱排行榜](https://pmwallets.com/zh) —— 从 Polygon 链上计算的 Polymarket 盈利交易者，胜率带置信区间
- [Polymarket 跟单指南](https://pmwallets.com/zh/copy-trading) —— 哪些钱包值得跟，以及怎样及时拿到他们的成交
- [怎样向 Polymarket 聪明钱学习](https://pmwallets.com/zh/learn) —— 读懂一份战绩：置信区间、挂单与吃单、擅长的市场
- [PMWallets API 文档](https://pmwallets.com/zh/docs) —— WebSocket 与 Webhook 成交推送、补发接口、交易历史导出
- [成交推送实测延迟](https://pmwallets.com/zh/latency) —— 出块到推送的 p50 / p95，实时公布
- [追踪 Polymarket 钱包的几种做法对比](https://pmwallets.com/zh/compare) —— 官方榜单、免费追踪器、SQL 看板
- [常见问题](https://pmwallets.com/zh/faq) · [English site](https://pmwallets.com)

## 代理

PMWallets API、WebSocket 与 Polymarket CLOB 都会读取 `HTTPS_PROXY` / `HTTP_PROXY` / `NO_PROXY`。

## 免责声明

`mode: live` 时本软件会用真金白银下真实订单。跟出去的单不是交易者的那一单：价格可能已经变了、有手续费、
流动性是共享的。过往结果不预示未来。PMWallets 提供的是交易者做了什么的数据，不是投资建议。风险自负；
许可证见 [LICENSE](LICENSE)（MIT）。

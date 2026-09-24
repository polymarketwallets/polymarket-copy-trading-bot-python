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
  signatureType: 2                       # 0 普通钱包 · 1 邮箱/Magic 登录 · 2 浏览器钱包登录
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

**每个账户只有一条推送流。** PMWallets 每个账户只允许一条 WebSocket，后连上的会顶掉先连的 —— 所以机器人
和 pmwallets.com 上的实时推送页（或者第二个机器人）会互相踢。一个账户只跑一个消费者。

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

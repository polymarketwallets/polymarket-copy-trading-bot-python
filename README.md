# Polymarket copy-trading bot (Python)

[![PyPI](https://img.shields.io/pypi/v/pmwallets-copytrade.svg)](https://pypi.org/project/pmwallets-copytrade/) [![License: MIT](https://img.shields.io/badge/License-MIT-green.svg)](LICENSE)

A **ready-to-run Polymarket copy-trading bot** built on [PMWallets](https://pmwallets.com/copy-trading): it follows the
traders you subscribe to and mirrors their fills on the Polymarket CLOB with your own account, inside the limits you set.

[`pmwallets-copytrade` on PyPI](https://pypi.org/project/pmwallets-copytrade/) · [中文说明](README.zh.md) · Node.js version: [polymarket-copy-trading-bot](https://github.com/polymarketwallets/polymarket-copy-trading-bot) ·
SDK: [pmwallets-python](https://github.com/polymarketwallets/pmwallets-python)

## What the bot does

1. **You pick the traders** on [pmwallets.com](https://pmwallets.com): filter the board by the lower bound of the
   win-rate confidence interval, profit without the single best market, activity and style, then subscribe.
2. **The bot receives every fill** of those traders over the PMWallets WebSocket, typically within a second of the
   block ([measured latency](https://pmwallets.com/latency)). Missed frames are detected (`session`/`seq`) and
   replayed from the last fill handled, including everything that happened while the bot was down.
3. **It mirrors them on Polymarket** with your own account, inside the limits you set. Your keys never leave your
   machine; PMWallets never sees them and never places orders.

## Quick start

```bash
# Python ≥ 3.10
pip install pmwallets-copytrade
pmwallets-copytrade init            # writes config.yaml
export PMW_API_KEY=pmw_...          # https://pmwallets.com/keys
pmwallets-copytrade run             # dry-run: logs every decision, trades nothing
```

When the dry-run log looks right, switch to live:

```yaml
mode: live
polymarket:
  privateKey: ${POLY_PRIVATE_KEY}        # signs your orders
  signatureType: 2                       # 2 browser-wallet sign-up · 1 email/Google sign-up · 0 own wallet — see Configuration
  funderAddress: ${POLY_FUNDER_ADDRESS}  # your Polymarket profile address (holds the USDC)
```

`pmwallets-copytrade status` prints open positions, today's spend and any order still being confirmed. If the bot
cannot establish by itself whether an order filled, it keeps the order's reservation (no further BUY can exceed
your limits because of it) and asks you: check polymarket.com and run
`pmwallets-copytrade reconcile <key> --none` or `--filled <shares> --usdc <usdc>` with the bot stopped. Every decision — including each skip and its
reason — is appended to `pmw-data/decisions.<mode>.jsonl`.

## How it decides

The rules come from a copy-trading bot that ran live on Polymarket; each one is there because of a lost trade or a
rejected order.

**BUY** (the trader bought)
- skip fills older than `maxFillAgeSec` (a replay after downtime must not buy history), below
  `minTargetNotionalUsdc`, or from a role you excluded;
- one copy per trader transaction, one position per (trader, outcome), at most `maxBuysPerOutcome` buys into it
  (DCA), `maxOpenPositionsPerTarget` / `maxOpenPositions` / `maxDailySpendUsdc` caps;
- the market must be open, accepting orders and not settle within `minSecondsToEndDate`;
- the best ask must sit in `[minPrice, maxPrice]`, be at most `maxSlippage` above what the trader paid, and the
  book must hold `minBookDepthUsdc`;
- order: **fill-or-kill for an exact number of shares** at the best ask (`orderSizeUsdc` / ask), rounded so the
  CLOB accepts it. Share-denominated on purpose — a USDC-budget market order can overfill.

**SELL** (the trader sold)
- `sellMode: all` exits everything the bot bought following **that** trader in that outcome. Shares booked to
  another trader in the same outcome are never sold: the most it sells is your token balance minus what the other
  traders hold. If that comes to nothing, it stops and asks you to reconcile;
- the exit is saved and retried (with backoff, across restarts) until the position is gone. Freshness does not
  apply: if the trader left while the bot was down, it still gets out;
- order: **fill-and-kill** at the best bid — a partial exit beats none, and the rest is retried.

**Safety**
- **dry-run by default**; live needs an explicit `mode: live`;
- **at most once**: a fill is marked decided in the state file *before* any order is sent, so a crash can miss a
  copy but never place it twice. One bot per data directory: a second instance refuses to start;
- **no lost fills**: every order is saved as *pending* before it is sent. An order the exchange reports as killed
  (such replies have carried real partial fills) is looked up again by its order id with backoff until its fill —
  or its absence — is certain, even across restarts. One that got no answer at all has no id to look up: if a
  matching trade appears, the bot does not guess whose it is (it could be yours by hand, or another trader's copy)
  and hands it to you to `reconcile`;
- resolved markets are swept every 10 minutes so they stop counting against the caps.

**One stream per account.** PMWallets allows one WebSocket per account and the newest connection wins, so the bot
and the live-feed page on pmwallets.com (or a second bot) will take the stream from each other. Run one consumer
per account.

## Configuration

`pmwallets-copytrade init` writes a commented `config.yaml`. `${NAME}` in the file is replaced by the environment
variable `NAME`; a missing variable stops the bot at start-up. Invalid values are rejected at start-up, never
silently ignored.

### Which Polymarket account you have (`polymarket.signatureType`)

This decides which private key signs your orders and which address holds your money:

| `signatureType` | How the account was created | `privateKey` | `funderAddress` |
|---|---|---|---|
| **2** (default) | Signed up on polymarket.com **by connecting a browser wallet** (MetaMask, Coinbase Wallet, Rabby, WalletConnect…). Polymarket created a Safe wallet owned by it. | the browser wallet's key (MetaMask: *Account details → Show private key*) | your **Polymarket profile address** (the Safe) — *not* your MetaMask address |
| **1** | Signed up on polymarket.com **with email or Google** (a Magic wallet). Polymarket created a proxy wallet. | the key polymarket.com lets you export (*Settings → Export Private Key*) | your **Polymarket profile address** (the proxy) |
| **0** | You trade from **a plain wallet you control yourself**, and that address holds the funds. | its key | the same address (may be omitted) |

If unsure: `funderAddress` is the address polymarket.com shows on your profile; the key is the wallet you log in
with. Use an account funded only with what you are willing to let the bot trade, and keep the key in an
environment variable rather than in `config.yaml`. The key never leaves your machine.

### All options

| Key | Default | What it does |
|---|---|---|
| `mode` | `dry-run` | `dry-run` logs every decision and simulates fills at the best price on the book; `live` sends real orders. |
| `pmwallets.apiKey` | — (required) | Your PMWallets API key (`pmw_…`, from [pmwallets.com/keys](https://pmwallets.com/keys)). |
| `pmwallets.baseUrl` | `https://api.pmwallets.com` | PMWallets API endpoint. |
| `polymarket.signatureType` | `2` | Account type, see the table above. |
| `polymarket.privateKey` | — | Key that signs your orders. Required in live mode. |
| `polymarket.funderAddress` | — | Address that holds your funds. Required in live mode unless `signatureType` is 0. |
| `polymarket.apiKey` / `apiSecret` / `apiPassphrase` | derived | Polymarket CLOB API credentials; derived from `privateKey` at start-up when absent. |
| `polymarket.clobUrl` | `https://clob.polymarket.com` | Polymarket CLOB endpoint. |
| `targets` | `[]` | Entities to copy (0x address, or board handle once you own the address). Empty = every entity your account subscribes to. |
| `targets[].orderSizeUsdc` | `copy.orderSizeUsdc` | Per-target order size. |
| `targets[].maxBuysPerOutcome` | `copy.maxBuysPerOutcome` | Per-target cap on BUYs into one outcome. |
| `copy.orderSizeUsdc` | `10` | USDC spent on each copied BUY, converted to shares at the best ask and rounded down to a size the CLOB accepts. |
| `copy.roles` | `[taker, maker]` | Copy the target's taker fills, maker fills, or both. Taker fills are easier to follow. |
| `copy.maxBuysPerOutcome` | `3` | Copy up to this many BUYs into the same outcome (averaging in), then only hold. |
| `copy.maxOpenPositions` | `20` | Outcomes held at once across all targets (unconfirmed BUYs count). |
| `copy.maxOpenPositionsPerTarget` | `5` | Outcomes held at once per target. |
| `copy.maxFillAgeSec` | `60` | A target BUY older than this when it reaches the bot is not copied (a replay after downtime must not buy history). SELLs are never too old. |
| `copy.minTargetNotionalUsdc` | `25` | Ignore target BUYs smaller than this. |
| `copy.minPrice` / `copy.maxPrice` | `0.05` / `0.95` | Only BUY when the best ask is inside this band. |
| `copy.maxSlippage` | `0.03` | Skip if the best ask is more than this above the target's price (0.03 = 3 cents). |
| `copy.minBookDepthUsdc` | `50` | Skip if the side we take holds less than this much USDC. |
| `copy.minSecondsToEndDate` | `600` | Don't BUY a market that settles sooner than this. |
| `copy.maxSecondsToEndDate` | `0` | Don't BUY a market that settles later than this; `0` = no limit. |
| `copy.sellMode` | `all` | `all`: when the target sells, exit what the bot bought following that target in that outcome. `none`: hold to settlement. |
| `risk.maxDailySpendUsdc` | `200` | Total USDC of copied BUYs per UTC day, unconfirmed orders included; `0` = no limit. |
| `dataDir` | `./pmw-data` | Where the state, stream cursor, lock file and decision log live. |

### Commands, environment and files

| | |
|---|---|
| `pmwallets-copytrade init [file]` | Write the example config. |
| `pmwallets-copytrade run [--config file] [--json]` | Start the bot (`--json`: one JSON log line per event). |
| `pmwallets-copytrade status [--config file]` | Open positions, today's spend, orders still being confirmed, exits being retried. |
| `pmwallets-copytrade reconcile [<key> --none \| <key> --filled <shares> --usdc <usdc>]` | Settle an order the bot could not verify by itself. Run with the bot stopped. |
| `HTTPS_PROXY` / `HTTP_PROXY` / `NO_PROXY` | Honoured for the PMWallets API, the WebSocket and the Polymarket CLOB. |
| `<dataDir>/state.<mode>.json` | Positions, decided fills, orders being confirmed, pending exits, today's spend. |
| `<dataDir>/stream.<mode>.json` | Where the fill stream resumes after a restart. |
| `<dataDir>/decisions.<mode>.jsonl` | Every decision, including each skip and its reason. |
| `<dataDir>/lock.<mode>` | One bot per data directory and mode. |

## Development

```bash
pip install -e '.[dev]'   # or: uv pip install -e . pytest pytest-asyncio
pytest
```

The same `config.yaml`, state files and lock file work with the Node.js version. `testdata/` holds the
contract cases both implementations must pass — keep it identical in both repositories.

## Resources

- [Polymarket smart-money leaderboard](https://pmwallets.com) — profitable Polymarket traders scored from the Polygon chain, with win-rate confidence intervals
- [Polymarket copy trading guide](https://pmwallets.com/copy-trading) — which wallets are worth following and how to get their fills in time
- [How to learn from Polymarket smart money](https://pmwallets.com/learn) — reading a trader's record: confidence intervals, maker vs taker, market specialism
- [PMWallets API documentation](https://pmwallets.com/docs) — WebSocket and webhook fill push, fills replay, trade-history exports
- [Measured fill-push latency](https://pmwallets.com/latency) — block-to-push p50 / p95, published live
- [Ways to follow Polymarket wallets, compared](https://pmwallets.com/compare) — official leaderboard, free trackers, SQL dashboards
- [FAQ](https://pmwallets.com/faq) · [中文站](https://pmwallets.com/zh)

## Proxies

`HTTPS_PROXY` / `HTTP_PROXY` / `NO_PROXY` are honoured for the PMWallets API, the WebSocket and the Polymarket CLOB.

## Disclaimer

This software places real orders with real money when `mode: live`. A copied order is not the trader's order: the
price may have moved, fees apply and liquidity is shared. Past results do not predict future ones. PMWallets
provides data about what traders did, not advice. Use at your own risk; see [LICENSE](LICENSE) (MIT).

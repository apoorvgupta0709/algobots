# India Strategy Platform — Operational Report

> Paper/research only. No live broker orders are placed anywhere in this system.
> This report reads the registry and qualification config only; it makes no
> database, broker/FYERS, network, or LLM calls and changes no strategy status.

- Registry: `/opt/data/algobots-strategy-universe-20260702-075240/config/strategy_universe_india.json` (schema 1.0)
- Qualification config: `/opt/data/algobots-strategy-universe-20260702-075240/config/strategy_qualification.json`
- Total strategies: **93** — 46 executable, 47 scorecard-only

## Safety posture

- Registry `paper_only`: **True**
- Registry `live_orders_enabled`: **False**
- Strategies not paper-only: **0** (must be 0)
- Executable option-selling / short-premium strategies: **0** (must be 0)
- Strategies pre-labelled live-eligible in the file: **0** (must be 0; that label is manual-only)

## Desks

| Desk | Total | Executable | Scorecard-only |
| --- | ---: | ---: | ---: |
| Options Desk | 31 | 12 | 19 |
| Equities Desk | 32 | 27 | 5 |
| Investment Desk | 27 | 7 | 20 |
| Futures Desk | 3 | 0 | 3 |

## Lifecycle funnel

Backtest → paper → qualified progression. The terminal `live_eligible_requires_manual_approval` status is a governance label only and is never set by the file or by automation.

| Lifecycle status | Count |
| --- | ---: |
| research_candidate | 49 |
| backtest_ready | 27 |
| backtested | 9 |
| paper_enabled | 3 |
| paper_observing | 4 |
| qualified | 1 |
| live_eligible_requires_manual_approval | 0 |

## Aggregate risk envelope (executable strategies)

Per-strategy rupee risk caps (min–max across all executable strategies). Every executable strategy defines its own caps; none may enable live orders.

| Risk cap | Range |
| --- | --- |
| Paper capital | ₹50000.00 |
| Max loss / trade | ₹0.00 – ₹2500.00 |
| Max daily loss | ₹0.00 – ₹5000.00 |
| Max premium / exposure | ₹40000.00 – ₹50000.00 |
| Max trades / day | 1 – 3 |
| Max open positions | 1 – 5 |

## One-month paper-trial requirements

A strategy must run on paper for **1 calendar month** and clear every threshold below before the engine will *recommend* advancing it to `qualified`:

| Criterion | Threshold |
| --- | --- |
| Min closed trades | 15 |
| Min trading days | 10 |
| Min win rate | 40.00% |
| Min profit factor | 1.20 |
| Min net P&L | ₹0.00 |
| Max drawdown | ₹6000.00 |
| Min expectancy / trade | ₹0.00 |

## Manual live-approval gate

A passing one-month trial only *recommends* `qualified`. The single path to the `live_eligible_requires_manual_approval` label requires **all** of:

1. the strategy is executable (scorecard-only strategies can never be made live-eligible),
2. its current status is `qualified`,
3. a named human approver,
4. the exact confirmation phrase `APPROVE LIVE ELIGIBILITY <strategy_id>`, and
5. an explicit acknowledgement flag.

Even when granted it returns a governance record only — **no order-placement, modification, cancellation, or exit code is enabled anywhere in this repository.**


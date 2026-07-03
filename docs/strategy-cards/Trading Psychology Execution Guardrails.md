# Strategy: Trading Psychology Execution Guardrails

Status: `idea`  
Source type: book / execution-risk module  
No live execution permitted from this note.

## Source

- Book/report/source: Trading in the Zone; Best Loser Wins pending; The Disciplined Trader pending
- Page/chapter/link: Local extracted Trading in the Zone source + pending legal uploads
- Extracted idea date: 2026-06-09

## Core idea

The options bot must enforce discipline mechanically because most losses come from overriding stops, revenge trading, oversized positions, and trading unclear setups.

## Market / universe

- Market: NIFTY / BANKNIFTY options
- Universe: all paper/live candidates
- Timeframe: applies to every trade

## Setup rules

- Every trade must have pre-defined entry reason, stop, invalidation, target/trailing plan, and max loss.
- No trade if the signal is not classifiable into an approved strategy card.
- No trade after daily max-loss breach.

## Entry rules

- Bot must log: strategy card, reason, risk amount, stop logic, expected scenario, invalidation.
- Manual live mode must require Apoorv’s exact confirmation before any real order.

## Exit rules

- Stop exits are mandatory; no manual or automated “hope hold”.
- After 2 consecutive losses, reduce aggression or pause depending on config.
- After daily loss cap, block new entries.

## Risk controls

- Max risk per idea: ₹1,500 paper loss per trade; ₹5,000 daily paper loss cap; 4 trades/day.
- Stop/invalidations: breach of pre-defined stop or strategy thesis.
- Avoid conditions: revenge trades, unclassified signals, chasing after missed move, post-loss size increase.

## Data needed

- Candles: trade and P&L state
- Fundamentals: not required
- Sentiment/events: optional reason logging

## Backtest plan

- Date range: apply as overlay to all paper/backtest campaigns
- Fees/slippage: inherited from underlying strategy
- Metrics: blocked trades, prevented loss, drawdown reduction, missed winners, rule violations

## Backtest status

- Not tested

## Questions to test

- Does forced pause after losses reduce drawdown or just reduce opportunity?
- What is the best cooldown after loss cap breach?
- Which behavioral violations are most common in paper logs?

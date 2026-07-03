# Strategy: Options Greeks Risk Filter for Index Options

Status: `idea`  
Source type: book / public web reference / risk-control module  
No live execution permitted from this note.

## Source

- Book/report/source: Option Volatility and Pricing; Trading Options Greeks pending; Options Playbook public reference
- Page/chapter/link: Local extracted Natenberg source + public strategy reference
- Extracted idea date: 2026-06-09

## Core idea

Do not treat CE/PE as simple leveraged index proxies. Every proposed option trade must pass Delta, Gamma, Theta, Vega, liquidity and spread checks before paper entry.

## Market / universe

- Market: NIFTY / BANKNIFTY options
- Universe: ATM and near-ATM weekly/monthly index options
- Timeframe: intraday to same-day paper trades initially

## Setup rules

- Candidate direction must come from a separate signal card, e.g. constituent-led BankNifty direction.
- Prefer liquid ATM/near-ATM options where bid/ask spread is acceptable.
- Avoid far OTM lottery options unless a separate tested event strategy allows them.
- Avoid trades where expected holding period makes theta decay dominate the directional thesis.

## Entry rules

- Record selected strike, expiry, option type, LTP, spread, implied volatility if available, and estimated Greeks if available.
- Long CE/PE only when the option has enough delta to express the index view.
- Reject if premium-risk stop cannot fit the configured per-trade loss cap.

## Exit rules

- Primary invalidation from index structure, not hope-based option P&L.
- Secondary option premium guard exits if premium loss cap is reached before index SL.
- Exit if liquidity/spread becomes abnormal.

## Risk controls

- Max risk per idea: ₹1,500 per paper trade until changed by Apoorv.
- Stop/invalidations: index swing invalidation + option premium max-loss guard.
- Avoid conditions: wide spreads, stale quote, high event risk, unclear direction, IV crush risk after event.

## Data needed

- Candles: BankNifty/Nifty index + selected option candles/quotes
- Fundamentals: not required intraday except event calendar context
- Sentiment/events: news/reason checks for top constituent moves

## Backtest plan

- Date range: at least 6-12 months of intraday option/index replay when data is available
- Fees/slippage: include brokerage, STT, exchange fees, GST, and realistic spread/slippage
- Metrics: trades, win rate, average win/loss, max drawdown, profit factor, average premium decay, stop slippage

## Backtest status

- Not tested

## Questions to test

- Does a minimum-delta rule reduce false CE/PE entries?
- Does theta filter improve intraday long-option expectancy?
- What spread threshold makes paper results unrealistic?

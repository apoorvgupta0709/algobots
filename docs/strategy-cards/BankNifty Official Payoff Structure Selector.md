# Strategy: BankNifty Official Payoff Structure Selector

Status: `idea`  
Source type: official NSE/public PDF / option payoff catalog  
No live execution permitted from this note.

## Source

- Book/report/source: Firecrawl-saved NSE Bank Nifty Option Strategies Booklet; NSE Trading Strategies for Indian Markets; Nifty Bank Index PDF
- Page/chapter/link: `/opt/data/trading-library/books/legal_sources/firecrawl/downloaded_public_pdfs/pdf_bank_nifty_option_strategies_booklet_nse.pdf`
- Extracted idea date: 2026-06-09

## Core idea

Before the BankNifty bot chooses a CE/PE instrument, it should classify the market thesis into an appropriate payoff structure. For the first paper bot, only long-option or debit-defined-risk structures should be considered. Short-option strategies are recorded as research-only until margin, gap, and adjustment risks are modeled.

## Market / universe

- Market: BANKNIFTY options
- Universe: BankNifty index options and, later, tested defined-risk spreads
- Timeframe: intraday paper trading first; expiry/positional structures only after separate testing

## Setup rules

- Directional thesis must come from [[BankNifty Constituent-Led Directional Long Options]].
- Option must pass [[Options Greeks Risk Filter for Index Options]].
- IV/event context must pass [[Implied Volatility Regime Filter for Long Options]].
- Trade must pass [[Position Sizing / Risk Rule Card]] and [[Trading Psychology Execution Guardrails]].
- Default allowed structures for automation:
  - Long CE for confirmed bullish move.
  - Long PE for confirmed bearish move.
  - Debit spread only after separate backtest and implementation.
- Research-only structures until further approval/testing:
  - Naked short call/put.
  - Short straddle/strangle.
  - Covered/futures-combination structures.
  - Ratio or undefined-risk structures.

## Entry rules

- If constituent + index structure gives bullish bias, evaluate long CE first.
- If constituent + index structure gives bearish bias, evaluate long PE first.
- If premium risk is too high but direction remains strong, later versions may evaluate a debit spread; current bot should skip until tested.
- Reject any structure where maximum loss cannot be bounded before entry.

## Exit rules

- Primary stop: index swing invalidation from the directional card.
- Secondary stop: option premium max-loss guard.
- Exit on daily-loss breach or strategy-card invalidation.
- For spreads, future implementation must close the full spread, not one leg independently, unless an explicit adjustment card exists.

## Risk controls

- Max risk per idea: ₹1,500 paper loss per trade for current BankNifty bot.
- Stop/invalidations: index trend peak/trough invalidation + option premium loss cap.
- Avoid conditions: undefined-risk selling, wide spreads, unclear constituent signal, insufficient liquidity, no fresh quote, expiry-day gamma risk without explicit rules.

## Data needed

- Candles: BankNifty index candles, selected option quote/candle, and future spread-leg quotes if spreads are enabled.
- Fundamentals: not directly needed except constituent reason/news layer.
- Sentiment/events: reason monitor for top BankNifty constituent moves and market-wide events.

## Backtest plan

- Date range: multiple market regimes, including trend days, range days, event days, and expiry days.
- Fees/slippage: include Indian option costs, bid/ask spread, and realistic stop execution.
- Metrics: expectancy by structure, max drawdown, stop slippage, skipped trades, loss-cap hits, spread-vs-long-option comparison.

## Backtest status

- Not tested

## Questions to test

- Does a debit spread improve expectancy versus naked long CE/PE under Apoorv's ₹1,500 risk cap?
- Which conditions make short-premium structures attractive enough for paper research, while still blocking live automation?
- How much option spread/slippage makes the theoretical payoff unusable in BankNifty weekly options?

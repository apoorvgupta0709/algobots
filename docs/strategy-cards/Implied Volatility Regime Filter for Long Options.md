# Strategy: Implied Volatility Regime Filter for Long Options

Status: `idea`  
Source type: book / volatility-risk module  
No live execution permitted from this note.

## Source

- Book/report/source: Option Volatility and Pricing; Dynamic Hedging; Options Playbook public reference
- Page/chapter/link: Local extracted volatility sources
- Extracted idea date: 2026-06-09

## Core idea

A correct directional view can still lose money if implied volatility is too expensive or collapses after entry. Long CE/PE entries should be filtered by IV regime and event timing.

## Market / universe

- Market: NIFTY / BANKNIFTY options
- Universe: ATM and near-ATM index options
- Timeframe: intraday initially; positional only after separate testing

## Setup rules

- Compute current IV/India VIX context if available.
- Avoid new long options immediately before/after known IV-crush events unless momentum is exceptional and risk is tiny.
- Prefer long-option entries when direction is strong and IV is not excessively stretched versus recent range.

## Entry rules

- Candidate CE/PE must have direction confirmation plus acceptable IV regime.
- Record IV/VIX snapshot, expiry distance, and event calendar status.
- If IV is extreme, either skip or require faster target/shorter hold.

## Exit rules

- Exit on index invalidation, premium loss cap, or IV collapse that invalidates expected payoff.
- Do not average down purely because premium fell from IV decay.

## Risk controls

- Max risk per idea: ₹1,500 per paper trade.
- Stop/invalidations: index structure + premium guard + IV/event invalidation.
- Avoid conditions: post-news IV crush, expiry-day illiquidity, very wide spread, low-delta option.

## Data needed

- Candles: index and selected option
- Fundamentals: event calendar / RBI / results / macro events
- Sentiment/events: news/reason feed for top constituents and index events

## Backtest plan

- Date range: include different VIX regimes
- Fees/slippage: model spreads separately by expiry/strike
- Metrics: expectancy by IV percentile, hold time, IV change, stop type

## Backtest status

- Not tested

## Questions to test

- Which IV percentile should block long options?
- Does VIX rising help long directional trades enough to offset premium cost?
- Are expiry-day long options viable under strict ₹1,500 risk?

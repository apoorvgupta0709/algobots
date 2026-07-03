# Nifty VWAP Mean Reversion Long

## Source
- [[Deterministic Intraday Options Strategy Pack - NSE 2026]]

## Status
- Card type: entry
- Implementation status: research-only
- Paper trade enabled: false
- Live enabled: false
- Proposed order: 4

## Core idea
Counter-trend Nifty range-day strategy: fade 2σ stretches away from session VWAP back toward VWAP using long ATM options or tight debit spreads.

## Market
- Underlying: NIFTY
- Instrument: ATM long option or tight debit spread
- Regime: range day only

## Entry rules
- CE: price ≤ VWAP - 2σ, bullish rejection candle, RSI-9 between 30 and 45.
- PE: price ≥ VWAP + 2σ, bearish rejection candle, RSI-9 between 55 and 70.
- Entry window: 09:50–14:00.

## Filters
- Require range-day classification.
- Skip narrow-CPR trend-day setups.
- Skip steep VWAP slope / persistent one-side trend.
- VIX ≤ 20.
- Skip event days.

## Risk
- Premium stop per share about ₹22.3 for Nifty lot-size risk cap, or use debit spread.
- Max trade loss ₹1,500.
- Minimum reward ≥1.2R to VWAP target.

## Exits
- Book 60% at VWAP touch.
- Trail remainder to opposite intraday swing.
- Exit by 14:45; force exit 15:20.

## Validation gates
- Profitability must hold only inside range-day labels.
- PF ≥1.3.
- Win rate ≥50%.
- Reject if edge only appears because trend days are mislabeled.

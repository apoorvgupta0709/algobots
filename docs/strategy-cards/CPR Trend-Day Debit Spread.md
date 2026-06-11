# CPR Trend-Day Debit Spread

## Source
- [[Deterministic Intraday Options Strategy Pack - NSE 2026]]

## Status
- Card type: entry
- Implementation status: research-only
- Paper trade enabled: false
- Live enabled: false
- Proposed order: 2

## Core idea
Use narrow CPR as a pre-market trend-day regime filter, then trade first clean directional break through prior-day high/low using debit spreads.

## Market
- Underlyings: NIFTY, BANKNIFTY
- Structure: bull-call or bear-put debit spread

## Entry rules
- Pre-market CPR from previous H/L/C.
- Narrow CPR threshold: NIFTY ≤0.30%, BANKNIFTY ≤0.35%.
- Bias: first 15-minute close above TC = long; below BC = short.
- Enter after 09:45 on 5-minute close beyond prior-day high/low.
- Entry cutoff: 13:30.

## Filters
- Trade only narrow-CPR days.
- Abort if price crosses pivot ≥3 times by 10:15.
- VIX between 10 and 24.
- Skip event days.
- BankNifty: avoid last 3 sessions before monthly expiry.

## Structure and sizing
- Nifty: ATM + 1 strike OTM; net debit ≤ ₹23/share.
- BankNifty: ATM + 1 strike OTM; net debit ≤ ₹50/share.
- Net debit × lot ≤ ₹1,500.

## Exits
- Structure stop: close back inside CPR / opposite CPR zone.
- Premium stop ₹1,200.
- Book 50% at +1R; trail remainder to BE.
- Target prior-day R1/S1 or +2R.
- Exit by 14:45; force exit 15:20.

## Validation gates
- ≥120 trades across Nifty + BankNifty.
- PF ≥1.35.
- Narrow CPR bucket must materially outperform medium/wide buckets.
- BankNifty leg must stay positive excluding pre-expiry blackout.

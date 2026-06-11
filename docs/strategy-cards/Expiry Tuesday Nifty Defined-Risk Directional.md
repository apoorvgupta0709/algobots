# Expiry Tuesday Nifty Defined-Risk Directional

## Source
- [[Deterministic Intraday Options Strategy Pack - NSE 2026]]

## Status
- Card type: entry
- Implementation status: research-only
- Paper trade enabled: false
- Live enabled: false
- Proposed order: 3

## Core idea
Nifty Tuesday-expiry gamma strategy: take a long ATM option only on confirmed morning directional break, with strict time stops to avoid theta decay.

## Market
- Underlying: NIFTY
- Day: weekly expiry Tuesday only
- Structure: long ATM CE/PE; optional 1-strike debit spread in high VIX

## Entry rules
- After 09:45, before 12:30.
- 5-minute close beyond opening range.
- Volume proxy ≥1.5× prior six 5-minute average.
- Price must be on breakout side of VWAP.
- One morning entry; one re-entry allowed only if explicitly backtested.

## Filters
- Tuesday weekly expiry only.
- VIX ≤24.
- OR width ≥0.20%.
- Skip event days.
- Never hold to settlement.

## Risk
- ATM only for highest gamma and liquidity.
- Premium stop per share about ₹22.3 or ₹1,450/lot.
- If spread mode, net debit × lot ≤₹1,500.

## Exits
- Structure stop: close back inside OR.
- Book 50% at +20 option points or +1R.
- Trail remainder to BE.
- Hard exit 13:00; never hold past 13:30.
- Force exit 15:20 as last safety.

## Validation gates
- ≥60 Tuesday expiries.
- PF ≥1.3 net of theta/slippage.
- Bulk of P&L must occur before 12:00.
- Show that 12:30 cutoff improves expectancy.

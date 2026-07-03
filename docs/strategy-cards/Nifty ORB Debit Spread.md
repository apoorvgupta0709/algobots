# Nifty ORB Debit Spread

## Source
- [[Deterministic Intraday Options Strategy Pack - NSE 2026]]

## Status
- Card type: entry
- Implementation status: research-only
- Paper trade enabled: false
- Live enabled: false
- Proposed order: 1

## Core idea
Trade a fixed-clock Nifty opening-range breakout using a bull-call or bear-put debit spread. This is a breakout/initiation strategy, not a pullback-continuation strategy.

## Market
- Underlying: NIFTY
- Instrument: nearest valid Nifty weekly options
- Structure: debit spread only

## Entry rules
- Build opening range from 09:15–09:45 5-minute candles.
- Long: 5-minute close above OR high with volume proxy ≥ 1.5× prior six 5-minute average.
- Short: 5-minute close below OR low with same volume confirmation.
- Entry window: 09:45–13:30.

## Filters
- India VIX between 10 and 22.
- OR width between 0.25% and 1.2% of spot.
- Skip if OR breached both sides before 09:45.
- Skip event days.

## Structure and sizing
- Long leg: ATM CE/PE.
- Short leg: one strike OTM; widen only if still within cap.
- Net debit × lot ≤ ₹1,500.
- Exposure ≤ ₹40,000.

## Exits
- Structure stop: 5-minute close back inside OR.
- Premium stop: ₹1,200 loss.
- Book 50% at +1R; trail remainder to breakeven.
- Final target +2R or spread max value.
- Time exit 13:45; force exit 15:20.

## Validation gates
- ≥150 trades.
- Profit factor ≥1.3 net of costs.
- Win rate ≥40%.
- Max drawdown ≤ 12× average trade risk.
- Must work specifically on valid wide-range breakout days.

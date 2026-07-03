# BankNifty Constituent-Led Directional Long Options

## Status
- Status: draft / research-only; implementation pending
- Confidence: 0.40 until backtested and paper-traded with constituent breadth data
- Market regime: intraday directional move driven by weighted BankNifty constituents
- Timeframe: intraday; 5-minute constituent/index confirmation, option entry after signal confirmation
- Safety: Paper only. No live order may be placed without Apoorv's exact Telegram confirmation and live-gate risk checks.

## Description
Directional long CE/PE strategy for BankNifty options. The index option entry must start from the **base index constituents**, not from BankNifty index LTP alone. The system first measures how the constituent banks are moving, whether the move is broad or concentrated in heavyweights, and whether there are explainable reasons/news for sharp jumps. Only after constituent-led index direction is confirmed should the strategy map the expected index move into option selection and CE/PE paper entry.

## Source references
- Apoorv correction / strategy requirement, 2026-06-09.
- To be extended with uploaded option strategy documents and book evidence.

## Required data inputs
- BankNifty constituent list with index weights.
- Live or near-live FYERS quotes for each constituent and `NSE:NIFTYBANK-INDEX`.
- Constituent change from open, previous close, day high/low, VWAP if available, and relative volume if available.
- News/reason monitor for constituent jumps: earnings, RBI/regulatory, sector news, broker upgrade/downgrade, large corporate actions, unusual volume.
- ATM/near-ATM option chain quotes for selected CE/PE.
- Index swing peaks/troughs from intraday candles.

## Entry thesis
- Buy **CE** only when weighted constituent breadth and index structure support upside continuation.
- Buy **PE** only when weighted constituent breadth and index structure support downside continuation.
- Avoid entering merely because the index moved by a fixed percent; the move must be explained by constituents and market structure.

## Entry rules
- Build weighted constituent score:
  - Positive if heavyweight constituents are above open/previous close and making higher highs/higher lows.
  - Negative if heavyweight constituents are below open/previous close and making lower lows/lower highs.
  - Penalize if movement is concentrated in only one stock while breadth is weak.
- Require constituent confirmation:
  - Majority of weighted constituents align with the proposed direction, or a small number of heavyweights create a clearly explainable index move.
  - At least one reason/news/volume explanation should be attached for large constituent jumps.
- Require index confirmation:
  - Index breaks or reclaims an intraday swing level in the same direction as the constituent score.
  - Index should not be trapped inside a noisy range unless explicitly running a range-reversal setup.
- Require option confirmation:
  - Selected ATM/near-ATM option premium should confirm momentum and have fresh quotes.
  - Option spread and premium must allow stop within per-trade risk.
- Strike selection:
  - Default: nearest expiry ATM option in signal direction.
  - Use near-ITM only if ATM spread/liquidity or risk sizing is unsuitable.

## Stop-loss rules
- Primary SL is based on **index structure**, not only option premium:
  - For CE: below the latest confirmed index higher-low / pullback trough / failed breakout level.
  - For PE: above the latest confirmed index lower-high / pullback peak / failed breakdown level.
- Secondary SL is based on option premium to cap rupee risk:
  - Option SL must keep max loss within ₹1,500 per trade.
  - If index-structure SL implies option loss above ₹1,500, reduce quantity, choose a different strike, or skip.
- Never widen SL after entry.

## Target and trailing rules
- Target should be based on next index resistance/support, R-multiple, and option premium behavior.
- Trail using index swing peaks/troughs:
  - CE: raise SL when index forms a new higher low after advancing.
  - PE: lower risk when index forms a new lower high after declining.
- Option premium trailing may be used only as a risk guard, not as the sole market-structure logic.

## Risk rules
- Max trades per day: 4.
- Max daily paper loss: ₹5,000.
- Max per-trade paper loss: ₹1,500.
- Max open option positions: 1 unless explicitly changed.
- Long options only for this card; no short option selling.
- No new trades after configured cutoff.
- Force intraday exit at configured square-off time.

## Invalidations / no-trade filters
- Constituents diverge from index direction.
- Move has no breadth and no clear heavyweight explanation.
- Quote data stale or missing.
- Option spread/liquidity makes ₹1,500 risk cap impossible.
- Index is inside a tight noisy range and no reversal setup is active.
- Max trade/day or daily loss cap reached.

## Monitoring design
- Pre-entry monitor:
  - Refresh constituent quotes and compute weighted breadth.
  - Attach reason/news flags for large constituent movers.
  - Track index swing peaks/troughs and current regime.
- Post-entry monitor:
  - Monitor index SL level and option premium risk cap together.
  - Exit paper trade if either index invalidation or option-risk SL is hit.
  - Trail SL only after new confirmed index swing.

## Implementation checklist
- [ ] Add BankNifty constituent-weight source table.
- [ ] Ingest constituent quotes every monitor tick.
- [ ] Add weighted breadth and heavyweight contribution score.
- [ ] Add reason/news monitor for constituent jumps.
- [ ] Add intraday index swing peak/trough detector.
- [ ] Replace current fixed index-percent entry rule.
- [ ] Backtest/paper-test before re-enabling cron.

## Tags
options, banknifty, constituents, directional, long-options, risk

# Options 360 Short Straddle / Strangle Premium Decay

## Status
- Status: draft / research-only; do not automate live
- Confidence: 0.35 until tested with option-chain, margin, slippage, and gap-risk modeling
- Market regime: range-bound / volatility mean reversion / premium decay
- Timeframe: intraday and expiry/weekly-monthly variants
- Safety: Research-only. This card involves short options; no live order may be placed without explicit confirmation, margin checks, and risk-gate approval.

## Description
Short-premium strategy card extracted from `Options 360 - Straddle & Strangle Module`. The material focuses on short ATM straddles, short strangles, VWAP/combined-premium trailing, and adjustment rules. This is different from Apoorv's corrected BankNifty directional long-options model. Because short options can have large tail risk, this card is only for research and paper simulation until risk controls are fully modeled.

## Source references
- `Straddle (1).pdf`, saved at `/opt/data/profiles/finance/cache/documents/doc_e20ada105ef1_Straddle (1).pdf`
- Extracted note: `[[Options 360 - Straddle and Strangle Module]]`

## Setup 1 — Range / low-trend straddle filter
### Entry conditions
- Avoid opening at a fixed time without context.
- 5-minute RSI should be between 40 and 60.
- 5-minute ADX and DMI should be merged/flat.
- ADX should be below 25, preferably below both DMI lines.
- Nifty/index should be between day high and day low, not breaking out.
- Do not create after 14:45.

### Interpretation
This is a low-trend, range-bound premium-decay setup. It should be blocked if constituent breadth shows strong directional alignment.

## Setup 2 — Big fall reversal straddle
### Entry conditions
- Wait for a big fall.
- Avoid straddle until there is a reversal signal.
- Wait for hammer or strong reversal candle near good support.
- Wait for candle close before initiating ATM straddle.
- Check India VIX: prefer reversal candle from top, such as shooting star or bearish engulfing.

### Strike choice
- Default ATM straddle.
- If conviction about upside reversal is strong, consider upside strike straddle.
- If conviction is weaker, consider downside strike straddle.

### Exit / invalidation
- Keep the reversal candle low as exit point.
- Shift straddle as market moves upside.
- Aim to capture premium decay until good resistance.

## Setup 3 — Combined premium VWAP straddle
### Entry conditions
- Check spot at 09:30.
- Track selected strike combined premium.
- If by 10:30 combined premium goes above VWAP, wait.
- If combined premium comes below VWAP, sell straddle.

### Stop / trail
- Use day high of combined premium as stop-loss.
- Trail SL according to VWAP.

## Adjustment rules — premium bands
### Smaller premium scale
- Adjust on 100-point move if premiums are above 350.
- Adjust on 75-point move if premiums are 250-350.
- Adjust on 50-point move if premiums are 150-250.
- Adjust on 25-point move if premiums are below 150.
- Always book ITM strike.
- Shift from straddle to strangle or strangle to straddle as needed.

### Larger premium scale
- Adjust on 300-point move if premiums are above 1200.
- Adjust on 200-point move if premiums are 800-1200.
- Adjust on 150-point move if premiums are 600-800.
- Adjust on 100-point move if premiums are below 600.
- Always book ITM strike.
- Shift from straddle to strangle or strangle to straddle as needed.

## Adjustment 2 — breakeven defense
- Make a straddle and wait until breakeven point is breached.
- At BEP, book profit leg.
- Sell same-side ITM option in double quantity and sell one extra option of remaining leg.
- Repeat to increase range, but avoid more than 3 times.

## Strangle setup 1 — positional weekly/monthly
- Find two support and two resistance levels.
- Sell beyond those two levels and collect at least ₹100.
- Adjust according to minor levels from both sides.
- Alternatively adjust if one side premium doubles.
- Shift loss leg if instrument breaks any major level.

## Strangle setup 2 — expiry OI setup
- Use for expiry.
- Find max OI strikes on both sides on Wednesday around 13:00.
- Check support/resistance near those strikes.
- Choose strikes outside those OI strikes and levels.
- Choose premiums between ₹25 and ₹40.
- If one side premium falls 35-40%, shift that leg inward.

## Risk rules for research simulation
- Short-premium strategies need separate margin and tail-risk modeling.
- Must model gap risk, slippage, bid/ask spread, and adjustment fill risk.
- This card should not be connected to live or automated order placement until explicit approval.
- For Apoorv's current paper risk envelope, any simulated variant must obey:
  - Max daily loss: ₹5,000.
  - Max per-trade loss: ₹1,500.
  - Max trades/day: 4.

## No-trade filters
- Strong constituent-led directional move.
- Index breaks major support/resistance with breadth.
- VIX expanding rather than reversing.
- Combined premium above VWAP for Setup 3.
- After 14:45 unless manually reviewed.
- Any inability to cap risk within the configured loss limits.

## Implementation checklist
- [ ] Build combined premium series for CE+PE.
- [ ] Add option VWAP or proxy calculation.
- [ ] Add ADX/DMI/RSI on 5-minute index candles.
- [ ] Add support/resistance and OI strike ingestion.
- [ ] Add margin/tail-risk simulator before any short-option automation.
- [ ] Backtest and paper trade only.

## Tags
options, straddle, strangle, short-premium, volatility, research-only

# Volatility Regime Filter

## Status
- Status: draft / research-only
- Confidence: 0.85
- Market regime: all regimes; used as a filter
- Timeframe: daily/intraday risk overlay
- Safety: No live order may be placed from this card without Apoorv's exact Telegram confirmation and live-gate risk checks.

## Description
Filter that adjusts or blocks setups when volatility is too low, too high, or structurally unstable.

## Source references
- Dynamic_Hedging-Taleb pp. 50-55
- Dynamic_Hedging-Taleb pp. 13-17
- Dynamic_Hedging-Taleb p. 516
- Dynamic_Hedging-Taleb p. 511
- Dynamic_Hedging-Taleb pp. 403-406
- Dynamic_Hedging-Taleb pp. 152-155

## Entry rules
- Allow setups only when ATR/realized volatility is compatible with the planned stop and target.
- For high-volatility regimes, demand wider confirmation and smaller size; for dead-volatility regimes, avoid low-movement setups.

## Exit rules
- Reduce/exit if volatility expands against the trade or invalidates the stop model.

## Invalidation rules
- Reject setups where volatility makes the stop unrealistic or expected movement too small.

## Risk rules
- Use volatility-adjusted sizing; never use fixed quantity without checking ATR and max rupee risk.

## Evidence snippets
- s Equities The underlying equity The domestic interest rate Volatility . Dividend payout Volatility term structure Fixed Income Rates Higher order derivatives Term structure of rates Risks of pricing formula Volatility Volatility term structure* Stability of the covari- ances between maturities Currencies Price Rates ineach currency * Volatility Volatility t
- Ranking of Fungibility 209 Fungibility and the Term Structure of Prices: The Cash-and-Carry Line | 210 Fungibility and Option Arbitrage 212 Changes in the Rules of the Game 212 Convergence 213 Mapping Convergence 215 Convergence and Convexity 216 Levels of Convergence Trading 216 Volatility and Convergence 216 Convergence and Biased Assets 216 Stacking Tech
- rt reading, 368-375 Risk-neutrality, 128, 426-430, 463-464 Risk reversal, 135, 268, 275, 314, 323, Rolled, 429 Rollover option, lookback options, 404-405 Rosenberg, J., 142 Ross, ., 188, 372 Rotation (second order curve/surface shift), 162, 163 Rubber trees, 225 Rubinstein, Marc, 177, 345, 372 Index 505 Rudd, A. 114 Russell, B., 13 Savage, L., 65, 66 Savery
- 21 345, 415-425, 464 Calendar, smile, 27-29 Calendar rules, and ranking of securities, 454 Calendar spreading, 272 Call option, 18, 472-473 Caplets, 34, 382 Capped index option, 365 Caps, 34, 40 Caption, 382 Carry, 295. See also Convergence correlation between interest rates and, 252 “hogs,” 216 Case studies: at-settlement, 306-307 betspreads, 292-294 indexe
- useful to compare the deltas of the total structure. Table 22.1 shows the sensitivity of the option partial delta A to the asset A alone (asset B remains frozen). To measure the total, or uncorrelated delta, requires more involved ma- trix analysis: Total delta: V72 V which can be computed for our two-asset position as: 2 Gag A Ua, dal [oes This leads to the
- alendar spread between two different ma- turities in the SP500 contracts on the Chicago Mercantile Exchange (CME): True Gamma (in front Position Delta Gamma __ contract equivalent) Sep (90 days) Long 2000 1030 100 100 contracts March (270 days) Short 2000 (1054) (58) (65) contracts First Adjustment The back month could have a lower or higher volatility expos

## Tags
risk, volatility, filter

## Processing checklist
- [ ] Review source evidence manually
- [ ] Convert to explicit hypothesis
- [ ] Backtest on local market data
- [ ] Paper trade with journal
- [ ] Promote only after performance review

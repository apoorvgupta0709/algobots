# NSE Defined-Risk Payoff Selector

## Status
- Status: reviewed / research-paper only
- Live trading: not approved
- Universe: NIFTY/BANKNIFTY options
- Timeframe: pre-entry structure selection
- Market regime: all regimes; structure router

## Description
Payoff-structure selector derived from NSE option-strategy education; automation is restricted to long options and tested debit spreads.

## Entry rules
- Choose long CE for confirmed bullish thesis
- Choose long PE for confirmed bearish thesis
- Consider debit spread only when implemented/backtested

## Exit rules
- Close full structure at stop/target
- No legging out without explicit adjustment card

## Risk rules
- reject unlimited-risk short options
- max loss known before entry
- include costs/slippage in cap

## Invalidation rules
- max loss unknown
- margin/gap risk unmodeled
- expiry gamma risk unmodeled

## Source links
- [[NSE Official Public Sources in Vector DB]]
- [[Source-Backed Bot Rule Pack]]

## Processing checklist
- [x] Source-backed draft created
- [ ] Deterministic backtest implemented
- [ ] Paper-trading journal reviewed
- [ ] Promotion decision recorded

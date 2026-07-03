# Long Options Volatility and Greeks Gate

## Status
- Status: reviewed / research-paper only
- Live trading: not approved
- Universe: NIFTY/BANKNIFTY long options
- Timeframe: pre-entry and monitor
- Market regime: high IV, event, expiry, or unstable volatility

## Description
Pre-entry filter for long-option trades using premium behavior, IV/realized-volatility context, and Greeks-sensitive risk sizing.

## Entry rules
- Premium must permit a valid stop under rupee cap
- Prefer fresh quote and stable spread
- Avoid event IV crush unless explicitly modeled

## Exit rules
- Exit if premium stop or index invalidation triggers
- Do not average down long options

## Risk rules
- smaller size in high IV
- skip if spread/slippage dominates risk
- cost-aware loss cap

## Invalidation rules
- stale quote
- wide spread
- IV/event context not modeled

## Source links
- [[NSE Official Public Sources in Vector DB]]
- [[Source-Backed Bot Rule Pack]]

## Processing checklist
- [x] Source-backed draft created
- [ ] Deterministic backtest implemented
- [ ] Paper-trading journal reviewed
- [ ] Promotion decision recorded

# BankNifty Constituent-Led Long Options

## Status
- Status: reviewed / research-paper only
- Live trading: not approved
- Universe: BANKNIFTY index options; long CE/PE only until debit spreads pass backtests
- Timeframe: 5-minute entry check with intraday monitor
- Market regime: directional expansion with constituent confirmation

## Description
Long CE/PE BankNifty paper playbook using index structure plus top-constituent confirmation before selecting a defined-risk option structure.

## Entry rules
- Index structure gives bullish/bearish bias
- Top-weight constituents confirm direction
- Option quote is fresh and liquid
- Risk cap passes including costs

## Exit rules
- Index swing invalidation
- Option premium max-loss guard
- Trailing runner exit after partial favorable move
- Time/EOD exit

## Risk rules
- paper_only
- live_orders_enabled=false
- max net loss <= ₹1,500/trade
- daily loss guard <= ₹5,000

## Invalidation rules
- constituents diverge
- breakout fails
- quote stale
- risk cap fails
- undefined-risk structure requested

## Source links
- [[NSE Official Public Sources in Vector DB]]
- [[Source-Backed Bot Rule Pack]]

## Processing checklist
- [x] Source-backed draft created
- [ ] Deterministic backtest implemented
- [ ] Paper-trading journal reviewed
- [ ] Promotion decision recorded

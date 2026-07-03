# Source-Backed Bot Rule Pack

Status: reviewed / paper-only. No live execution approval.

## Mandatory pre-entry rules
1. `paper_only=true` and `live_orders_enabled=false` must remain true/false respectively.
2. Net modeled loss including costs/slippage must be <= ₹1,500 per trade.
3. Daily strategy loss guard remains <= ₹5,000.
4. BankNifty direction needs index structure plus constituent confirmation.
5. Only long CE/PE are automation-eligible now; debit spreads need separate backtest; naked short options are blocked.
6. Fresh quote/liquidity check required before selecting an option contract.
7. Breakout entries require hold/follow-through confirmation; close back inside structure is defensive-exit/no-trade evidence.
8. No promotion from paper to live-review without deterministic backtest + multi-week paper journal.

## Implementation note
These rules are also stored in `knowledge.rules` for search/audit. They are specifications, not live order instructions.

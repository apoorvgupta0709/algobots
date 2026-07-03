# Single-Stock Momentum with Index Confirmation

## Source
- [[Deterministic Intraday Options Strategy Pack - NSE 2026]]

## Status
- Card type: entry
- Implementation status: research-only
- Paper trade enabled: false
- Live enabled: false
- Proposed order: 5

## Core idea
Trade liquid stock options only when the stock breakout and its confirming index agree. This trades constituents directly, unlike the active BankNifty index options strategy.

## Market
- Stocks: HDFCBANK, ICICIBANK, SBIN, RELIANCE, INFY, TCS
- Confirming index: BankNifty for banks; Nifty for others
- Structure: prefer debit spread; ATM long only if risk cap holds

## Entry rules
- Stock breaks opening range.
- Stock is on correct VWAP side.
- Confirming index is on same VWAP side and breaking same direction.
- Stock intraday move exceeds index by at least 0.2%.
- Entry window: 09:50–13:30.

## Filters
- Skip stock earnings days.
- Skip if stock and index disagree.
- Option spread ≤0.5% of option price.
- VIX between 10 and 24.

## Risk
- Pull lot sizes from live contract file.
- Net debit per share must satisfy ₹1,500 / lot.
- Premium exposure ≤₹40,000.
- Model stock-option slippage much higher than index options.

## Exits
- Structure stop: stock closes back inside OR or loses VWAP side.
- Premium hard stop around ₹1,300/lot.
- Book 50% at +1R; trail to BE.
- Exit by 14:30; force exit 15:20.

## Validation gates
- Backtest each name separately ≥12 months.
- Enable only names with PF ≥1.3 and ≥40 trades.
- Earnings-window trades must be excluded from edge.

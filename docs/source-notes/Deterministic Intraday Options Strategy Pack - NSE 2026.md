# Deterministic Intraday Options Strategy Pack - NSE 2026

## Source
- Uploaded document: `Hermes_Agent_Five_Deterministic_Intraday_Options_Strategies_for.md`
- Cache path: `/opt/data/profiles/finance/cache/documents/doc_733a50065bbb_Hermes_Agent_Five_Deterministic_Intraday_Options_Strategies_for.md`
- Status: research source note
- Trading status: **not runnable / not paper-enabled**

## Core thesis
Build five deterministic intraday NSE options strategies that avoid overlap with the active BankNifty constituent-led pullback-continuation bot. Favor defined-risk debit spreads and long options because Apoorv's risk cap is ₹1,500 per trade and credit spreads are usually infeasible under that cap.

## Market-structure assumptions to verify before any implementation
- Nifty weekly expiry: Tuesday.
- BankNifty: monthly only, last Tuesday.
- Lot sizes must be pulled from live NSE/FYERS contract master; do not hardcode.
- Options sell-side STT: 0.15% from April 2026; model costs in every spread backtest.
- Credit-spread guard: block unless `(width - credit) * lot <= ₹1,500` and explicit expiry exception applies.

## Strategy candidates created from this source
- [[Nifty ORB Debit Spread]]
- [[Nifty VWAP Mean Reversion Long]]
- [[CPR Trend-Day Debit Spread]]
- [[Single-Stock Momentum with Index Confirmation]]
- [[Expiry Tuesday Nifty Defined-Risk Directional]]

## Implementation priority
1. [[Nifty ORB Debit Spread]]
2. [[CPR Trend-Day Debit Spread]]
3. [[Expiry Tuesday Nifty Defined-Risk Directional]]
4. [[Nifty VWAP Mean Reversion Long]]
5. [[Single-Stock Momentum with Index Confirmation]]

## Global guards for all candidates
- Paper-only until explicitly approved.
- Intraday-only; force exit by 15:20 IST.
- Max 1 open position.
- Max 3 trades/day.
- Max daily loss ₹5,000.
- Max trade loss ₹1,500.
- Max premium exposure ₹40,000.
- No first 15 minutes; no new entries after strategy-specific cutoff.
- Major event-day filter required before paper enabling.
- Config hash/version should be logged with every simulated/paper trade.

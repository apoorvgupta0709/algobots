# BankNifty Day Pattern — 2026-06-15 (BANKNIFTY)

Research / paper-only after-market analysis. No live orders; no order placement code.

## 1. Classification

- **Class:** `trend`
- **Direction:** bearish
- **Confidence:** 0.5995
- **Rule version:** banknifty_trend_patterns_v1 (deterministic_rules)
- **Secondary tags:** spike_channel

## 2. Evidence

- **Open/High/Low/Close:** 57679.650000 / 57804.500000 / 57119.200000 / 57160.650000
- **Gap / Day return / Range:** 1.58% / -0.90% / 1.19%
- **ORB:** 57445.550000–57804.500000 (range 0.62%); break down, hold=True
- **VWAP:** crosses=2, side 4.00% of candles above
- **Close location:** 0.0605 (0=low, 1=high)
- **Realized vol (5m σ):** 0.0629%; range vs ADR10 0.7825x
- **Breadth:** +0.00% / -100.00%; VWAP-confirm 100.00%; divergence=False
- **Option chain:** unavailable for this session (warned, not guessed — IV/OI/PCR not used).
- **Top negative contributors:** NSE:HDFCBANK-EQ (-1.61%), NSE:ICICIBANK-EQ (-1.89%), NSE:SBIN-EQ (-1.35%)
- **Data warnings:** option-chain context unavailable for session

### Day segments

| Segment | Return | Range | VWAP side | Net | Close loc |
| --- | --- | --- | --- | --- | --- |
| open_drive | -0.51% | 0.8% | 0.0% | down | 0.0972 |
| midday | -0.15% | 0.64% | 7.69% | down | 0.228 |
| close | -0.24% | 0.49% | 0.0% | down | 0.148 |

## 3. Similar historical days

| Date | Class | Direction | Similarity | Note |
| --- | --- | --- | --- | --- |
| 2026-05-21 | trending_range | bearish | 0.5213 | trending_range/bearish, ret -0.89% |
| 2026-06-01 | trend | bearish | 0.3065 | trend/bearish, ret -1.37% |
| 2026-03-23 | trend | bearish | 0.1993 | trend/bearish, ret -2.15% |
| 2026-03-30 | trend | bearish | 0.198 | trend/bearish, ret -2.18% |
| 2026-03-11 | trend | bearish | 0.1654 | trend/bearish, ret -1.98% |

## 4. How it could have been played (paper/research)

- Trend day (bearish): paper-bias toward PE (long puts). ORB hold + VWAP/pullback continuation entries; hold the runner.
- Add only on shallow pullbacks that respect VWAP; avoid counter-trend fades.
- Exit model: after +0.5R move the paper stop to breakeven + one tick / cost proxy, then trail via MFE ratchet / structure trailing. No fixed profit cap.

## 5. Bot lessons

- ORB break down held; this is a primary allowed/blocked-entry signal for the day type.
- Option-chain context (ATM IV / PCR / max-pain) was unavailable; classification used index + breadth only (warned, not guessed).
- Exit model: after +0.5R move the paper stop to breakeven + one tick / cost proxy, then trail via MFE ratchet / structure trailing. No fixed profit cap.

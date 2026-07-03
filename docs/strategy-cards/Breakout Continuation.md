# Breakout Continuation

## Status
- Status: draft / research-only
- Confidence: 0.85
- Market regime: trend / momentum expansion
- Timeframe: daily setup with intraday confirmation
- Safety: No live order may be placed from this card without Apoorv's exact Telegram confirmation and live-gate risk checks.

## Description
Momentum continuation setup after price breaks and holds above a prior resistance/range level with participation.

## Source references
- Reading Price Charts Bar by Bar pp. 276-279
- Trading Price Action Reversals: Technical Analysis of Price Charts Bar by Bar for the Serious Trader - PDFDrive.com pp. 524-529
- 3) Trading Price Action Trading Ranges AL Brooks pp. 442-445
- 3) Trading Price Action Trading Ranges AL Brooks pp. 419-420
- 3) Trading Price Action Trading Ranges AL Brooks pp. 252-254
- 3) Trading Price Action Trading Ranges AL Brooks pp. 373-374

## Entry rules
- Only consider long setups when price breaks or reclaims a clear resistance/range high and then holds above it.
- Prefer setups with trend alignment and expanding participation/volume rather than thin breakouts.
- Use current local FYERS/Postgres technical factors as the execution-time source of truth.

## Exit rules
- Take partial/full exit at predefined reward-to-risk target or when momentum fails.
- Exit if price closes back inside the old range after entry and invalidation is confirmed.

## Invalidation rules
- Reject if breakout level is not identifiable or price cannot hold above the level.
- Reject if quote/factor data is stale or volume participation is weak.

## Risk rules
- Stop must be below the breakout/retest level or recent swing low before any trade idea is sent.
- Position size must cap rupee risk; never widen stop after entry.

## Evidence snippets
- orrection. For example, if there is a Wedge top in a bull and the market drops a little but then reverses strongly to a new high, and that new high fails, this can lead to a strong bear move since it is a second failure by the bulls to push beyond this price area. The move down from ## Page 276 248 READING PRICE CHARTS BAR BY BAR FIGURE 9.26 A Failed Wedge R
- ive definition, creation, and traders’ responses to final flags and increased slope and late acceleration as exhaustion momentum needed for with more reasons to reverse multiple trend channel lines and opposite trend bars creating parabolic moves and seen as three-push patterns spike pullbacks spike up and down in one bar spike up but channel down ## Page 52
- es the other way. Otherwise, you will miss great trades, and traps are among the best. ## Page 442 When a tight trading range forms at an extreme of the day after a breakout, it usually becomes a continuation pattern. Look for a with-trend entry, which sometimes happens after a false breakout in either direction. By bar 13 or so in Figure 22.9, most traders
- sloping channels, trading within a channel is especially difficult when the channel is tight. When a channel or trading range is tight, only the most consistently profitable traders should trade, and these traders will enter using limit orders. Most traders should enter almost exclusively on stops. Since that is exactly the opposite way to trade tight channe
- al moving average that led to new highs on the move up to bar 4. Bar 4 was a trend channel line overshoot that led to a sharp correction to bar 5, which tested the 15 minute 20-bar EMA (dotted line) and was then followed by a test of the trend high (bar 6 was a higher high). The market gapped down to bar 7, and, although the market initially appeared bearish
- that the market will now move in the opposite direction). Most of the time, that will occur at some area of support or resistance, like a prior swing point or trend line or trend channel line, and most of the time it will be in the area of some measured move. The reversal is a move toward what will become the middle of a trading range where there is a 50 pe

## Tags
technical, breakout, momentum

## Processing checklist
- [ ] Review source evidence manually
- [ ] Convert to explicit hypothesis
- [ ] Backtest on local market data
- [ ] Paper trade with journal
- [ ] Promote only after performance review

# Position Sizing / Risk Rule Card

## Status
- Status: draft / research-only
- Confidence: 0.85
- Market regime: all regimes; mandatory risk overlay
- Timeframe: every trade idea
- Safety: No live order may be placed from this card without Apoorv's exact Telegram confirmation and live-gate risk checks.

## Description
Universal risk card: trade size is derived from invalidation distance and maximum allowed loss.

## Source references
- DAY TRADING STRATEGIES: THE COMPLETE GUIDE WITH ALL THE ADVANCED TACTICS FOR STOCK AND OPTIONS TRADING STRATEGIES. FIND HERE THE TOOLS YOU WILL NEED TO INVEST IN THE FOREX MARKET. pp. 30-33
- DAY TRADING STRATEGIES: THE COMPLETE GUIDE WITH ALL THE ADVANCED TACTICS FOR STOCK AND OPTIONS TRADING STRATEGIES. FIND HERE THE TOOLS YOU WILL NEED TO INVEST IN THE FOREX MARKET. pp. 37-41
- Edwin_LeFevre_Reminiscences_of_a_Stock_Operator p. 269
- Edwin_LeFevre_Reminiscences_of_a_Stock_Operator p. 259
- Algorithmic Trading pp. 196-198
- 3) Trading Price Action Trading Ranges AL Brooks pp. 572-573

## Entry rules
- No trade idea is valid until entry, stop, target, quantity, and max loss are known.
- Size must be computed from rupee risk per share and configured per-trade risk cap.

## Exit rules
- Exit when stop/target/time-stop triggers; do not move risk limit farther away after entry.

## Invalidation rules
- Reject if stop is missing, stop is on wrong side of entry, or quantity breaches risk config.

## Risk rules
- Respect max risk per trade, daily loss, weekly loss, max capital, and max open positions.
- Live execution remains disabled unless the approval gate and kill-switch checks pass.

## Evidence snippets
- and wait to trade another day. This will help them in ensuring that they are disciplined when it comes to investing since they will invest an amount of money that they are ready to lose. Fear is defined as something that one perceives as a threat to their income and also to their profits. Fear is also beneficial because it encourages the trader to hold back
- as your technical system indicates, but this is simply not true. ## Page 37 Getting adequate rest and following proper discipline is what ensures success. You’re increasing your risk massively by choosing to ignore these principles. Mental Prep The trading session is not the time and place for you to analyze anything. Your trade entries should be an automat
- 1 5 T h e M o n t h l y N e w s l e t t e r o f t h e M a r k e t Te c h n i c i a n s A s s o c i a t i o n , I n c . Risk Equalization a month period. All indications pointed to a very totalsituationaccountmaysizebe different,in either example.but the mathYourworkspersonalthe by Michael Covel promising future. same. The risk in dollars  uctuates as the a
- olutely crucial and it is 72 ## Page 259 People regularly disregarded by most market F1) Let Your Profits Run, Cut Your Losses Short participants: There is absolutely nothing that can be predicted! P RUN PROFITS / CUT LOSSES -> LONG OPTIONALITY TRADERS´: This is indeed in contrast the value of an Option is driven by the [low probability, to all we are offere
- ion. If we try ff of 31, we shall find that the growth rate is −1; that is, ruin. This is because the most negative return per period is −0.0331, so any leverage higher than 1 / 0.0331 = 30.2 will result in total loss during that period. Optimization of Historical Growth Rate Instead of optimizing the expected value of the growth rate using our ana- lytical
- by a few ticks after you've scalped out part of your trade, you can hold through the pullback and rely on your original stop, despite being in a drawdown of several ticks. Otherwise (for example, in a new long) you will exit the swing portion at breakeven and then buy again above the high of the bar that ran your stop, giving up a couple of points or more of

## Tags
risk, sizing, mandatory

## Processing checklist
- [ ] Review source evidence manually
- [ ] Convert to explicit hypothesis
- [ ] Backtest on local market data
- [ ] Paper trade with journal
- [ ] Promote only after performance review

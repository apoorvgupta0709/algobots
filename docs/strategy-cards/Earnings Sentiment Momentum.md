# Earnings Sentiment Momentum

## Status
- Status: draft / research-only
- Confidence: 0.85
- Market regime: event-driven trend / post-news drift
- Timeframe: fresh event window plus daily confirmation
- Safety: No live order may be placed from this card without Apoorv's exact Telegram confirmation and live-gate risk checks.

## Description
Context card for price strength supported by earnings/news/filing sentiment rather than technicals alone.

## Source references
- Algorithmic Trading pp. 179-181
- Algorithmic Trading pp. 173-175
- Advances in Financial Machine Learning pp. 53-54
- Algorithmic Trading pp. 223-224
- Edwin_LeFevre_Reminiscences_of_a_Stock_Operator p. 269
- Edwin_LeFevre_Reminiscences_of_a_Stock_Operator pp. 232-233

## Entry rules
- Require a fresh positive or improving event context before adding sentiment score.
- Pair sentiment with technical confirmation; do not buy solely from a headline or LLM summary.

## Exit rules
- Exit/downgrade if the catalyst is contradicted by later filings/news or price rejects the move.

## Invalidation rules
- Reject stale, unsourced, or low-quality sentiment; mark mixed/negative event risk explicitly.

## Risk rules
- Lower size around binary events and when sentiment confidence is low.

## Evidence snippets
- ge return of close to 27 percent. You might wonder whether holding these positions overnight will gener- ate additional profits. The answer is no: the overnight returns are negative on average. On the contrary, many published results from 10 or 20 years ago have shown that PEAD lasted more than a day. This may be an example where the duration of momentum is
- of futures or stocks often exhibit cross-sectional momentum: a simple ranking algorithm based on returns would work. • Profitable strategies on news sentiment momentum show that the slow diffusion of news is a cause for stock price momentum. • The contagion of forced asset sales and purchases among mutual funds ALGORITHMIC contributes to stock price momentu
- ties, competition, outlook, etc. Some specialized firms sell statistics derived from alternative data, for example, the sentiment extracted from news reports and social media. A positive aspect of analytics is that the signal has been extracted for you from a raw source. The negative aspects are that analytics may be costly, the methodology used in their pro
- for, 64–70 multiple symbols, 35 stocks, 89–91, 102 Mutual funds Pearson system, 176 asset fire sale, 149–151 P/E (price-earnings) ratio, ranking stocks composition changes in, 162–163 using, 104–105 Pitfalls National best bid and offer (NBBO) quote sizes of mean-reversion strategies, 60–61, 83–84, for stocks, 90, 166–167 153 Neural net trading model, 23 of m
- unpredictable trends for possible lifelong profits? ◆ Forinformationontheauthor,seep.8. 21 ## Page 269 News from the Technical Analysts’ Professional Certiﬁ cation Organization Vo l u m e 3 6 • N u m b e r 1 5 T h e M o n t h l y N e w s l e t t e r o f t h e M a r k e t Te c h n i c i a n s A s s o c i a t i o n , I n c . Risk Equalization a month period. A
- considerable following among the professional traders and the wire houses. The deal was extremely well advertised. The newspapers certainly were generous with their space. The older concerns were identified with the stove industry of America and their product was known the world over. It was a patriotic amalgamation and there was a heap of literature in the

## Tags
sentiment, fundamental, event

## Processing checklist
- [ ] Review source evidence manually
- [ ] Convert to explicit hypothesis
- [ ] Backtest on local market data
- [ ] Paper trade with journal
- [ ] Promote only after performance review

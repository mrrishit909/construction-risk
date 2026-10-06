# Evaluation

Everything here is measured on synthetic projects generated with seeds the demo does not use (`(101, 102, 103)`), each played out 8 different
ways (its own trade overruns, suppliers late at random, a wet spell at a random time), with the forecast made on day 180. Reproduce with
`python -m cr.evaluate` (under a minute). The numbers say how well the methods do against the project as the simulator plays it, where the
past projects the models learn from come from the same process as the one being forecast. That is the generous case: on a real site the history
is from other buildings, other crews and other years, and the forecast would be less sure than this.

## 1. The finish forecast

Three answers for the finish day, scored against the real finish. "Re-plan" is what a scheduler gets from the schedule tool: remaining work at
planned durations from today. "Blind" is the Monte Carlo forecast knowing only the site status, which is what the demo shows first. "Told" is
the same forecast after the wet spell and the late deliveries are announced, which is what the demo shows after the scenario is applied.

|  | mean error (days) | bias (days, + is late) | truth inside P10–P90 |
|---|---|---|---|
| plan | 80.1 | -80.1 | no band |
| blind | 16.7 | -6.4 | 75% |
| told | 14.2 | 2.3 | 71% |

Over 24 project-outcomes. Re-planning at planned durations is -80 days optimistic on average: the plan does not know that trades run late.
The blind forecast corrects most of that from history alone. Being told about the disruptions helped less than hoped: the mean error drops
2.5 days and the bias moves from -6.4 to 2.3, because in most of these outcomes the late supplier and the rain fall on work with
float, and only the few where they hit the critical path (the outcomes where "told" moves 30 to 50 days) are improved. The told band covers the truth 71% of
the time, which for an 80% band is a little narrow: the trade spread is estimated from history, but the correlation between activities (a slow crew is slow on every floor) is not modelled, so the band is tighter than it should be.
The forecast is only as good as what the site tells it; the scenario step exists to make the telling cheap.

| project (seed) | outcome | planned end | real finish | re-plan | blind P10–P50–P90 | told P10–P50–P90 | told covers |
|---|---|---|---|---|---|---|---|
| 101 | 1 | 305 | 393 | 335 | 395–413–442 | 397–414–439 | no |
| 101 | 2 | 305 | 396 | 324 | 381–397–418 | 386–399–418 | yes |
| 101 | 3 | 305 | 429 | 346 | 414–432–456 | 423–440–461 | yes |
| 101 | 4 | 305 | 371 | 309 | 361–377–395 | 359–375–396 | yes |
| 101 | 5 | 305 | 393 | 325 | 384–399–420 | 384–402–426 | yes |
| 101 | 6 | 305 | 399 | 335 | 394–413–440 | 404–420–444 | no |
| 101 | 7 | 305 | 359 | 290 | 337–351–369 | 339–353–372 | yes |
| 101 | 8 | 305 | 422 | 318 | 371–389–409 | 370–387–412 | no |
| 102 | 1 | 312 | 397 | 335 | 394–412–436 | 395–413–440 | yes |
| 102 | 2 | 312 | 378 | 308 | 359–374–396 | 359–376–397 | yes |
| 102 | 3 | 312 | 454 | 342 | 401–423–445 | 448–472–494 | yes |
| 102 | 4 | 312 | 484 | 342 | 402–423–451 | 475–497–520 | yes |
| 102 | 5 | 312 | 456 | 339 | 400–421–446 | 438–456–479 | yes |
| 102 | 6 | 312 | 377 | 317 | 376–391–412 | 378–397–416 | no |
| 102 | 7 | 312 | 394 | 327 | 380–399–424 | 382–402–424 | yes |
| 102 | 8 | 312 | 387 | 320 | 376–395–416 | 377–396–417 | yes |
| 103 | 1 | 304 | 431 | 335 | 389–408–438 | 392–409–425 | no |
| 103 | 2 | 304 | 410 | 334 | 390–408–438 | 398–413–435 | yes |
| 103 | 3 | 304 | 447 | 334 | 384–407–448 | 387–406–428 | no |
| 103 | 4 | 304 | 418 | 337 | 403–420–447 | 414–429–450 | yes |
| 103 | 5 | 304 | 359 | 324 | 373–391–415 | 373–392–417 | no |
| 103 | 6 | 304 | 414 | 329 | 380–398–421 | 379–396–417 | yes |
| 103 | 7 | 304 | 415 | 328 | 375–393–420 | 377–396–419 | yes |
| 103 | 8 | 304 | 406 | 328 | 389–404–421 | 393–408–427 | yes |

Days from the planned start.

## 2. Which activities run late

A gradient-boosted classifier (trade, size, floor, outdoor, wet season, fed by procurement, cost) for "more than 10% over plan", trained on sixteen
past projects and tested on four. The base rate is the share that run late anyway.

| project (seed) | test activities | AUC | base rate | calibration, predicted→observed by bin |
|---|---|---|---|---|
| 101 | 2400 | 0.577 | 65% | 0.38→0.62 / 0.55→0.59 / 0.68→0.67 / 0.84→0.76 |
| 102 | 2400 | 0.617 | 65% | 0.36→0.39 / 0.53→0.55 / 0.69→0.68 / 0.85→0.80 |
| 103 | 2400 | 0.598 | 67% | 0.38→0.46 / 0.53→0.56 / 0.69→0.70 / 0.84→0.85 |

An AUC around 0.6 is weak: most of the overrun in this world is noise around a trade median, which the features cannot see. The classifier is used
only to rank the trade-performance risks in the register; the forecast itself uses the trade distributions, not this model.

## 3. What is not measured

- The site photo reader counts coloured rectangles in a schematic image. It is exact on those images and would read nothing from a real photo.
- The change-order impact is the same Monte Carlo with extra days added; its accuracy is the forecast's.
- The cost exposure (trade overruns at 60% labour share plus 18,000 a day of overhead past the planned end) is a stated formula, not a model.

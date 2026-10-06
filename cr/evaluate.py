"""Measure the forecast and the models on projects and outcomes the demo never uses. Writes docs/evaluation.md.

    python -m cr.evaluate
"""
import pathlib

import numpy as np

from . import engine, world

OUT = pathlib.Path(__file__).resolve().parents[1] / "docs" / "evaluation.md"
SEEDS = (101, 102, 103)
TRUTHS = range(1, 9)
RUNS = 300


def table(head, rows):
    return "\n".join(["| " + " | ".join(head) + " |", "|" + "---|" * len(head)] + ["| " + " | ".join(str(c) for c in r) + " |" for r in rows])


def main():
    model_rows, fc_rows, cover = [], [], {"blind": [], "told": [], "plan": []}
    err = {"blind": [], "told": [], "plan": []}
    for seed in SEEDS:
        w = world.build(seed)
        ha, hp = world.history(w)
        trades, procs, late = engine.fit_trades(ha), engine.fit_procurement(hp), engine.late_model(ha)
        model_rows.append([seed, late["metrics"]["tested_on"], late["metrics"]["auc"], f"{late['metrics']['base_rate']:.0%}", " / ".join(f"{c['predicted']:.2f}→{c['observed']:.2f}" for c in late["metrics"]["calibration"])])
        plan = engine.planned_end(w)
        for t in TRUTHS:
            r = np.random.default_rng([seed, t])
            late_items = {k: int(r.exponential(25)) for k in world.PROCUREMENT if r.random() < 0.4}
            rain = (int(r.integers(160, 260)), 0)
            rain = (rain[0], rain[0] + int(r.integers(10, 30)))
            act = world.actuals(w, t, late=late_items, rain=rain)
            st = engine.status_at(w, act, world.NOW)
            truth = st["true_finish"]
            blind = engine.forecast(w, st, trades, procs, world.NOW, runs=RUNS, seed=t)
            told = engine.forecast(w, st, trades, procs, world.NOW, runs=RUNS, seed=t, rain=rain, deliveries_known=act["deliveries"])
            for k, f in (("blind", blind), ("told", told)):
                cover[k].append(f["finish"]["p10"] <= truth <= f["finish"]["p90"])
                err[k].append(f["finish"]["p50"] - truth)
            # the plan's own answer: remaining work at planned durations from today
            dur = np.where(st["done"], st["actual_duration"], np.where(st["started"], (world.NOW - st["start"]) + np.array([a["duration"] for a in w["activities"]]) * (1 - st["percent"]), [a["duration"] for a in w["activities"]]))
            es = np.where(st["started"], st["start"], np.maximum(engine.earliest_from_deliveries(w, {k: v["ordered_day"] + v["lead_days"] for k, v in w["procurement"].items()}), world.NOW))
            replan = float(engine.cpm(len(dur), w["dependencies"], dur, es)[1].max())
            err["plan"].append(replan - truth)
            fc_rows.append([seed, t, int(plan), int(truth), int(replan), f"{blind['finish']['p10']}–{blind['finish']['p50']}–{blind['finish']['p90']}", f"{told['finish']['p10']}–{told['finish']['p50']}–{told['finish']['p90']}", "yes" if cover["told"][-1] else "no"])
    n = len(fc_rows)
    mae = {k: np.mean(np.abs(v)) for k, v in err.items()}
    bias = {k: np.mean(v) for k, v in err.items()}
    doc = f"""# Evaluation

Everything here is measured on synthetic projects generated with seeds the demo does not use (`{SEEDS}`), each played out {len(TRUTHS)} different
ways (its own trade overruns, suppliers late at random, a wet spell at a random time), with the forecast made on day {world.NOW}. Reproduce with
`python -m cr.evaluate` (under a minute). The numbers say how well the methods do against the project as the simulator plays it, where the
past projects the models learn from come from the same process as the one being forecast. That is the generous case: on a real site the history
is from other buildings, other crews and other years, and the forecast would be less sure than this.

## 1. The finish forecast

Three answers for the finish day, scored against the real finish. "Re-plan" is what a scheduler gets from the schedule tool: remaining work at
planned durations from today. "Blind" is the Monte Carlo forecast knowing only the site status, which is what the demo shows first. "Told" is
the same forecast after the wet spell and the late deliveries are announced, which is what the demo shows after the scenario is applied.

{table(["", "mean error (days)", "bias (days, + is late)", "truth inside P10–P90"], [[k, round(mae[k], 1), round(bias[k], 1), f"{np.mean(cover[k]):.0%}" if k != "plan" else "no band"] for k in ("plan", "blind", "told")])}

Over {n} project-outcomes. Re-planning at planned durations is {round(bias['plan'], 0):.0f} days optimistic on average: the plan does not know that trades run late.
The blind forecast corrects most of that from history alone. Being told about the disruptions helped less than hoped: the mean error drops
{round(mae['blind'] - mae['told'], 1)} days and the bias moves from {round(bias['blind'], 1)} to {round(bias['told'], 1)}, because in most of these outcomes the late supplier and the rain fall on work with
float, and only the few where they hit the critical path (the outcomes where "told" moves 30 to 50 days) are improved. The told band covers the truth {np.mean(cover['told']):.0%} of
the time, which for an 80% band is {"about right" if 0.75 <= np.mean(cover['told']) <= 0.88 else "a little narrow" if np.mean(cover['told']) >= 0.6 else "too narrow"}: the trade spread is estimated from history, but the correlation between activities (a slow crew is slow on every floor) is not modelled, so the band is tighter than it should be.
The forecast is only as good as what the site tells it; the scenario step exists to make the telling cheap.

{table(["project (seed)", "outcome", "planned end", "real finish", "re-plan", "blind P10–P50–P90", "told P10–P50–P90", "told covers"], fc_rows)}

Days from the planned start.

## 2. Which activities run late

A gradient-boosted classifier (trade, size, floor, outdoor, wet season, fed by procurement, cost) for "more than 10% over plan", trained on sixteen
past projects and tested on four. The base rate is the share that run late anyway.

{table(["project (seed)", "test activities", "AUC", "base rate", "calibration, predicted→observed by bin"], model_rows)}

An AUC around 0.6 is weak: most of the overrun in this world is noise around a trade median, which the features cannot see. The classifier is used
only to rank the trade-performance risks in the register; the forecast itself uses the trade distributions, not this model.

## 3. What is not measured

- The site photo reader counts coloured rectangles in a schematic image. It is exact on those images and would read nothing from a real photo.
- The change-order impact is the same Monte Carlo with extra days added; its accuracy is the forecast's.
- The cost exposure (trade overruns at 60% labour share plus 18,000 a day of overhead past the planned end) is a stated formula, not a model.
"""
    OUT.write_text(doc)
    print(doc)


if __name__ == "__main__":
    main()

"""Critical-path scheduling, the delay models learned from past projects, Monte Carlo schedule and cost risk, and the toy
progress-image reader."""
import io

import numpy as np
from sklearn.ensemble import HistGradientBoostingClassifier

from . import world

VERSION = "schedule-risk-1"
LABOUR_SHARE = 0.6            # share of an activity's cost that grows with its duration


def cpm(n, deps, durations, earliest_start=None):
    """Forward and backward pass. -> start[n], finish[n], float[n], critical mask. Activities with no predecessor start at 0
    (or their earliest_start, such as a delivery day)."""
    succ, preds = [[] for _ in range(n)], [[] for _ in range(n)]
    for p, s, lag in deps:
        succ[p].append((s, lag))
        preds[s].append((p, lag))
    order = []
    indeg = [len(p) for p in preds]
    stack = [i for i in range(n) if indeg[i] == 0]
    while stack:
        i = stack.pop()
        order.append(i)
        for s, _ in succ[i]:
            indeg[s] -= 1
            if indeg[s] == 0:
                stack.append(s)
    es = np.zeros(n) if earliest_start is None else np.array(earliest_start, dtype=float)
    start, finish = es.copy(), np.zeros(n)
    for i in order:
        start[i] = max([es[i]] + [finish[p] + lag for p, lag in preds[i]])
        finish[i] = start[i] + durations[i]
    end = finish.max()
    lf = np.full(n, end)
    for i in reversed(order):
        if succ[i]:
            lf[i] = min(start[s] - lag for s, lag in succ[i])
    slack = lf - finish
    return start, finish, slack, slack < 0.5


def planned_end(w):
    """The baseline plan: planned durations and planned lead times."""
    acts = w["activities"]
    es = earliest_from_deliveries(w, {k: v["ordered_day"] + v["lead_days"] for k, v in w["procurement"].items()})
    return float(cpm(len(acts), w["dependencies"], np.array([a["duration"] for a in acts], dtype=float), es)[1].max())


def status_at(w, act, now):
    """What the site knows at `now` from how the project has really run: finished, in progress, not started; and the
    durations still planned for the rest. -> dict"""
    n = len(w["activities"])
    es = earliest_from_deliveries(w, act["deliveries"])
    dur = rain_adjusted(w, act["durations"].astype(float), act["rain"], es, w["dependencies"])
    start, finish, _, _ = cpm(n, w["dependencies"], dur, es)
    done, started = finish <= now, start <= now
    pct = np.where(done, 1.0, np.where(started, (now - start) / dur, 0.0)).clip(0, 1)
    return {"start": start, "finish": finish, "done": done, "started": started, "percent": pct, "actual_duration": dur, "true_finish": float(finish.max())}


def earliest_from_deliveries(w, deliveries):
    return np.array([deliveries.get(a["procurement"], 0) if a["procurement"] else 0 for a in w["activities"]], dtype=float)


def rain_adjusted(w, dur, rain, es, deps):
    """Outdoor work overlapping the wet window takes longer. Done once on the planned timeline (an approximation: the
    overlap is measured on a schedule without the rain)."""
    if not rain:
        return dur
    start, finish, _, _ = cpm(len(dur), deps, dur, es)
    a, b = rain
    overlap = np.clip(np.minimum(finish, b) - np.maximum(start, a), 0, None)
    out = dur.copy()
    for i, act in enumerate(w["activities"]):
        if act["outdoor"] and overlap[i] > 0:
            out[i] = dur[i] + overlap[i] * (1 / world.RAIN_FACTOR - 1)
    return out


def fit_trades(hist_acts):
    """Per trade: the log of actual/planned, its median and spread, from past projects."""
    out = {}
    for t in world.TRADES:
        ratio = np.log([h["actual"] / h["planned"] for h in hist_acts if h["trade"] == t])
        out[t] = {"median_ratio": round(float(np.exp(np.median(ratio))), 3), "log_sd": round(float(ratio.std()), 3), "n": int(len(ratio))}
    return out


def fit_procurement(hist_procs):
    """Per item: the ratios of actual to planned lead time, kept as an empirical distribution; the share delivered late."""
    out = {}
    for item in world.PROCUREMENT:
        ratios = np.array([h["actual"] / h["planned"] for h in hist_procs if h["item"] == item])
        out[item] = {"ratios": np.sort(ratios), "share_late": round(float((ratios > 1.0).mean()), 2), "p90_ratio": round(float(np.quantile(ratios, 0.9)), 2)}
    return out


def late_model(hist_acts):
    """Chance an activity runs more than 10% over its planned duration, from trade, size, floor, outdoor, season, procurement."""
    x = np.array([[list(world.TRADES).index(h["trade"]), h["planned"], h["floor"], h["outdoor"], h["wet_season"], h["procurement_fed"], h["cost"] / 1000] for h in hist_acts], dtype=float)
    y = np.array([h["actual"] > 1.1 * h["planned"] for h in hist_acts])
    cut = int(0.8 * len(y))
    m = HistGradientBoostingClassifier(max_iter=150, learning_rate=0.05, max_depth=4, categorical_features=[0], random_state=0).fit(x[:cut], y[:cut])
    p = m.predict_proba(x[cut:])[:, 1]
    bins = np.digitize(p, [0.2, 0.4, 0.6, 0.8])
    calib = [{"bin": k, "predicted": round(float(p[bins == k].mean()), 3), "observed": round(float(y[cut:][bins == k].mean()), 3), "n": int((bins == k).sum())} for k in range(5) if (bins == k).any()]
    from sklearn.metrics import roc_auc_score
    return {"model": m, "metrics": {"auc": round(float(roc_auc_score(y[cut:], p)), 3), "base_rate": round(float(y[cut:].mean()), 3), "trained_on": cut, "tested_on": len(y) - cut, "calibration": calib}}


def late_features(w, wet):
    return np.array([[list(world.TRADES).index(a["trade"]), a["duration"], a["floor"] if a["floor"] is not None else -1, a["outdoor"], wet, a["procurement"] is not None, a["cost"] / 1000] for a in w["activities"]], dtype=float)


def forecast(w, st, trades, procs, now, runs=2000, seed=0, rain=None, deliveries_known=None, extra_dur=None, overrides=None):
    """Monte Carlo over the rest of the project from `now`. Finished activities keep their actual durations; in-progress ones
    keep what is left of their plan; the rest draw from their trade's distribution (and the wet window, if announced).
    Procurement not yet delivered draws a lead-time ratio from history, or uses a known delivery day. -> dict"""
    r = np.random.default_rng(seed)
    n, acts = len(w["activities"]), w["activities"]
    planned = np.array([a["duration"] for a in acts], dtype=float)
    if extra_dur is not None:
        planned = planned + extra_dur
    finishes, costs, crit_count, starts_of = np.zeros(runs), np.zeros(runs), np.zeros(n), []
    for k in range(runs):
        dur = planned.copy()
        for i, a in enumerate(acts):
            if st["done"][i]:
                dur[i] = st["actual_duration"][i]
            elif st["started"][i]:
                dur[i] = (now - st["start"][i]) + planned[i] * (1 - st["percent"][i]) * np.exp(r.normal(np.log(trades[a["trade"]]["median_ratio"]), trades[a["trade"]]["log_sd"] / 2))
            else:
                dur[i] = planned[i] * np.exp(r.normal(np.log(trades[a["trade"]]["median_ratio"]), trades[a["trade"]]["log_sd"]))
        deliv = {}
        for item, v in w["procurement"].items():
            if deliveries_known and item in deliveries_known:
                deliv[item] = deliveries_known[item]
            else:
                deliv[item] = v["ordered_day"] + v["lead_days"] * float(r.choice(procs[item]["ratios"]))
        if overrides:
            deliv.update(overrides)
        es = earliest_from_deliveries(w, deliv)
        es = np.where(st["started"], st["start"], np.maximum(es, now * ~st["started"]))                  # nothing new starts before today
        dur = rain_adjusted(w, dur, rain, es, w["dependencies"])
        start, finish, slack, crit = cpm(n, w["dependencies"], dur, es)
        finishes[k] = finish.max()
        costs[k] = float(sum(a["cost"] * LABOUR_SHARE * (dur[i] / planned[i] - 1) for i, a in enumerate(acts) if not st["done"][i]))
        crit_count += crit
    plan_end = planned_end(w)
    p = lambda q: float(np.quantile(finishes, q))      # noqa: E731
    delay_cost = np.maximum(0, finishes - plan_end) * world.OVERHEAD_PER_DAY
    return {"runs": runs, "planned_end": int(plan_end), "finish": {"p10": round(p(0.1)), "p50": round(p(0.5)), "p90": round(p(0.9)), "mean": round(float(finishes.mean()), 1)}, "chance_on_time": round(float((finishes <= plan_end).mean()), 3),
            "cost_exposure": {"p50": round(float(np.quantile(costs + delay_cost, 0.5))), "p80": round(float(np.quantile(costs + delay_cost, 0.8))), "overhead_per_day": world.OVERHEAD_PER_DAY, "what": "trade overruns on remaining work plus overhead for days past the planned end"},
            "critical_share": crit_count / runs, "samples": finishes}


def expected_path(w, st, trades, now, rain=None, deliveries=None, extra_dur=None):
    """The critical path on median durations: which activities, in order, with float for the rest."""
    n, acts = len(w["activities"]), w["activities"]
    planned = np.array([a["duration"] for a in acts], dtype=float) + (extra_dur if extra_dur is not None else 0)
    dur = np.where(st["done"], st["actual_duration"], np.where(st["started"], (now - st["start"]) + planned * (1 - st["percent"]), planned * np.array([trades[a["trade"]]["median_ratio"] for a in acts])))
    es = earliest_from_deliveries(w, deliveries or {})
    es = np.where(st["started"], st["start"], np.maximum(es, now))
    dur = rain_adjusted(w, dur, rain, es, w["dependencies"])
    start, finish, slack, crit = cpm(n, w["dependencies"], dur, es)
    path = [i for i in np.argsort(start) if crit[i] and not st["done"][i]]
    return {"end": round(float(finish.max())), "path": [int(i) for i in path], "start": start, "finish": finish, "float": slack}


def read_facade(png_bytes, cols=8):
    """The toy image reader: count glazed (blue) panels in a schematic elevation. Not a vision model, and says so."""
    from PIL import Image
    img = np.array(Image.open(io.BytesIO(png_bytes)).convert("RGB"), dtype=int)
    h, w_ = img.shape[:2]
    rows = (h - 20) // 30
    installed = total = 0
    for k in range(rows * cols):
        x, y = 10 + (k % cols) * 40, 10 + (k // cols) * 30
        patch = img[y + 8:y + 22, x + 8:x + 32]
        blue = patch[..., 2].mean() - patch[..., 0].mean()
        total += 1
        installed += blue > 60
    return {"panels": total, "installed": installed, "completion": round(installed / total, 3) if total else None, "confidence": 0.9 if total else 0.0, "method": "counts blue (glazed) panels in a schematic elevation; a toy reader, not a vision model"}

"""Unit tests for the simulator, CPM, the fitted models, the forecast and the toy photo reader: no database."""
import numpy as np

from cr import engine, world

W = world.build()
ACT = world.actuals(W, 1)


def test_world_is_deterministic_and_the_plan_is_a_dag():
    assert world.build()["activities"] == W["activities"] and len(W["activities"]) == 600 and len(W["dependencies"]) > 600
    n = len(W["activities"])
    start, finish, slack, crit = engine.cpm(n, W["dependencies"], np.array([a["duration"] for a in W["activities"]], float))
    assert all(start[s] >= finish[p] + lag for p, s, lag in W["dependencies"]) and crit.sum() >= 10 and (slack >= -1e-9).all()
    assert 280 <= engine.planned_end(W) <= 340 and world.actuals(W, 1)["deliveries"] == ACT["deliveries"] and ACT["deliveries"]["switchgear"] > 150 + world.LATE["switchgear"] - 30


def test_cpm_hand_case():
    # 0 -> 1 -> 3, 0 -> 2 -> 3, durations 2,5,1,2: path 0-1-3 is critical, 2 has float 4
    start, finish, slack, crit = engine.cpm(4, [(0, 1, 0), (0, 2, 0), (1, 3, 0), (2, 3, 0)], np.array([2, 5, 1, 2], float))
    assert list(finish) == [2, 7, 3, 9] and list(crit) == [True, True, False, True] and slack[2] == 4
    start, _, _, _ = engine.cpm(2, [(0, 1, 3)], np.array([1, 1], float), [10, 0])
    assert list(start) == [10, 14]                                   # earliest start and a lag


def test_status_at_splits_done_in_progress_and_to_come():
    st = engine.status_at(W, ACT, world.NOW)
    assert st["done"].sum() > 50 and 5 <= (st["started"] & ~st["done"]).sum() <= 30 and (~st["started"]).sum() > 450
    assert ((st["percent"] == 1) == st["done"]).all() and (st["percent"][st["started"] & ~st["done"]] < 1).all() and st["true_finish"] > engine.planned_end(W)


def test_models_fit_and_the_forecast_covers_the_truth():
    ha, hp = world.history(W)
    trades, procs, late = engine.fit_trades(ha), engine.fit_procurement(hp), engine.late_model(ha)
    assert all(1.0 < v["median_ratio"] < 1.6 for v in trades.values()) and all(0.3 < v["share_late"] < 1 for v in procs.values()) and late["metrics"]["auc"] > 0.55
    st = engine.status_at(W, ACT, world.NOW)
    blind = engine.forecast(W, st, trades, procs, world.NOW, runs=200)
    told = engine.forecast(W, st, trades, procs, world.NOW, runs=200, rain=ACT["rain"], deliveries_known=ACT["deliveries"])
    assert blind["finish"]["p10"] < blind["finish"]["p50"] < blind["finish"]["p90"] and blind["chance_on_time"] == 0 and len(blind["samples"]) == 200
    assert told["finish"]["p10"] <= st["true_finish"] <= told["finish"]["p90"] and told["finish"]["p50"] > blind["finish"]["p50"]        # knowing the disruptions moves the forecast towards the truth
    extra = np.zeros(600)
    extra[[i for i, a in enumerate(W["activities"]) if a["trade"] == "finishes"]] = 3
    assert engine.forecast(W, st, trades, procs, world.NOW, runs=200, extra_dur=extra)["finish"]["p50"] > blind["finish"]["p50"]
    path = engine.expected_path(W, st, trades, world.NOW)
    assert path["path"] and not any(st["done"][i] for i in path["path"]) and path["end"] >= blind["finish"]["p10"] - 40


def test_rain_only_slows_outdoor_work_in_the_window():
    dur = np.array([a["duration"] for a in W["activities"]], float)
    es = np.zeros(600)
    wet = engine.rain_adjusted(W, dur, (0, 400), es, W["dependencies"])
    dry = engine.rain_adjusted(W, dur, None, es, W["dependencies"])
    outdoor = np.array([world.TRADES[a["trade"]][2] for a in W["activities"]])
    assert (dry == dur).all() and (wet[outdoor] > dur[outdoor]).all() and (wet[~outdoor] == dur[~outdoor]).all()


def test_facade_reader_counts_panels():
    for k in (0, 12, 36, 48):
        r = engine.read_facade(world.facade_image(k))
        assert r["panels"] == 48 and r["installed"] == k and r["completion"] == round(k / 48, 3), k
    assert engine.read_facade(world.facade_image(48, total=24, cols=8))["installed"] == 24

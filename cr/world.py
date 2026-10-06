"""An invented mid-rise building project: 600 activities over about a year, in phases and floors, with finish-to-start
dependencies, trades, costs, outdoor flags and procurement items feeding them; twenty past projects of the same kind for the
models to learn from; and what really happens on this one, including a late steel delivery and a wet month.
Day 0 is the planned start. Everything is a pure function of a seed."""
import functools

import numpy as np

FLOORS = 8
NOW = 180
TRADES = {"civil": (1.10, 0.30, True), "concrete": (1.08, 0.22, True), "steel": (1.12, 0.28, True), "envelope": (1.15, 0.30, True), "mechanical": (1.12, 0.25, False), "electrical": (1.08, 0.20, False), "finishes": (1.18, 0.30, False), "commissioning": (1.25, 0.35, False)}
# trade -> (median actual/planned duration ratio, log-sd, outdoor)
PROCUREMENT = {"structural steel": {"lead_days": 90, "feeds": "steel", "cost": 2_400_000}, "curtain wall": {"lead_days": 120, "feeds": "envelope", "cost": 1_900_000}, "switchgear": {"lead_days": 150, "feeds": "electrical", "cost": 650_000}, "elevators": {"lead_days": 180, "feeds": "commissioning", "cost": 820_000}}
OVERHEAD_PER_DAY = 18_000
RAIN_FACTOR, RAIN_WINDOW = 0.7, (185, 206)        # a wet three weeks ahead: outdoor work at 70% productivity
LATE = {"structural steel": 21, "switchgear": 40}      # what really happens to two deliveries


def _schedule(r):
    """The activity network. -> activities [dict], dependencies [(pred, succ, lag)]"""
    acts, deps = [], []

    def add(name, trade, dur, cost, floor=None, preds=(), lag=0):
        a = {"index": len(acts), "name": name, "trade": trade, "duration": int(max(2, round(dur))), "cost": int(cost), "floor": floor, "outdoor": TRADES[trade][2]}
        acts.append(a)
        deps.extend((p, a["index"], lag) for p in preds)
        return a["index"]

    site = [add(f"Site works {k + 1}", "civil", r.uniform(6, 10), r.uniform(60, 120) * 1000) for k in range(6)]
    for i in range(2, 6):
        deps.append((site[i - 2], site[i], 0))                                          # two crews
    found = [add(f"Foundations zone {k + 1}", "concrete", r.uniform(8, 14), r.uniform(150, 260) * 1000, 0, preds=site[-2:] if k < 3 else []) for k in range(6)]
    for i in range(3, 6):
        deps.append((found[i - 3], found[i], 0))                                        # three zones at a time
    prev = found[-3:]
    cores = []
    for f in range(FLOORS):
        steel = [add(f"Steel erection L{f + 1} bay {b + 1}", "steel", r.uniform(4, 7), r.uniform(90, 160) * 1000, f, preds=prev) for b in range(5)]
        deck = [add(f"Deck and slab L{f + 1} bay {b + 1}", "concrete", r.uniform(3, 6), r.uniform(70, 120) * 1000, f, preds=[steel[b]]) for b in range(5)]
        cores.append(add(f"Core walls L{f + 1}", "concrete", r.uniform(6, 9), r.uniform(120, 180) * 1000, f, preds=deck))
        prev = deck
    prev_env, prev_mech, prev_fin = [], [], []
    for f in range(FLOORS):
        env = [add(f"Curtain wall L{f + 1} elevation {e}", "envelope", r.uniform(6, 10), r.uniform(110, 190) * 1000, f, preds=[cores[f]] + prev_env[k:k + 1]) for k, e in enumerate("NESW")]
        mech = [add(f"Mechanical rough-in L{f + 1} zone {z + 1}", "mechanical", r.uniform(8, 14), r.uniform(90, 150) * 1000, f, preds=[cores[f]] + prev_mech[z:z + 1]) for z in range(4)]
        elec = [add(f"Electrical rough-in L{f + 1} zone {z + 1}", "electrical", r.uniform(6, 12), r.uniform(70, 120) * 1000, f, preds=[mech[z]]) for z in range(4)]
        fin = [add(f"Drywall and finishes L{f + 1} zone {z + 1}", "finishes", r.uniform(10, 18), r.uniform(120, 200) * 1000, f, preds=[elec[z], env[z]] + prev_fin[z:z + 1]) for z in range(4)]
        for z in range(4):
            add(f"Fit-out L{f + 1} zone {z + 1}", "finishes", r.uniform(8, 14), r.uniform(90, 160) * 1000, f, preds=[fin[z]])
        prev_env, prev_mech, prev_fin = env, mech, fin                                   # crews move up floor by floor
    last_fin = [a["index"] for a in acts if a["trade"] == "finishes"][-8:]
    com = [add(f"Commissioning package {k + 1}", "commissioning", r.uniform(8, 16), r.uniform(60, 140) * 1000, None, preds=last_fin[k % 8:k % 8 + 1]) for k in range(10)]
    add("Handover", "commissioning", 5, 50_000, None, preds=com)
    while len(acts) < 600:
        f = int(r.integers(0, FLOORS))
        add(f"Punch list L{f + 1} item {len(acts)}", "finishes", r.uniform(2, 5), r.uniform(5, 20) * 1000, f, preds=[com[f % 10]])
    return acts, deps


@functools.lru_cache(maxsize=8)
def build(seed=17):
    r = np.random.default_rng(seed)
    acts, deps = _schedule(r)
    for a in acts:
        a["procurement"] = next((k for k, v in PROCUREMENT.items() if v["feeds"] == a["trade"] and (a["floor"] in (None, 0) or k == "structural steel")), None)
    return {"seed": seed, "activities": acts, "dependencies": deps, "budget": int(sum(a["cost"] for a in acts) * 1.08), "procurement": {k: {**v, "ordered_day": 0, "need_day": None} for k, v in PROCUREMENT.items()}}


def actuals(w, seed, late=None, rain=RAIN_WINDOW):
    """What really happens: each activity's actual duration, each procurement item's actual delivery day."""
    r = np.random.default_rng([w["seed"], seed])
    dur = np.array([a["duration"] * np.exp(r.normal(np.log(TRADES[a["trade"]][0]), TRADES[a["trade"]][1])) * (1 + 0.025 * (a["floor"] or 0)) * (1.12 if a["procurement"] else 1) for a in w["activities"]])      # the same process the past projects came from
    late = LATE if late is None else late
    deliver = {k: int(v["lead_days"] * np.exp(r.normal(0.05, 0.15))) + late.get(k, 0) for k, v in w["procurement"].items()}
    return {"durations": np.maximum(1, dur.round()).astype(int), "deliveries": deliver, "rain": rain}


def history(w, n=20):
    """Past projects: for each activity, planned and actual duration and the circumstances. Also procurement planned vs actual lead."""
    acts, procs = [], []
    for k in range(n):
        r = np.random.default_rng([w["seed"], 500 + k])
        wet = r.random() < 0.35
        for a in w["activities"]:
            med, sd, outdoor = TRADES[a["trade"]]
            factor = np.exp(r.normal(np.log(med), sd)) * (1 / RAIN_FACTOR if (wet and outdoor and r.random() < 0.3) else 1) * (1 + 0.025 * (a["floor"] or 0)) * (1.12 if a["procurement"] else 1)      # higher floors and fed trades run later
            acts.append({"trade": a["trade"], "planned": a["duration"], "outdoor": outdoor, "floor": a["floor"] if a["floor"] is not None else -1, "wet_season": wet, "procurement_fed": a["procurement"] is not None, "cost": a["cost"], "actual": max(1, round(a["duration"] * factor))})
        for name, v in w["procurement"].items():
            procs.append({"item": name, "planned": v["lead_days"], "actual": int(v["lead_days"] * np.exp(r.normal(0.05, 0.15)) + (r.exponential(20) if r.random() < 0.25 else 0))})
    return acts, procs


def facade_image(installed, total=48, cols=8, seed=0):
    """A schematic elevation: a grid of curtain-wall panels, the installed ones glazed. -> PNG bytes"""
    import io

    from PIL import Image, ImageDraw
    rows = total // cols
    img = Image.new("RGB", (cols * 40 + 20, rows * 30 + 20), (90, 90, 95))
    d = ImageDraw.Draw(img)
    r = np.random.default_rng(seed)
    for k in range(total):
        x, y = 10 + (k % cols) * 40, 10 + (k // cols) * 30
        col = (70, 130, 190) if k < installed else (60, 60, 62)
        d.rectangle([x + 2, y + 2, x + 38, y + 28], fill=tuple(int(c + r.normal(0, 4)) for c in col), outline=(30, 30, 30))
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()

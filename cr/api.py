"""Public API (modular monolith). Blueprint services map to: schedule-ingestion, bim-service, progress-vision-service (a toy
image reader that says so), risk-model-service, critical-path-engine, change-order-service, scenario service and the
project-control API. Not built: Primavera/IFC adapters, weather feeds, a document RAG.

    uvicorn cr.api:app          python -m core.jobs cr.api      # the worker
"""
import base64
import datetime
import functools
import hashlib
import uuid
from typing import Literal

import numpy as np
from fastapi import Depends, Header, Query
from fastapi.encoders import jsonable_encoder
from psycopg.types.json import Jsonb
from pydantic import BaseModel, Field

from core import audit, db, jobs
from core.app import Ctx, Problem, create_app, run

from . import engine, world

READ = {"project:read", "jobs:read"}
WORK = READ | {"schedule:write", "progress:write", "scenarios:run", "change_orders:write"}
PERMISSIONS = {"viewer": READ, "scheduler": WORK, "director": WORK | {"project:load", "change_orders:approve", "audit:read"}}
app = create_app("construction-risk", PERMISSIONS)
auth = app.state.auth
IdemKey = Header(None, alias="Idempotency-Key")
START = datetime.date(2026, 6, 1)
TRUTH_SEED = 1
RUNS = 1000


def day(d):
    return START + datetime.timedelta(days=int(round(d)))


def aid(t, i):
    return uuid.uuid5(t, f"activity-{i}")


def worker_ctx(job):
    return Ctx(job["tenant_id"], uuid.UUID(job["payload"]["actor_id"]), "worker", "system")


def project(c):
    p = c.execute("SELECT * FROM projects").fetchone()
    if not p:
        raise Problem(409, "no_project", "load a project first: POST /v1/project:load")
    return p, world.build(p["model_seed"])


@functools.lru_cache(maxsize=4)
def models(seed):
    """Fitted once per generator seed: trade distributions, procurement lead ratios, the late-activity classifier."""
    ha, hp = world.history(world.build(seed))
    return {"trades": engine.fit_trades(ha), "procs": engine.fit_procurement(hp), "late": engine.late_model(ha)}


def status(c, p, w):
    """Site status from storage: finished and in-progress activities with their percentages, deliveries known, disruptions announced."""
    rows = c.execute("SELECT ordinal, percent_complete, status, actual_start, actual_end FROM activities ORDER BY ordinal").fetchall()
    n = len(rows)
    st = {"done": np.array([r["status"] == "finished" for r in rows]), "started": np.array([r["status"] in ("finished", "in_progress") for r in rows]), "percent": np.array([float(r["percent_complete"]) for r in rows]),
          "start": np.array([(r["actual_start"] - START).days if r["actual_start"] else 0 for r in rows], dtype=float), "actual_duration": np.zeros(n)}
    for i, r in enumerate(rows):
        if r["actual_end"]:
            st["actual_duration"][i] = (r["actual_end"] - r["actual_start"]).days
    known = {r["item"]: r["delivered_day"] for r in c.execute("SELECT item, delivered_day FROM procurement_items WHERE delivered_day IS NOT NULL")}
    rain, notices = None, {}
    for d in c.execute("SELECT kind, payload FROM disruptions ORDER BY created_at"):
        if d["kind"] == "weather":
            rain = (d["payload"]["from_day"], d["payload"]["to_day"])
        else:
            notices[d["payload"]["item"]] = d["payload"]["expected_day"]
    return st, known, rain, notices


_FORECASTS = {}


def run_forecast(w, st, known, rain, notices, now, runs=RUNS, extra=None, seed=0):
    """The Monte Carlo is deterministic for a state of knowledge, so each state is simulated once. ponytail: a dict capped at 64 entries, a shared cache if there are many workers"""
    key = hashlib.sha1(b"".join([str((w["seed"], sorted(known.items()), rain, sorted(notices.items()), now, runs, seed)).encode(), st["done"].tobytes(), st["started"].tobytes(), st["percent"].round(4).tobytes(), st["start"].tobytes(), st["actual_duration"].tobytes(), extra.tobytes() if extra is not None else b""])).hexdigest()
    if key not in _FORECASTS:
        if len(_FORECASTS) >= 64:
            _FORECASTS.pop(next(iter(_FORECASTS)))
        m = models(w["seed"])
        _FORECASTS[key] = engine.forecast(w, st, m["trades"], m["procs"], now, runs=runs, seed=seed, rain=rain, deliveries_known={**notices, **known}, extra_dur=extra)
    return _FORECASTS[key]


def describe(fc):
    return {"planned_end": str(day(fc["planned_end"])), "finish": {k: str(day(v)) if k != "mean" else str(day(v)) for k, v in fc["finish"].items()}, "finish_day": fc["finish"], "days_late_p50": round(fc["finish"]["p50"] - fc["planned_end"]), "chance_on_time": fc["chance_on_time"], "cost_exposure": fc["cost_exposure"]}


# ---------------------------------------------------------------- project

class LoadIn(BaseModel):
    seed: int = Field(17, ge=0, le=10 ** 6)


@app.post("/v1/project:load", status_code=202, tags=["project"], summary="(+) Load a synthetic building project at day 120: 600 activities, dependencies, BIM elements, procurement, site status; fit the models on 20 past projects")
def load_project(body: LoadIn, ctx: Ctx = Depends(auth("project:load")), idem: str | None = IdemKey):
    def work(c):
        if c.execute("SELECT 1 FROM projects LIMIT 1").fetchone():
            raise Problem(409, "project_exists")
        return 202, jobs.enqueue(c, ctx, "project.load", body.model_dump())
    return run(ctx, idem, body, work)


@jobs.handler("project.load")
def load_job(c, job):
    t, w, now = job["tenant_id"], world.build(job["payload"]["seed"]), world.NOW
    pid = uuid.uuid5(t, "project")
    planned_end = engine.planned_end(w)
    c.execute("INSERT INTO projects (id, tenant_id, name, start_date, planned_end, budget, model_seed, now_day) VALUES (%s,%s,'Riverbend Tower, 8 storeys',%s,%s,%s,%s,%s)", [pid, t, START, day(planned_end), w["budget"], w["seed"], now])
    act = world.actuals(w, TRUTH_SEED)
    st = engine.status_at(w, act, now)
    plan_es = engine.earliest_from_deliveries(w, {k: v["ordered_day"] + v["lead_days"] for k, v in w["procurement"].items()})
    ps, pf, _, _ = engine.cpm(len(w["activities"]), w["dependencies"], np.array([a["duration"] for a in w["activities"]], float), plan_es)
    rows = []
    for i, a in enumerate(w["activities"]):
        state = "finished" if st["done"][i] else "in_progress" if st["started"][i] else "not_started"
        rows.append((aid(t, i), t, pid, f"A{i + 1:04d}", a["name"], day(ps[i]), day(pf[i]), round(float(st["percent"][i]), 3), f"{a['trade'][:3].upper()}-{(a['floor'] or 0) + 1:02d}", i, a["trade"], a["duration"], a["cost"], a["floor"], a["outdoor"], a["procurement"], state,
                     day(st["start"][i]) if st["started"][i] else None, day(st["finish"][i]) if st["done"][i] else None))
    db.load(c, "activities", ["id", "tenant_id", "project_id", "external_id", "name", "start_planned", "end_planned", "percent_complete", "cost_code", "ordinal", "trade", "duration_days", "cost", "floor", "outdoor", "procurement_item", "status", "actual_start", "actual_end"], rows)
    db.load(c, "dependencies", ["predecessor_id", "successor_id", "relation", "lag_days", "tenant_id"], [(aid(t, p), aid(t, s), "FS", lag, t) for p, s, lag in w["dependencies"]])
    elements = [(uuid.uuid5(t, f"panel-{i}-{k}"), t, pid, f"CW-{i}-{k:02d}", "curtain wall panel", f"L{a['floor'] + 1}", None, aid(t, i)) for i, a in enumerate(w["activities"]) if a["trade"] == "envelope" for k in range(12)]
    db.load(c, "bim_elements", ["id", "tenant_id", "project_id", "guid", "element_type", "level", "geom_ref", "activity_id"], elements)
    db.load(c, "procurement_items", ["id", "tenant_id", "project_id", "item", "lead_days", "ordered_day", "expected_day", "delivered_day", "cost", "feeds_trade"],
            [(uuid.uuid5(t, f"proc-{k}"), t, pid, k, v["lead_days"], v["ordered_day"], v["ordered_day"] + v["lead_days"], act["deliveries"][k] if act["deliveries"][k] <= now else None, v["cost"], v["feeds"]) for k, v in w["procurement"].items()])
    m = models(w["seed"])
    write_risks(c, t, pid, w, st, m, now)
    out = {"project_id": pid, "name": "Riverbend Tower, 8 storeys", "day": now, "date": str(day(now)), "planned_end": str(day(planned_end)), "budget": w["budget"], "activities": len(rows), "dependencies": len(w["dependencies"]), "bim_elements": len(elements),
           "status": {"finished": int(st["done"].sum()), "in_progress": int(st["started"].sum() - st["done"].sum()), "not_started": int((~st["started"]).sum())}, "procurement": [{"item": k, "planned_delivery": str(day(v["lead_days"])), "delivered": str(day(act["deliveries"][k])) if act["deliveries"][k] <= now else None} for k, v in w["procurement"].items()],
           "models": {"trades": m["trades"], "procurement_late_share": {k: v["share_late"] for k, v in m["procs"].items()}, "late_activity_classifier": m["late"]["metrics"]}, "simulation_truth": {"what": "when this project really finishes, which the models never see", "finish": str(day(st["true_finish"])), "days_late": round(st["true_finish"] - planned_end)}}
    audit.record(c, worker_ctx(job), "project.loaded", "project", pid, {"activities": len(rows), "day": now})
    return out


def write_risks(c, t, pid, w, st, m, now):
    """The risk register, recomputed: procurement items not yet delivered, outdoor work on the critical path, trades that run late."""
    c.execute("DELETE FROM risks WHERE project_id = %s", [pid])
    rows = []
    for item, v in w["procurement"].items():
        delivered = c.execute("SELECT delivered_day FROM procurement_items WHERE item = %s", [item]).fetchone()["delivered_day"]
        if delivered is None:
            ratios = m["procs"][item]["ratios"]
            planned_day = v["ordered_day"] + v["lead_days"]
            p_late = float(np.mean(v["ordered_day"] + v["lead_days"] * ratios > max(planned_day, now)))
            days = float(np.quantile(v["lead_days"] * ratios, 0.8) - v["lead_days"])
            rows.append((uuid.uuid7(), t, pid, "procurement", round(p_late, 3), round(days * world.OVERHEAD_PER_DAY), round(max(0, days)), Jsonb({"planned_delivery": str(day(planned_day)), "history_late_share": m["procs"][item]["share_late"], "p80_lead_ratio": m["procs"][item]["p90_ratio"], "feeds": v["feeds"]}), item))
    x = engine.late_features(w, False)
    p_late = m["late"]["model"].predict_proba(x)[:, 1]
    for trade in world.TRADES:
        idx = [i for i, a in enumerate(w["activities"]) if a["trade"] == trade and not st["done"][i]]
        if idx:
            med = m["trades"][trade]["median_ratio"]
            cost = sum(w["activities"][i]["cost"] for i in idx)
            rows.append((uuid.uuid7(), t, pid, "trade_performance", round(float(p_late[idx].mean()), 3), round(cost * engine.LABOUR_SHARE * (med - 1)), round(sum(w["activities"][i]["duration"] for i in idx) * (med - 1) / 4), Jsonb({"activities_remaining": len(idx), "median_duration_ratio": med, "outdoor": world.TRADES[trade][2]}), trade))
    db.load(c, "risks", ["id", "tenant_id", "project_id", "risk_type", "probability", "impact_cost", "impact_days", "evidence", "subject"], rows)


class ScheduleRow(BaseModel):
    external_id: str = Field(pattern=r"^A\d{4}$")
    percent_complete: float = Field(ge=0, le=1)
    actual_start: datetime.date | None = None
    actual_end: datetime.date | None = None


class ScheduleIn(BaseModel):
    source: str = Field("P6 export", max_length=40)
    activities: list[ScheduleRow] = Field(min_length=1, max_length=600)


@app.post("/v1/projects/{project_id}/schedule/import", tags=["schedule"], summary="Import a schedule update: percent complete and actual dates per activity, as a Primavera export would carry them")
def import_schedule(project_id: uuid.UUID, body: ScheduleIn, ctx: Ctx = Depends(auth("schedule:write")), idem: str | None = IdemKey):
    def work(c):
        p, w = project(c)
        if p["id"] != project_id:
            raise Problem(404, "project_not_found")
        updated = 0
        for r in body.activities:
            a = c.execute("SELECT id, status, percent_complete FROM activities WHERE external_id = %s", [r.external_id]).fetchone()
            if not a:
                raise Problem(404, "activity_not_found", r.external_id)
            if r.actual_end and not r.actual_start:
                raise Problem(422, "bad_dates", f"{r.external_id}: an actual end needs an actual start")
            if a["status"] == "finished" and r.percent_complete < 1:
                raise Problem(409, "already_finished", f"{r.external_id} is finished; it cannot go backwards")
            state = "finished" if r.percent_complete >= 1 or r.actual_end else "in_progress" if r.percent_complete > 0 or r.actual_start else "not_started"
            c.execute("UPDATE activities SET percent_complete = %s, status = %s, actual_start = coalesce(%s, actual_start, CASE WHEN %s > 0 THEN %s END), actual_end = coalesce(%s, actual_end, CASE WHEN %s >= 1 THEN %s END) WHERE id = %s",
                      [r.percent_complete, state, r.actual_start, r.percent_complete, day(p["now_day"]), r.actual_end, r.percent_complete, day(p["now_day"]), a["id"]])
            updated += 1
        audit.record(c, ctx, "schedule.imported", "project", project_id, {"source": body.source, "activities": updated})
        return 200, {"project_id": project_id, "source": body.source, "updated": updated, "status": {r["status"]: r["n"] for r in c.execute("SELECT status, count(*) AS n FROM activities GROUP BY status")}}
    return run(ctx, idem, body, work)


class BimElement(BaseModel):
    guid: str = Field(max_length=60)
    element_type: str = Field(max_length=40)
    level: str = Field(max_length=10)
    activity_external_id: str | None = None
    geom_ref: str | None = None


class BimIn(BaseModel):
    source: str = Field("IFC export", max_length=40)
    elements: list[BimElement] = Field(min_length=1, max_length=5000)


@app.post("/v1/projects/{project_id}/bim/import", tags=["bim"], summary="Import BIM elements and link them to activities; an element already known is updated")
def import_bim(project_id: uuid.UUID, body: BimIn, ctx: Ctx = Depends(auth("schedule:write")), idem: str | None = IdemKey):
    def work(c):
        p, w = project(c)
        if p["id"] != project_id:
            raise Problem(404, "project_not_found")
        added = updated = 0
        for e in body.elements:
            act = c.execute("SELECT id FROM activities WHERE external_id = %s", [e.activity_external_id]).fetchone() if e.activity_external_id else None
            if e.activity_external_id and not act:
                raise Problem(404, "activity_not_found", e.activity_external_id)
            r = c.execute("INSERT INTO bim_elements (id, tenant_id, project_id, guid, element_type, level, geom_ref, activity_id) VALUES (%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT (project_id, guid) DO UPDATE SET element_type = EXCLUDED.element_type, level = EXCLUDED.level, activity_id = EXCLUDED.activity_id RETURNING (xmax = 0) AS inserted",
                          [uuid.uuid7(), ctx.tenant_id, project_id, e.guid, e.element_type, e.level, e.geom_ref, act and act["id"]]).fetchone()
            added, updated = added + r["inserted"], updated + (not r["inserted"])
        audit.record(c, ctx, "bim.imported", "project", project_id, {"source": body.source, "added": added, "updated": updated})
        return 200, {"project_id": project_id, "added": added, "updated": updated, "elements": c.execute("SELECT count(*) AS n FROM bim_elements").fetchone()["n"], "linked_to_activities": c.execute("SELECT count(*) AS n FROM bim_elements WHERE activity_id IS NOT NULL").fetchone()["n"]}
    return run(ctx, idem, body, work)


# ---------------------------------------------------------------- forecast, critical path, risks

@app.get("/v1/projects/{project_id}/forecast", tags=["forecast"], summary="Monte Carlo over the rest of the project: finish P10/P50/P90, chance of the planned date, cost exposure, the expected critical path and the top risks")
def forecast(project_id: uuid.UUID, runs: int = Query(RUNS, ge=100, le=5000), ctx: Ctx = Depends(auth("project:read"))):
    with db.tx(ctx.tenant_id) as c:
        p, w = project(c)
        if p["id"] != project_id:
            raise Problem(404, "project_not_found")
        st, known, rain, notices = status(c, p, w)
        risks = c.execute("SELECT risk_type, subject, probability, impact_cost, impact_days, evidence FROM risks ORDER BY probability * impact_days DESC LIMIT 6").fetchall()
    fc = run_forecast(w, st, known, rain, notices, p["now_day"], runs)
    path = engine.expected_path(w, st, models(w["seed"])["trades"], p["now_day"], rain=rain, deliveries={**notices, **known})
    truth = engine.status_at(w, world.actuals(w, TRUTH_SEED), p["now_day"])["true_finish"]
    acts = w["activities"]
    return jsonable_encoder({"project_id": project_id, "day": p["now_day"], "date": str(day(p["now_day"])), **describe(fc), "runs": runs, "disruptions_in_force": {"weather": rain and {"from": str(day(rain[0])), "to": str(day(rain[1]))}, "procurement_notices": {k: str(day(v)) for k, v in notices.items()}},
                             "critical_path": {"expected_end": str(day(path["end"])), "activities": [{"external_id": f"A{i + 1:04d}", "name": acts[i]["name"], "trade": acts[i]["trade"], "start": str(day(path["start"][i])), "finish": str(day(path["finish"][i])), "share_of_runs_critical": round(float(fc["critical_share"][i]), 2)} for i in path["path"][:14]], "length": len(path["path"])},
                             "finish_histogram": np.histogram(fc["samples"], bins=12)[0].tolist(), "finish_bins": [str(day(b)) for b in np.histogram(fc["samples"], bins=12)[1]], "risks": risks,
                             "simulation_truth": {"what": "when the project really finishes", "finish": str(day(truth)), "inside_p10_p90": bool(fc["finish"]["p10"] <= truth <= fc["finish"]["p90"])},
                             "how": f"{runs} runs; finished activities keep their actual durations, in-progress ones what is left of their plan, the rest draw from their trade's history; procurement not delivered draws a lead-time ratio from history unless a notice fixes it; announced wet weather slows outdoor work"})


@app.get("/v1/projects/{project_id}/critical-path", tags=["forecast"], summary="The critical path on median durations, and float for every remaining activity")
def critical_path(project_id: uuid.UUID, ctx: Ctx = Depends(auth("project:read"))):
    with db.tx(ctx.tenant_id) as c:
        p, w = project(c)
        if p["id"] != project_id:
            raise Problem(404, "project_not_found")
        st, known, rain, notices = status(c, p, w)
    path = engine.expected_path(w, st, models(w["seed"])["trades"], p["now_day"], rain=rain, deliveries={**notices, **known})
    acts = w["activities"]
    remaining = [i for i in range(len(acts)) if not st["done"][i]]
    return jsonable_encoder({"project_id": project_id, "expected_end": str(day(path["end"])), "critical": [{"external_id": f"A{i + 1:04d}", "name": acts[i]["name"], "trade": acts[i]["trade"], "start": str(day(path["start"][i])), "finish": str(day(path["finish"][i]))} for i in path["path"]],
                             "float_days": {"zero": int(sum(path["float"][i] < 0.5 for i in remaining)), "under_a_week": int(sum(0.5 <= path["float"][i] < 7 for i in remaining)), "a_week_or_more": int(sum(path["float"][i] >= 7 for i in remaining))}, "remaining_activities": len(remaining)})


@app.get("/v1/projects/{project_id}/risks", tags=["forecast"], summary="(+) The risk register: procurement, outdoor work, trade performance")
def risks(project_id: uuid.UUID, ctx: Ctx = Depends(auth("project:read"))):
    with db.tx(ctx.tenant_id) as c:
        project(c)
        rows = c.execute("SELECT risk_type, subject, probability, impact_cost, impact_days, evidence, computed_at FROM risks ORDER BY risk_type, probability DESC").fetchall()
    return jsonable_encoder({"project_id": project_id, "items": rows})


# ---------------------------------------------------------------- scenarios and disruptions

class Event(BaseModel):
    kind: Literal["procurement_delay", "weather"]
    item: str | None = None
    expected_day: int | None = Field(None, ge=0, le=1000)
    from_day: int | None = Field(None, ge=0, le=1000)
    to_day: int | None = Field(None, ge=0, le=1000)


class ScenarioIn(BaseModel):
    events: list[Event] = Field(min_length=1, max_length=5)
    apply: bool = Field(False, description="Record the events as announced, so every forecast from now on includes them")
    runs: int = Field(RUNS, ge=100, le=5000)


@app.post("/v1/scenarios", status_code=201, tags=["scenarios"], summary="What a late delivery or a wet spell does: the forecast before and after, and the critical-path shift. With apply, the events are recorded")
def scenario(body: ScenarioIn, ctx: Ctx = Depends(auth("scenarios:run")), idem: str | None = IdemKey):
    def work(c):
        p, w = project(c)
        st, known, rain, notices = status(c, p, w)
        new_rain, new_notices = rain, dict(notices)
        for e in body.events:
            if e.kind == "procurement_delay":
                if e.item not in w["procurement"] or e.expected_day is None:
                    raise Problem(422, "bad_event", "a procurement delay needs a known item and an expected_day")
                if e.item in known:
                    raise Problem(409, "already_delivered", f"{e.item} was delivered on {day(known[e.item])}")
                new_notices[e.item] = e.expected_day
            else:
                if e.from_day is None or e.to_day is None or e.to_day <= e.from_day:
                    raise Problem(422, "bad_event", "weather needs from_day < to_day")
                new_rain = (e.from_day, e.to_day)
        before = run_forecast(w, st, known, rain, notices, p["now_day"], body.runs)
        after = run_forecast(w, st, known, new_rain, new_notices, p["now_day"], body.runs)
        m = models(w["seed"])["trades"]
        path0, path1 = engine.expected_path(w, st, m, p["now_day"], rain=rain, deliveries={**notices, **known}), engine.expected_path(w, st, m, p["now_day"], rain=new_rain, deliveries={**new_notices, **known})
        acts = w["activities"]
        joined, left = [i for i in path1["path"] if i not in set(path0["path"])], [i for i in path0["path"] if i not in set(path1["path"])]
        if body.apply:
            for e in body.events:
                c.execute("INSERT INTO disruptions (id, tenant_id, project_id, kind, payload) VALUES (%s,%s,%s,%s,%s)", [uuid.uuid7(), ctx.tenant_id, p["id"], e.kind, Jsonb(e.model_dump(exclude_none=True))])
            write_risks(c, ctx.tenant_id, p["id"], w, st, models(w["seed"]), p["now_day"])
            audit.record(c, ctx, "disruptions.announced", "project", p["id"], {"events": [e.kind for e in body.events]})
        return 201, {"applied": body.apply, "events": [e.model_dump(exclude_none=True) for e in body.events], "before": describe(before), "after": describe(after), "shift_days_p50": round(after["finish"]["p50"] - before["finish"]["p50"]), "exposure_shift_p80": round(after["cost_exposure"]["p80"] - before["cost_exposure"]["p80"]),
                     "critical_path": {"before_end": str(day(path0["end"])), "after_end": str(day(path1["end"])), "joined": [{"external_id": f"A{i + 1:04d}", "name": acts[i]["name"]} for i in joined[:10]], "left": [{"external_id": f"A{i + 1:04d}", "name": acts[i]["name"]} for i in left[:10]], "unchanged": len(set(path0["path"]) & set(path1["path"]))}}
    return run(ctx, idem, body, work)


# ---------------------------------------------------------------- progress-vision-service (toy)

class PhotoIn(BaseModel):
    activity_external_id: str = Field(pattern=r"^A\d{4}$")
    captured_at: datetime.datetime
    png_base64: str = Field(min_length=100, max_length=2_000_000)


@app.post("/v1/progress/photos", status_code=201, tags=["progress"], summary="A site photo of a curtain-wall elevation: the toy reader counts glazed panels and compares with the schedule's percent complete")
def photo(body: PhotoIn, ctx: Ctx = Depends(auth("progress:write")), idem: str | None = IdemKey):
    def work(c):
        p, w = project(c)
        a = c.execute("SELECT id, name, trade, percent_complete, status, start_planned, end_planned FROM activities WHERE external_id = %s", [body.activity_external_id]).fetchone()
        if not a:
            raise Problem(404, "activity_not_found")
        if a["trade"] != "envelope":
            raise Problem(422, "not_a_facade", "the reader only knows curtain-wall elevations")
        if body.captured_at.tzinfo is None:
            raise Problem(422, "timezone_required")
        try:
            png = base64.b64decode(body.png_base64, validate=True)
            read = engine.read_facade(png)
        except Exception as e:                       # noqa: BLE001  a bad image is a client error, not a crash
            raise Problem(422, "unreadable_image", str(e)[:80])
        if read["completion"] is None:
            raise Problem(422, "unreadable_image", "no panel grid found")
        today = day(p["now_day"])
        span = max(1, (a["end_planned"] - a["start_planned"]).days)
        scheduled = min(1.0, max(0.0, (today - a["start_planned"]).days / span))
        panels = c.execute("SELECT count(*) AS n FROM bim_elements WHERE activity_id = %s", [a["id"]]).fetchone()["n"]
        oid = uuid.uuid7()
        c.execute("INSERT INTO progress_observations (id, tenant_id, project_id, captured_at, source, media_uri, activity_id, progress_estimate, confidence, detail) VALUES (%s,%s,%s,%s,'site photo',%s,%s,%s,%s,%s)",
                  [oid, ctx.tenant_id, p["id"], body.captured_at, f"upload://{oid}.png", a["id"], read["completion"], read["confidence"], Jsonb({**read, "scheduled": round(scheduled, 3), "recorded_percent": float(a["percent_complete"])})])
        audit.record(c, ctx, "progress.observed", "activity", a["id"], {"activity": body.activity_external_id, "observed": read["completion"], "scheduled": round(scheduled, 3)})
        return 201, {"observation_id": oid, "activity": {"external_id": body.activity_external_id, "name": a["name"], "status": a["status"], "planned": [str(a["start_planned"]), str(a["end_planned"])]}, "observed_completion": read["completion"], "panels_seen": read["panels"], "panels_installed": read["installed"], "bim_panels_linked": panels,
                     "scheduled_completion_today": round(scheduled, 3), "recorded_percent_complete": float(a["percent_complete"]), "variance": round(read["completion"] - scheduled, 3), "confidence": read["confidence"], "method": read["method"]}
    return run(ctx, idem, body, work)


# ---------------------------------------------------------------- change orders

class ChangeOrderIn(BaseModel):
    title: str = Field(min_length=3, max_length=200)
    amount: float = Field(ge=0, le=50_000_000)
    requested_days: int = Field(ge=0, le=365)
    cause: Literal["owner change", "design error", "site condition", "weather", "supplier"]
    scope: dict = Field(description="{trade: extra days per remaining activity} and/or {activity_external_id: extra days}")


@app.post("/v1/change-orders", status_code=201, tags=["change orders"], summary="(+) Record a change order request")
def change_order(body: ChangeOrderIn, ctx: Ctx = Depends(auth("change_orders:write")), idem: str | None = IdemKey):
    def work(c):
        p, w = project(c)
        for k in body.scope:
            if k not in world.TRADES and not c.execute("SELECT 1 FROM activities WHERE external_id = %s", [k]).fetchone():
                raise Problem(422, "bad_scope", f"{k} is neither a trade nor an activity")
        cid = uuid.uuid7()
        c.execute("INSERT INTO change_orders (id, tenant_id, project_id, title, amount, requested_days, cause, scope, created_by) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)", [cid, ctx.tenant_id, p["id"], body.title, body.amount, body.requested_days, body.cause, Jsonb(body.scope), ctx.actor_id])
        audit.record(c, ctx, "change_order.requested", "change_order", cid, {"title": body.title, "amount": body.amount, "days": body.requested_days})
        return 201, {"change_order_id": cid, "status": "requested", **body.model_dump()}
    return run(ctx, idem, body, work)


@app.post("/v1/change-orders/{co_id}/impact", tags=["change orders"], summary="Simulate the change: best, expected and worst completion (P10, P50, P90) and cost exposure against the current forecast")
def impact(co_id: uuid.UUID, runs: int = Query(RUNS, ge=100, le=5000), ctx: Ctx = Depends(auth("change_orders:write")), idem: str | None = IdemKey):
    def work(c):
        p, w = project(c)
        co = c.execute("SELECT * FROM change_orders WHERE id = %s FOR UPDATE", [co_id]).fetchone()
        if not co:
            raise Problem(404, "change_order_not_found")
        st, known, rain, notices = status(c, p, w)
        extra = np.zeros(len(w["activities"]))
        for k, days in co["scope"].items():
            for i, a in enumerate(w["activities"]):
                if not st["done"][i] and (a["trade"] == k or f"A{i + 1:04d}" == k):
                    extra[i] += float(days)
        before = run_forecast(w, st, known, rain, notices, p["now_day"], runs)
        after = run_forecast(w, st, known, rain, notices, p["now_day"], runs, extra=extra)
        result = {"activities_affected": int((extra > 0).sum()), "extra_activity_days": float(extra.sum()), "before": describe(before), "after": describe(after),
                  "completion": {"best": str(day(after["finish"]["p10"])), "expected": str(day(after["finish"]["p50"])), "worst": str(day(after["finish"]["p90"]))}, "shift_days": {"p10": round(after["finish"]["p10"] - before["finish"]["p10"]), "p50": round(after["finish"]["p50"] - before["finish"]["p50"]), "p90": round(after["finish"]["p90"] - before["finish"]["p90"])},
                  "exposure_shift_p80": round(after["cost_exposure"]["p80"] - before["cost_exposure"]["p80"]), "requested_days": co["requested_days"], "requested_vs_simulated_p50": round(co["requested_days"] - (after["finish"]["p50"] - before["finish"]["p50"]))}
        c.execute("UPDATE change_orders SET status = 'evaluated', impact = %s WHERE id = %s", [Jsonb(result), co_id])
        audit.record(c, ctx, "change_order.evaluated", "change_order", co_id, {"shift_p50": result["shift_days"]["p50"], "requested_days": co["requested_days"]})
        return 200, {"change_order_id": co_id, "title": co["title"], "status": "evaluated", **result}
    return run(ctx, idem, {}, work)


@app.post("/v1/change-orders/{co_id}/decide", tags=["change orders"], summary="(+) A director approves or rejects an evaluated change order")
def decide(co_id: uuid.UUID, decision: Literal["approved", "rejected"] = Query(...), ctx: Ctx = Depends(auth("change_orders:approve")), idem: str | None = IdemKey):
    def work(c):
        co = c.execute("SELECT * FROM change_orders WHERE id = %s FOR UPDATE", [co_id]).fetchone()
        if not co:
            raise Problem(404, "change_order_not_found")
        if co["status"] != "evaluated":
            raise Problem(409, "not_evaluated", f"the change order is {co['status']}; simulate its impact first")
        c.execute("UPDATE change_orders SET status = %s, decided_by = %s WHERE id = %s", [decision, ctx.actor_id, co_id])
        audit.record(c, ctx, f"change_order.{decision}", "change_order", co_id, {"title": co["title"], "shift_p50": co["impact"]["shift_days"]["p50"]})
        return 200, {"change_order_id": co_id, "status": decision}
    return run(ctx, idem, {"decision": decision}, work)

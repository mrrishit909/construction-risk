"""Integration, end-to-end and security tests against a real Postgres."""
import base64
import uuid

import pytest

from core import db, jobs, scenario
from cr import world

from .conftest import bearer

DIRECTOR, SCHEDULER, VIEWER, OTHER = bearer("gc", "director"), bearer("gc", "scheduler"), bearer("gc", "viewer"), bearer("other", "director")


def test_nothing_works_before_a_project_is_loaded(client):
    assert client.get(f"/v1/projects/{uuid.uuid4()}/risks", headers=VIEWER).json()["code"] == "no_project" and client.post("/v1/change-orders", headers=SCHEDULER, json={"title": "x y z", "amount": 1, "requested_days": 1, "cause": "weather", "scope": {}}).json()["code"] == "no_project"


@pytest.fixture(scope="module")
def demo(client):
    results, _ = scenario.run(client, drain=jobs.drain)
    return results


def one(sql, params=(), tenant=None):
    with db.tx(tenant) as c:
        return c.execute(sql, params).fetchone()


def test_demo_journey(demo):
    load = demo["load"]["result"]
    assert load["activities"] == 600 == one("SELECT count(*) AS n FROM activities")["n"] and load["bim_elements"] == one("SELECT count(*) AS n FROM bim_elements WHERE element_type = 'curtain wall panel'")["n"] and load["status"]["in_progress"] >= 5
    assert demo["sched"]["updated"] == 2 and demo["sched"]["status"]["finished"] == load["status"]["finished"] + 1 and demo["bad"]["code"] == "already_finished"
    assert demo["bim"] == {**demo["bim"], "added": 2, "updated": 1} and demo["bim"]["linked_to_activities"] == demo["bim"]["elements"]
    f0, f1 = demo["fc0"], demo["fc1"]
    assert f0["finish"]["p10"] < f0["finish"]["p50"] < f0["finish"]["p90"] and f0["chance_on_time"] == 0 and len(f0["critical_path"]["activities"]) == 14 and len(f0["risks"]) == 6 and f0["disruptions_in_force"]["weather"] is None
    assert demo["cp0"]["expected_end"] == f0["critical_path"]["expected_end"] and sum(demo["cp0"]["float_days"].values()) == demo["cp0"]["remaining_activities"] and demo["cp0"]["float_days"]["zero"] == len(demo["cp0"]["critical"])
    assert {r["risk_type"] for r in demo["risks"]["items"]} == {"procurement", "trade_performance"} and all(r["subject"] != "structural steel" for r in demo["risks"]["items"] if r["risk_type"] == "procurement")      # steel was delivered
    w = demo["what"]
    assert w["applied"] and w["shift_days_p50"] > 20 and w["after"]["finish"] == f1["finish"] and f1["disruptions_in_force"]["procurement_notices"] == {"switchgear": f1["disruptions_in_force"]["procurement_notices"]["switchgear"]} and one("SELECT count(*) AS n FROM disruptions")["n"] == 2
    assert f1["simulation_truth"]["inside_p10_p90"] and f1["finish_day"]["p50"] > f0["finish_day"]["p50"]
    p = demo["photo"]
    assert (p["panels_seen"], p["panels_installed"], p["observed_completion"], p["variance"]) == (48, 24, 0.5, -0.5) and p["recorded_percent_complete"] == 0.625 and one("SELECT progress_estimate FROM progress_observations")["progress_estimate"] == 0.5
    assert demo["early"]["code"] == "not_evaluated" and demo["impact"]["activities_affected"] > 300 and demo["impact"]["shift_days"]["p10"] <= demo["impact"]["shift_days"]["p90"] and demo["impact"]["shift_days"]["p50"] > 0
    assert demo["decide"]["status"] == "approved" == one("SELECT status FROM change_orders")["status"]
    assert demo["audit"]["chain_valid"] and {"project.loaded", "schedule.imported", "bim.imported", "disruptions.announced", "progress.observed", "change_order.evaluated", "change_order.approved"} <= {e["action"] for e in demo["audit"]["events"]}


def test_validation(client, demo):
    pid = demo["load"]["result"]["project_id"]
    post = lambda path, body, h=SCHEDULER: client.post(path, headers=h, json=body)     # noqa: E731
    assert post(f"/v1/projects/{pid}/schedule/import", {"activities": [{"external_id": "A9999", "percent_complete": 0.5}]}).json()["code"] == "activity_not_found"
    assert post(f"/v1/projects/{pid}/schedule/import", {"activities": [{"external_id": "A0300", "percent_complete": 0.5, "actual_end": "2026-11-01"}]}).json()["code"] == "bad_dates"
    assert post(f"/v1/projects/{uuid.uuid4()}/schedule/import", {"activities": [{"external_id": "A0300", "percent_complete": 0.5}]}).status_code == 404
    assert post(f"/v1/projects/{pid}/bim/import", {"elements": [{"guid": "X", "element_type": "t", "level": "L1", "activity_external_id": "A9999"}]}).json()["code"] == "activity_not_found"
    assert post("/v1/scenarios", {"events": [{"kind": "procurement_delay", "item": "structural steel", "expected_day": 300}]}).json()["code"] == "already_delivered"
    assert post("/v1/scenarios", {"events": [{"kind": "procurement_delay", "item": "bricks", "expected_day": 300}]}).json()["code"] == "bad_event" and post("/v1/scenarios", {"events": [{"kind": "weather", "from_day": 200, "to_day": 190}]}).json()["code"] == "bad_event"
    png = base64.b64encode(world.facade_image(10)).decode()
    assert post("/v1/progress/photos", {"activity_external_id": "A0057", "captured_at": "2026-11-28T10:00:00+00:00", "png_base64": png}).json()["code"] == "not_a_facade"
    assert post("/v1/progress/photos", {"activity_external_id": "A0144", "captured_at": "2026-11-28T10:00:00", "png_base64": png}).json()["code"] == "timezone_required"
    assert post("/v1/progress/photos", {"activity_external_id": "A0144", "captured_at": "2026-11-28T10:00:00+00:00", "png_base64": "A" * 200}).json()["code"] == "unreadable_image"
    assert post("/v1/change-orders", {"title": "bad scope", "amount": 1, "requested_days": 1, "cause": "weather", "scope": {"plumbing": 2}}).json()["code"] == "bad_scope"
    assert client.post(f"/v1/change-orders/{uuid.uuid4()}/impact", headers=SCHEDULER).status_code == 404 and client.post(f"/v1/change-orders/{demo['co']['change_order_id']}/decide?decision=rejected", headers=DIRECTOR).json()["code"] == "not_evaluated"


def test_scenario_without_apply_changes_nothing(client, demo):
    before = one("SELECT count(*) AS n FROM disruptions")["n"]
    r = client.post("/v1/scenarios", headers=SCHEDULER, json={"events": [{"kind": "procurement_delay", "item": "elevators", "expected_day": 300}], "runs": 100}).json()
    assert r["applied"] is False and r["shift_days_p50"] >= 0 and one("SELECT count(*) AS n FROM disruptions")["n"] == before


def test_roles(client, demo):
    pid = demo["load"]["result"]["project_id"]
    assert client.get(f"/v1/projects/{pid}/forecast").status_code == 401 and client.get(f"/v1/projects/{pid}/forecast", headers=VIEWER, params={"runs": 100}).status_code == 200
    for path in (f"/v1/projects/{pid}/schedule/import", f"/v1/projects/{pid}/bim/import", "/v1/scenarios", "/v1/progress/photos", "/v1/change-orders"):
        assert client.post(path, headers=VIEWER, json={}).status_code == 403, path
    assert client.post(f"/v1/change-orders/{demo['co']['change_order_id']}/decide?decision=approved", headers=SCHEDULER).status_code == 403
    assert client.post("/v1/project:load", headers=SCHEDULER, json={}).status_code == 403 and client.get("/v1/audit", headers=SCHEDULER).status_code == 403 and client.get("/v1/audit", headers=DIRECTOR).status_code == 200


def test_another_contractor_sees_nothing(client, demo, seeded):
    assert client.get(f"/v1/projects/{uuid.uuid4()}/risks", headers=OTHER).json()["code"] == "no_project"
    for table in ("projects", "activities", "dependencies", "bim_elements", "procurement_items", "progress_observations", "disruptions", "change_orders", "risks"):
        assert one(f"SELECT count(*) AS n FROM {table}", tenant=seeded["other"])["n"] == 0 < one(f"SELECT count(*) AS n FROM {table}", tenant=seeded["gc"])["n"], table
    with pytest.raises(Exception, match="row-level security"), db.tx(seeded["other"]) as c:
        c.execute("INSERT INTO projects (id, tenant_id, name, start_date, planned_end, budget, model_seed, now_day) VALUES (%s,%s,'x','2026-06-01','2027-04-07',1,1,0)", [uuid.uuid4(), seeded["gc"]])


def test_idempotent_writes(client, demo):
    body = {"title": "Upgraded lobby and corridor finishes", "amount": 450000, "requested_days": 30, "cause": "owner change", "scope": {"finishes": 3}}
    again = client.post("/v1/change-orders", headers={**SCHEDULER, "Idempotency-Key": "demo-1:co"}, json=body)
    assert again.status_code == 201 and again.headers["Idempotent-Replay"] == "true" and again.json()["change_order_id"] == demo["co"]["change_order_id"]
    assert client.post("/v1/change-orders", headers={**SCHEDULER, "Idempotency-Key": "demo-1:co"}, json={**body, "amount": 1}).json()["code"] == "idempotency_key_reused"
    assert client.post("/v1/project:load", headers=DIRECTOR, json={"seed": 17}).json()["code"] == "project_exists"

import json
import os
import sqlite3
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone

from app import api, clock, db, rules, server

BASE = datetime(2026, 10, 20, 10, 0, tzinfo=timezone.utc)
PLANT, ENG, QM, SUP, INSP, INSP2, OPR, CLERK = 1, 2, 3, 4, 5, 6, 7, 8
# seeded template 1 fields: 1 Diameter (CTQ) 24.98..25.05, 2 Pitch 1.48..1.52, 3 Ra max 0.8, 4 Runout max 0.02, 5 Burrs
ALL_PASS = [(1, "25.00", "VC-104"), (2, "1.50", "PG-04"), (3, "0.5", "MIC-07"), (4, "0.01", "MIC-07"), (5, True, None)]
PHOTOS = [{"name": "a.jpg"}, {"name": "b.jpg"}]


class RulesTests(unittest.TestCase):
    RANGE = {"kind": "range", "nominal": "25.00", "upper_tol": "0.05", "lower_tol": "-0.02"}

    def test_range_boundaries_are_inclusive_and_exact(self):
        for v, ok in [("24.98", True), ("24.979", False), ("25.05", True), ("25.051", False),
                      (25.05, True), ("25", True), ("26", False)]:
            self.assertEqual(rules.evaluate(self.RANGE, v)[1], ok, v)

    def test_max_min(self):
        mx = {"kind": "max", "limit_value": "0.8"}
        mn = {"kind": "min", "limit_value": "10"}
        self.assertTrue(rules.evaluate(mx, "0.8")[1])
        self.assertFalse(rules.evaluate(mx, "0.81")[1])
        self.assertTrue(rules.evaluate(mn, "10")[1])
        self.assertFalse(rules.evaluate(mn, "9.99")[1])

    def test_visual(self):
        f = {"kind": "visual"}
        self.assertEqual(rules.evaluate(f, "Yes"), ("yes", True))
        self.assertEqual(rules.evaluate(f, False), ("no", False))
        with self.assertRaises(ValueError):
            rules.evaluate(f, "maybe")

    def test_bad_numbers_rejected(self):
        for v in ("abc", "NaN", "inf", "", True, None):
            with self.assertRaises(ValueError, msg=repr(v)):
                rules.evaluate(self.RANGE, v)

    def test_field_definition_validation(self):
        with self.assertRaises(ValueError):
            rules.validate_field_def({"label": "x", "kind": "range", "nominal": "1", "upper_tol": "-0.1",
                                      "lower_tol": "-0.2"})
        with self.assertRaises(ValueError):
            rules.validate_field_def({"label": "", "kind": "visual"})
        with self.assertRaises(ValueError):
            rules.validate_field_def({"label": "x", "kind": "max"})
        self.assertEqual(rules.validate_field_def({"label": "x", "kind": "max", "limit_value": 0.8})["limit_value"], "0.8")

    def test_format_remaining(self):
        self.assertEqual(rules.format_remaining(82 * 60), "1 hr 22 min")
        self.assertEqual(rules.format_remaining(3600), "1 hr")
        self.assertEqual(rules.format_remaining(5), "1 min")
        self.assertEqual(rules.format_remaining(61), "2 min")


class ApiTests(unittest.TestCase):
    def setUp(self):
        fd, self.path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        os.environ["QC_DB"] = self.path
        clock.set_now(BASE)
        db.init_db()

    def tearDown(self):
        clock.reset()
        os.unlink(self.path)

    # ---- helpers
    def call(self, method, path, user=None, body=None, query=None):
        headers = {"x-user-id": str(user)} if user else {}
        payload = ({} if body is None else body) if method == "POST" else None
        return api.dispatch(method, path, query or {}, payload, headers, "127.0.0.1")

    def ok(self, *a, **k):
        s, p = self.call(*a, **k)
        self.assertIn(s, (200, 201), p)
        return p

    def err(self, status, *a, **k):
        s, p = self.call(*a, **k)
        self.assertEqual(s, status, p)
        return p["detail"]

    def start(self, barcode="JOB-1001", final=False, user=INSP):
        return self.call("POST", "/api/inspections", user,
                         {"machine_qr": "QR-MCH-CNC04", "traveler_barcode": barcode, "is_final": final})

    def enter(self, iid, field, value, gauge=None, photos=None, user=INSP):
        body = {"field_id": field, "value": value, "gauge_code": gauge, "photos": photos or []}
        return self.call("POST", f"/api/inspections/{iid}/values", user, body)

    def fill_pass(self, iid):
        for f, v, g in ALL_PASS:
            s, p = self.enter(iid, f, v, g)
            self.assertEqual(s, 200, p)

    def run_ops(self, job_id, upto, advance=True):
        for op in self.ok("GET", f"/api/jobs/{job_id}", PLANT)["ops"]:
            if op["seq"] > upto:
                break
            if op["status"] == "done":
                continue
            self.ok("POST", f"/api/jobs/{job_id}/ops/{op['id']}/start", OPR)
            self.ok("POST", f"/api/jobs/{job_id}/ops/{op['id']}/complete", OPR)
            if advance and op["hold_minutes"]:
                clock.set_now(clock.now() + timedelta(minutes=op["hold_minutes"], seconds=1))

    def final_pdi(self, barcode="JOB-1001", values=None):
        s, insp = self.start(barcode, final=True)
        self.assertEqual(s, 201, insp)
        for f, v, g in (values or ALL_PASS):
            s, p = self.enter(insp["id"], f, v, g, PHOTOS)
            self.assertEqual(s, 200, p)
        return insp["id"]

    # ---- auth / roles
    def test_auth_and_roles(self):
        self.err(401, "GET", "/api/jobs")
        self.err(403, "POST", "/api/templates", INSP, {"part_id": 1, "rev": "B", "fields": []})
        self.err(403, "GET", "/api/audit", INSP)
        self.assertEqual(self.call("GET", "/api/users")[0], 200)

    # ---- templates
    def test_template_lifecycle_and_revisions(self):
        body = {"part_id": 1, "rev": "B", "fields": [
            {"label": "Bore", "kind": "range", "nominal": 10, "upper_tol": 0.1, "lower_tol": -0.1, "unit": "mm", "ctq": True}]}
        t = self.ok("POST", "/api/templates", ENG, body)
        self.assertEqual(t["status"], "draft")
        self.err(409, "POST", "/api/templates", ENG, body)            # duplicate revision
        self.err(403, "POST", f"/api/templates/{t['id']}/release", ENG)  # only Quality Manager
        # a draft is not usable on the floor: inspection still uses Rev A
        s, insp = self.start()
        self.assertEqual(s, 201)
        self.assertEqual(insp["template_id"], 1)
        rel = self.ok("POST", f"/api/templates/{t['id']}/release", QM)
        self.assertEqual(rel["status"], "released")
        self.assertEqual(self.ok("GET", "/api/templates/1", QM)["status"], "superseded")
        self.err(409, "POST", f"/api/templates/{t['id']}/release", QM)  # already released

    def test_template_validation(self):
        bad = {"part_id": 1, "rev": "C", "fields": [{"label": "x", "kind": "range", "nominal": 1, "upper_tol": 0.1,
                                                      "lower_tol": 0.2}]}
        self.err(400, "POST", "/api/templates", ENG, bad)
        self.err(400, "POST", "/api/templates", ENG, {"part_id": 1, "rev": "C", "fields": []})
        self.err(404, "POST", "/api/templates", ENG, {"part_id": 99, "rev": "C", "fields": [{"label": "a", "kind": "visual"}]})

    # ---- QR scan rule
    def test_station_and_traveler_scan_required(self):
        self.err(400, "POST", "/api/inspections", INSP, {"traveler_barcode": "JOB-1001"})
        s, p = self.call("POST", "/api/inspections", INSP, {"machine_qr": "NOPE", "traveler_barcode": "JOB-1001"})
        self.assertEqual(s, 404)
        s, p = self.call("POST", "/api/inspections", INSP, {"machine_qr": "QR-MCH-CNC04", "traveler_barcode": "X"})
        self.assertEqual(s, 404)
        s, insp = self.start()
        self.assertEqual(s, 201)
        self.assertIn("locked", self.err(409, "POST", "/api/inspections", INSP,
                      {"machine_qr": "QR-MCH-CNC04", "traveler_barcode": "JOB-1002"}))
        self.assertEqual(self.start("JOB-1002", user=INSP2)[0], 201)  # another inspector is independent

    # ---- gauge calibration
    def test_gauge_rules(self):
        _, insp = self.start()
        i = insp["id"]
        self.assertIn("Expired Tool Calibration", self.err(400, "POST", f"/api/inspections/{i}/values", INSP,
                      {"field_id": 1, "value": "25.00", "gauge_code": "VC-12"}))
        self.err(400, "POST", f"/api/inspections/{i}/values", INSP, {"field_id": 1, "value": "25.00"})
        self.err(400, "POST", f"/api/inspections/{i}/values", INSP, {"field_id": 1, "value": "25", "gauge_code": "ZZ"})
        self.assertEqual(self.enter(i, 1, "25.00", "VC-104")[0], 200)
        self.assertEqual(self.enter(i, 5, True)[0], 200)  # visual checks need no gauge
        # the day after the expiry date it is blocked; on the day itself it is still valid
        exp = self.ok("GET", "/api/gauges", INSP)
        vc104 = next(g for g in exp if g["code"] == "VC-104")
        clock.set_now(datetime.fromisoformat(vc104["cal_expiry"]).replace(tzinfo=timezone.utc, hour=12))
        self.assertEqual(self.enter(i, 1, "25.00", "VC-104")[0], 200)
        clock.set_now(clock.now() + timedelta(days=1))
        self.assertIn("Expired", self.err(400, "POST", f"/api/inspections/{i}/values", INSP,
                      {"field_id": 1, "value": "25.00", "gauge_code": "VC-104"}))

    # ---- tolerance / photos / CTQ
    def test_tolerance_photos_and_no_partial_write(self):
        _, insp = self.start()
        i = insp["id"]
        self.assertTrue(self.enter(i, 1, "24.98", "VC-104")[1]["in_tolerance"])
        self.assertTrue(self.enter(i, 1, "25.05", "VC-104")[1]["in_tolerance"])
        # non-CTQ failure (pitch) without photos is rejected and writes nothing
        self.assertIn("two", self.err(400, "POST", f"/api/inspections/{i}/values", INSP,
                      {"field_id": 2, "value": "1.53", "gauge_code": "PG-04", "photos": [{"name": "a.jpg"}]}))
        d = self.ok("GET", f"/api/inspections/{i}", INSP)
        self.assertIsNone(next(f for f in d["fields"] if f["id"] == 2)["value"])
        s, p = self.enter(i, 2, "1.53", "PG-04", PHOTOS)
        self.assertEqual((s, p["result"], p["frozen"]), (200, "fail", False))
        d = self.ok("GET", f"/api/inspections/{i}", INSP)
        self.assertEqual(next(f for f in d["fields"] if f["id"] == 2)["photo_count"], 2)
        self.assertEqual(self.enter(i, 2, "1.50", "PG-04")[1]["result"], "pass")  # correction before submit

    def test_ctq_failure_freezes_alerts_and_blocks_concession(self):
        _, insp = self.start()
        i = insp["id"]
        s, p = self.enter(i, 1, "25.06", "VC-104", PHOTOS)
        self.assertTrue(p["frozen"])
        self.assertEqual(p["inspection_status"], "frozen")
        self.assertTrue(any("CTQ failure" in a["message"] for a in self.ok("GET", "/api/alerts", QM)))
        self.assertEqual({a["channel"] for a in self.ok("GET", "/api/alerts", QM)}, {"email", "sms"})
        self.assertIn("locked", self.err(409, "POST", f"/api/inspections/{i}/values", INSP,
                      {"field_id": 2, "value": "1.5", "gauge_code": "PG-04"}))
        self.err(409, "POST", f"/api/inspections/{i}/submit", INSP)
        queue = self.ok("GET", "/api/review-queue", SUP)
        self.assertEqual((queue[0]["status"], queue[0]["ctq_fails"]), ("frozen", 1))
        self.assertIn("CTQ", self.err(403, "POST", f"/api/inspections/{i}/review", SUP,
                      {"decision": "concession", "reason_code": "FUNCTIONAL_OK", "justification": "looks fine to me"}))
        r = self.ok("POST", f"/api/inspections/{i}/review", SUP, {"decision": "reject", "notes": "OD too big"})
        self.assertEqual((r["status"], r["job"]["status"]), ("rejected", "quarantined"))
        self.assertEqual(r["job"]["location"], "Digital Rejection Holding Area")

    def test_frozen_inspection_cannot_be_approved(self):
        _, insp = self.start()
        self.enter(insp["id"], 1, "25.06", "VC-104", PHOTOS)
        self.assertIn("rejected", self.err(409, "POST", f"/api/inspections/{insp['id']}/review", SUP,
                                           {"decision": "approve"}))

    # ---- submit / lock / review
    def test_cannot_skip_fields_and_submitted_data_is_locked(self):
        _, insp = self.start()
        i = insp["id"]
        self.enter(i, 1, "25.00", "VC-104")
        self.assertIn("Still to measure", self.err(400, "POST", f"/api/inspections/{i}/submit", INSP))
        self.assertEqual(self.enter(i, 5, True)[0], 200)
        self.assertEqual(self.call("POST", f"/api/inspections/{i}/submit", INSP)[0], 400)
        for f, v, g in ALL_PASS:
            self.enter(i, f, v, g)
        self.assertEqual(self.ok("POST", f"/api/inspections/{i}/submit", INSP)["status"], "pending_review")
        self.assertIn("locked", self.err(409, "POST", f"/api/inspections/{i}/values", INSP,
                      {"field_id": 1, "value": "25.01", "gauge_code": "VC-104"}))
        self.err(403, "POST", f"/api/inspections/{i}/values", INSP2, {"field_id": 1, "value": "25", "gauge_code": "VC-104"})

    def test_approve_and_concession_rules(self):
        _, insp = self.start()
        i = insp["id"]
        for f, v, g in ALL_PASS:
            self.enter(i, f, v, g)
        self.enter(i, 2, "1.60", "PG-04", PHOTOS)  # non-CTQ failure
        self.ok("POST", f"/api/inspections/{i}/submit", INSP)
        self.err(403, "POST", f"/api/inspections/{i}/review", INSP, {"decision": "approve"})  # role
        self.assertIn("out of tolerance", self.err(400, "POST", f"/api/inspections/{i}/review", SUP, {"decision": "approve"}))
        self.err(400, "POST", f"/api/inspections/{i}/review", SUP, {"decision": "concession", "reason_code": "BAD",
                                                                     "justification": "long enough text"})
        self.err(400, "POST", f"/api/inspections/{i}/review", SUP, {"decision": "concession",
                                                                     "reason_code": "COSMETIC_ONLY", "justification": "short"})
        r = self.ok("POST", f"/api/inspections/{i}/review", QM, {"decision": "concession", "reason_code": "COSMETIC_ONLY",
                                                                  "justification": "Pitch error is cosmetic for this customer"})
        self.assertEqual(r["status"], "conceded")
        self.assertEqual(r["concession"]["reason_code"], "COSMETIC_ONLY")
        self.err(409, "POST", f"/api/inspections/{i}/review", SUP, {"decision": "reject"})  # already decided

    def test_concession_not_needed_when_all_pass(self):
        _, insp = self.start()
        for f, v, g in ALL_PASS:
            self.enter(insp["id"], f, v, g)
        self.ok("POST", f"/api/inspections/{insp['id']}/submit", INSP)
        self.err(400, "POST", f"/api/inspections/{insp['id']}/review", SUP,
                 {"decision": "concession", "reason_code": "FUNCTIONAL_OK", "justification": "not really needed"})
        self.assertEqual(self.ok("POST", f"/api/inspections/{insp['id']}/review", SUP, {"decision": "approve"})["status"], "approved")

    # ---- quarantine + MRB
    def test_quarantine_blocks_work_and_mrb_dispositions(self):
        self.run_ops(1, 20)
        _, insp = self.start()
        for f, v, g in ALL_PASS:
            self.enter(insp["id"], f, v, g)
        self.ok("POST", f"/api/inspections/{insp['id']}/submit", INSP)
        self.ok("POST", f"/api/inspections/{insp['id']}/review", SUP, {"decision": "reject"})
        job = self.ok("GET", "/api/jobs/1", SUP)
        op30 = next(o for o in job["ops"] if o["seq"] == 30)
        self.assertIn("quarantined", self.err(409, "POST", f"/api/jobs/1/ops/{op30['id']}/start", OPR))
        self.assertEqual(self.start()[0], 409)                       # no new inspection either
        self.err(400, "POST", "/api/jobs/1/mrb", QM, {"disposition": "rework", "notes": ""})
        self.err(400, "POST", "/api/jobs/1/mrb", QM, {"disposition": "nope", "notes": "x"})
        self.err(403, "POST", "/api/jobs/1/mrb", SUP, {"disposition": "rework", "notes": "x"})
        j = self.ok("POST", "/api/jobs/1/mrb", QM, {"disposition": "rework", "notes": "Re-grind OD"})
        self.assertEqual((j["status"], j["location"]), ("active", "Rework Cell"))
        self.assertEqual(j["ops"][-1]["name"], "Rework (MRB)")
        self.err(409, "POST", "/api/jobs/1/mrb", QM, {"disposition": "scrap", "notes": "x"})  # no longer quarantined

    def test_scrap_rtv_and_customer_concession(self):
        for jid, disp, status in [(1, "scrap", "scrapped"), (2, "rtv", "returned_to_vendor"),
                                  (3, "customer_concession", "held_customer_approval")]:
            _, insp = self.start(f"JOB-{1000 + jid}")
            for f, v, g in ALL_PASS:
                self.enter(insp["id"], f, v, g)
            self.ok("POST", f"/api/inspections/{insp['id']}/submit", INSP)
            self.ok("POST", f"/api/inspections/{insp['id']}/review", SUP, {"decision": "reject"})
            self.assertEqual(self.ok("POST", f"/api/jobs/{jid}/mrb", QM, {"disposition": disp, "notes": "n"})["status"], status)
        self.err(400, "POST", "/api/jobs/3/customer-approval", QM, {})
        self.assertEqual(self.ok("POST", "/api/jobs/3/customer-approval", QM, {"reference": "CUST-APR-77"})["status"], "active")
        self.err(409, "POST", "/api/jobs/1/customer-approval", QM, {"reference": "x"})

    # ---- traveler sequencing + hold timers
    def test_operations_must_run_in_sequence(self):
        ops = self.ok("GET", "/api/jobs/1", SUP)["ops"]
        self.assertIn("Op 10", self.err(409, "POST", f"/api/jobs/1/ops/{ops[1]['id']}/start", OPR))
        self.err(409, "POST", f"/api/jobs/1/ops/{ops[0]['id']}/complete", OPR)  # not started
        self.ok("POST", f"/api/jobs/1/ops/{ops[0]['id']}/start", OPR)
        self.err(409, "POST", f"/api/jobs/1/ops/{ops[0]['id']}/start", OPR)     # already running
        self.err(409, "POST", f"/api/jobs/1/ops/{ops[1]['id']}/start", OPR)     # Op 10 not done
        self.err(403, "POST", f"/api/jobs/1/ops/{ops[0]['id']}/complete", INSP)

    def test_hold_timer_blocks_next_operation(self):
        self.run_ops(1, 20)
        ops = {o["seq"]: o for o in self.ok("GET", "/api/jobs/1", SUP)["ops"]}
        self.ok("POST", f"/api/jobs/1/ops/{ops[30]['id']}/start", OPR)
        self.ok("POST", f"/api/jobs/1/ops/{ops[30]['id']}/complete", OPR)   # 1-minute hold begins
        msg = self.err(409, "POST", f"/api/jobs/1/ops/{ops[40]['id']}/start", OPR)
        self.assertIn("Mandatory Hold Active (1 min remaining)", msg)
        clock.set_now(clock.now() + timedelta(seconds=59))
        self.err(409, "POST", f"/api/jobs/1/ops/{ops[40]['id']}/start", OPR)
        clock.set_now(clock.now() + timedelta(seconds=2))
        self.assertEqual(self.ok("POST", f"/api/jobs/1/ops/{ops[40]['id']}/start", OPR)["id"], 1)

    def test_long_hold_message_uses_hours(self):
        job = self.ok("POST", "/api/jobs", PLANT, {"traveler_no": "2001", "part_id": 1, "ops": [
            {"name": "Stress Relief", "hold_minutes": 82}, {"name": "Machining"}]})
        o1, o2 = job["ops"]
        self.ok("POST", f"/api/jobs/{job['id']}/ops/{o1['id']}/start", OPR)
        self.ok("POST", f"/api/jobs/{job['id']}/ops/{o1['id']}/complete", OPR)
        self.assertIn("1 hr 22 min", self.err(409, "POST", f"/api/jobs/{job['id']}/ops/{o2['id']}/start", OPR))
        self.err(409, "POST", "/api/jobs", PLANT, {"traveler_no": "2001", "part_id": 1, "ops": [{"name": "x"}]})
        self.err(400, "POST", "/api/jobs", PLANT, {"traveler_no": "2002", "part_id": 1, "ops": [{"name": "x", "hold_minutes": -1}]})

    # ---- shipping interlock
    def test_gate_pass_locked_until_all_three_conditions(self):
        d = self.err(409, "POST", "/api/jobs/1/gate-pass", CLERK)
        self.assertEqual(len(d["unmet"]), 2)  # operations + final PDI; holds are clear
        self.assertTrue(any("Operations not signed off" in u for u in d["unmet"]))
        self.assertTrue(any("No final PDI" in u for u in d["unmet"]))
        self.err(403, "POST", "/api/jobs/1/gate-pass", SUP)
        self.run_ops(1, 60)
        d = self.err(409, "POST", "/api/jobs/1/gate-pass", CLERK)
        self.assertEqual(len(d["unmet"]), 1)
        self.assertIn("No final PDI", d["unmet"][0])
        iid = self.final_pdi()
        self.ok("POST", f"/api/inspections/{iid}/submit", INSP)
        self.assertIn("pending_review", self.err(409, "POST", "/api/jobs/1/gate-pass", CLERK)["unmet"][0])
        self.ok("POST", f"/api/inspections/{iid}/review", SUP, {"decision": "approve"})
        p = self.ok("POST", "/api/jobs/1/gate-pass", CLERK)
        self.assertEqual(p["gate_pass"]["pass_no"], "GP-1001")
        self.assertIn("already issued", self.err(409, "POST", "/api/jobs/1/gate-pass", CLERK))

    def test_fresh_job_dispatch_check_shape(self):
        d = self.ok("GET", "/api/jobs/2/dispatch-check", SUP)
        self.assertFalse(d["ok"])
        self.assertEqual([c["key"] for c in d["conditions"]], ["operations_complete", "holds_clear", "final_pdi_approved"])
        self.assertEqual([c["ok"] for c in d["conditions"]], [False, True, False])

    def test_gate_pass_blocked_when_hold_is_active(self):
        job = self.ok("POST", "/api/jobs", PLANT, {"traveler_no": "3001", "part_id": 1, "ops": [{"name": "Cure", "hold_minutes": 30}]})
        op = job["ops"][0]
        self.ok("POST", f"/api/jobs/{job['id']}/ops/{op['id']}/start", OPR)
        self.ok("POST", f"/api/jobs/{job['id']}/ops/{op['id']}/complete", OPR)
        _, insp = self.start("JOB-3001", final=True)
        for f, v, g in ALL_PASS:
            self.enter(insp["id"], f, v, g)
        self.ok("POST", f"/api/inspections/{insp['id']}/submit", INSP)
        self.ok("POST", f"/api/inspections/{insp['id']}/review", SUP, {"decision": "approve"})
        d = self.err(409, "POST", f"/api/jobs/{job['id']}/gate-pass", CLERK)
        self.assertEqual(len(d["unmet"]), 1)
        self.assertIn("hold active (30 min remaining)", d["unmet"][0])
        clock.set_now(clock.now() + timedelta(minutes=31))
        self.assertEqual(self.ok("POST", f"/api/jobs/{job['id']}/gate-pass", CLERK)["gate_pass"]["pass_no"], "GP-3001")

    def test_conceded_final_pdi_clears_but_rework_needs_new_pdi(self):
        self.run_ops(1, 60)
        iid = self.final_pdi(values=[(1, "25.00", "VC-104"), (2, "1.60", "PG-04"), (3, "0.5", "MIC-07"),
                                     (4, "0.01", "MIC-07"), (5, True, None)])
        self.ok("POST", f"/api/inspections/{iid}/submit", INSP)
        self.ok("POST", f"/api/inspections/{iid}/review", QM, {"decision": "concession", "reason_code": "FUNCTIONAL_OK",
                                                                "justification": "Pitch deviation is functionally harmless"})
        self.assertTrue(self.ok("GET", "/api/jobs/1/dispatch-check", CLERK)["ok"])

    def test_rejected_final_pdi_and_rework_loop(self):
        self.run_ops(1, 60)
        iid = self.final_pdi()
        self.ok("POST", f"/api/inspections/{iid}/submit", INSP)
        self.ok("POST", f"/api/inspections/{iid}/review", SUP, {"decision": "reject"})
        d = self.err(409, "POST", "/api/jobs/1/gate-pass", CLERK)
        self.assertTrue(any("quarantined" in u for u in d["unmet"]))
        self.ok("POST", "/api/jobs/1/mrb", QM, {"disposition": "rework", "notes": "redo"})
        d = self.err(409, "POST", "/api/jobs/1/gate-pass", CLERK)
        self.assertEqual(len(d["unmet"]), 2)                      # rework op pending + latest PDI rejected
        self.run_ops(1, 999)
        iid2 = self.final_pdi()
        self.ok("POST", f"/api/inspections/{iid2}/submit", INSP)
        self.ok("POST", f"/api/inspections/{iid2}/review", SUP, {"decision": "approve"})
        self.assertEqual(self.ok("POST", "/api/jobs/1/gate-pass", CLERK)["gate_pass"]["pass_no"], "GP-1001")

    # ---- audit trail
    def test_audit_log_records_and_is_append_only(self):
        _, insp = self.start()
        self.enter(insp["id"], 1, "25.00", "VC-104")
        self.enter(insp["id"], 1, "25.01", "VC-104")
        rows = self.ok("GET", "/api/audit", PLANT, query={"entity": "inspection"})
        changes = [r for r in rows if r["action"] == "inspection.value"]
        self.assertEqual(len(changes), 2)
        newest = changes[0]
        self.assertEqual(json.loads(newest["old_value"])["raw_value"], "25.00")
        self.assertEqual(json.loads(newest["new_value"])["value"], "25.01")
        self.assertEqual((newest["user_id"], newest["ip"]), (INSP, "127.0.0.1"))
        conn = db.connect()
        try:
            for sql in ("DELETE FROM audit_log", "UPDATE audit_log SET action='x'",
                        "DELETE FROM inspection_values", "DELETE FROM inspections"):
                with self.assertRaises(sqlite3.IntegrityError, msg=sql):
                    conn.execute(sql)
        finally:
            conn.close()

    def test_failed_request_leaves_no_audit_or_data_behind(self):
        _, insp = self.start()
        before = len(self.ok("GET", "/api/audit", PLANT))
        self.err(400, "POST", f"/api/inspections/{insp['id']}/values", INSP,
                 {"field_id": 2, "value": "9", "gauge_code": "PG-04"})
        self.assertEqual(len(self.ok("GET", "/api/audit", PLANT)), before)


class HttpSmokeTest(unittest.TestCase):
    def test_http_end_to_end(self):
        fd, path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        os.environ["QC_DB"] = path
        os.environ["QC_QUIET"] = "1"
        clock.reset()
        db.init_db()
        srv = server.make_server("127.0.0.1", 0)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        base = f"http://127.0.0.1:{srv.server_address[1]}"

        def req(method, path, user=None, body=None):
            r = urllib.request.Request(base + path, method=method,
                                       data=json.dumps(body).encode() if body is not None else None,
                                       headers={"Content-Type": "application/json", **({"X-User-Id": str(user)} if user else {})})
            try:
                with urllib.request.urlopen(r) as resp:
                    return resp.status, resp.read()
            except urllib.error.HTTPError as e:
                return e.code, e.read()

        try:
            s, html = req("GET", "/")
            self.assertEqual(s, 200)
            self.assertIn(b"Zapuza", html)
            self.assertEqual(req("GET", "/api/jobs")[0], 401)
            s, b = req("GET", "/api/jobs", PLANT)
            self.assertEqual((s, len(json.loads(b))), (200, 3))
            s, b = req("POST", "/api/inspections", INSP, {"machine_qr": "QR-MCH-CNC04", "traveler_barcode": "JOB-1001"})
            self.assertEqual(s, 201)
            self.assertEqual(req("POST", "/api/inspections", INSP, "not-an-object")[0], 400)
            self.assertEqual(req("GET", "/api/nope", PLANT)[0], 404)
        finally:
            srv.shutdown()
            os.unlink(path)


if __name__ == "__main__":
    unittest.main()

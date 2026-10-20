"""Business logic + routing (framework-free). server.py exposes this over HTTP.

Every handler is a function of a Ctx and runs inside one DB transaction:
if it raises ApiError (or the DB rejects a write) the whole request rolls back,
so a change and its audit entry are always atomic.
"""
import json
import re
import sqlite3
from datetime import timedelta

from . import clock, db, rules

CONCESSION_REASONS = ["FUNCTIONAL_OK", "COSMETIC_ONLY", "WITHIN_CUSTOMER_AGREEMENT", "ENGINEERING_WAIVER"]
MRB_DISPOSITIONS = ["rework", "scrap", "rtv", "customer_concession"]
MAX_PHOTO_CHARS = 3_000_000


class ApiError(Exception):
    def __init__(self, status: int, detail):
        super().__init__(str(detail))
        self.status, self.detail = status, detail


class Ctx:
    def __init__(self, conn, user, body, params, query, ip, device):
        self.conn, self.user, self.body = conn, user, body
        self.params, self.query, self.ip, self.device = params, query, ip, device


# ------------------------------------------------------------------ helpers
def one(conn, sql, *args):
    r = conn.execute(sql, args).fetchone()
    return dict(r) if r else None


def many(conn, sql, *args):
    return [dict(r) for r in conn.execute(sql, args).fetchall()]


def need(cond, status, msg):
    if not cond:
        raise ApiError(status, msg)


def get_or_404(conn, table, row_id, label):
    r = one(conn, f"SELECT * FROM {table} WHERE id=?", row_id)
    need(r, 404, f"{label} {row_id} not found")
    return r


def audit(ctx, action, entity, entity_id, old=None, new=None):
    ctx.conn.execute(
        "INSERT INTO audit_log(ts,user_id,user_name,action,entity,entity_id,old_value,new_value,ip,device_serial)"
        " VALUES (?,?,?,?,?,?,?,?,?,?)",
        (clock.iso(), ctx.user["id"], ctx.user["name"], action, entity, str(entity_id),
         None if old is None else json.dumps(old), None if new is None else json.dumps(new),
         ctx.ip, ctx.device))


def req_str(body, key):
    v = body.get(key)
    need(isinstance(v, str) and v.strip(), 400, f"'{key}' is required")
    return v.strip()


def req_int(body, key):
    v = body.get(key)
    need(isinstance(v, int) and not isinstance(v, bool), 400, f"'{key}' must be an integer")
    return v


def alert(conn, recipient_role, message):
    for channel in ("email", "sms"):  # delivery is simulated; the record is the evidence
        conn.execute("INSERT INTO alerts(ts, channel, recipient_role, message) VALUES (?,?,?,?)",
                     (clock.iso(), channel, recipient_role, message))


# ------------------------------------------------------------- reference data
def list_users(ctx):
    return many(ctx.conn, "SELECT id, name, role FROM users ORDER BY id")


def list_gauges(ctx):
    return many(ctx.conn, "SELECT * FROM gauges ORDER BY id")


def list_machines(ctx):
    return many(ctx.conn, "SELECT * FROM machines ORDER BY id")


def list_parts(ctx):
    return many(ctx.conn, "SELECT * FROM parts ORDER BY id")


# ------------------------------------------------------------------ templates
def create_template(ctx):
    b, conn = ctx.body, ctx.conn
    part_id = req_int(b, "part_id")
    get_or_404(conn, "parts", part_id, "Part")
    rev = req_str(b, "rev")
    fields = b.get("fields")
    need(isinstance(fields, list) and fields, 400, "At least one inspection field is required")
    try:
        norm = [rules.validate_field_def(f) for f in fields]
    except ValueError as e:
        raise ApiError(400, str(e))
    need(one(conn, "SELECT id FROM templates WHERE part_id=? AND rev=?", part_id, rev) is None,
         409, f"Revision {rev} already exists for this part")
    cur = conn.execute("INSERT INTO templates(part_id, rev, status, created_by, created_at) VALUES (?,?,?,?,?)",
                       (part_id, rev, "draft", ctx.user["id"], clock.iso()))
    tid = cur.lastrowid
    for i, f in enumerate(norm, start=1):
        conn.execute("INSERT INTO template_fields(template_id, seq, label, kind, nominal, upper_tol, lower_tol,"
                     " limit_value, unit, ctq) VALUES (?,?,?,?,?,?,?,?,?,?)",
                     (tid, i, f["label"], f["kind"], f["nominal"], f["upper_tol"], f["lower_tol"],
                      f["limit_value"], f["unit"], f["ctq"]))
    audit(ctx, "template.create", "template", tid, None, {"part_id": part_id, "rev": rev, "fields": len(norm)})
    return 201, template_detail(conn, tid)


def template_detail(conn, tid):
    t = get_or_404(conn, "templates", tid, "Template")
    t["fields"] = many(conn, "SELECT * FROM template_fields WHERE template_id=? ORDER BY seq", tid)
    for f in t["fields"]:
        f["limits"] = rules.describe_limits(f)
    return t


def get_template(ctx):
    return template_detail(ctx.conn, int(ctx.params["id"]))


def list_templates(ctx):
    sql, args = "SELECT t.*, p.part_no FROM templates t JOIN parts p ON p.id=t.part_id", []
    if ctx.query.get("part_id"):
        sql += " WHERE t.part_id=?"
        args.append(int(ctx.query["part_id"]))
    return many(ctx.conn, sql + " ORDER BY t.id", *args)


def release_template(ctx):
    conn, tid = ctx.conn, int(ctx.params["id"])
    t = get_or_404(conn, "templates", tid, "Template")
    need(t["status"] == "draft", 409, f"Template is '{t['status']}', only drafts can be released")
    need(t["created_by"] != ctx.user["id"], 403, "A template cannot be approved by its own author")
    conn.execute("UPDATE templates SET status='superseded' WHERE part_id=? AND status='released'", (t["part_id"],))
    conn.execute("UPDATE templates SET status='released', approved_by=? WHERE id=?", (ctx.user["id"], tid))
    audit(ctx, "template.release", "template", tid, {"status": "draft"}, {"status": "released"})
    return template_detail(conn, tid)


# ----------------------------------------------------------------------- jobs
def job_detail(conn, job_id):
    job = get_or_404(conn, "jobs", job_id, "Job")
    now = clock.now()
    ops = many(conn, "SELECT * FROM job_ops WHERE job_id=? ORDER BY seq", job_id)
    for o in ops:
        rem = 0
        if o["hold_until"]:
            rem = max(0, (clock.parse(o["hold_until"]) - now).total_seconds())
        o["hold_remaining_seconds"] = rem
    job["ops"] = ops
    job["inspections"] = many(
        conn, "SELECT i.id, i.status, i.is_final, i.created_at, u.name AS inspector FROM inspections i "
              "JOIN users u ON u.id=i.inspector_id WHERE i.job_id=? ORDER BY i.id", job_id)
    job["gate_pass"] = one(conn, "SELECT * FROM gate_passes WHERE job_id=?", job_id)
    job["dispatch"] = dispatch_check(conn, job)
    return job


def create_job(ctx):
    b, conn = ctx.body, ctx.conn
    traveler_no = req_str(b, "traveler_no")
    part_id = req_int(b, "part_id")
    get_or_404(conn, "parts", part_id, "Part")
    ops = b.get("ops")
    need(isinstance(ops, list) and ops, 400, "A traveler needs at least one operation")
    clean = []
    for o in ops:
        name = (o.get("name") or "").strip() if isinstance(o, dict) else ""
        need(name, 400, "Every operation needs a name")
        hold = o.get("hold_minutes", 0)
        need(isinstance(hold, (int, float)) and not isinstance(hold, bool) and hold >= 0, 400,
             "hold_minutes must be a number >= 0")
        clean.append((name, hold))
    need(one(conn, "SELECT id FROM jobs WHERE traveler_no=?", traveler_no) is None, 409,
         f"Traveler {traveler_no} already exists")
    cur = conn.execute("INSERT INTO jobs(traveler_no, barcode, part_id, status, location, created_at)"
                       " VALUES (?,?,?,?,?,?)",
                       (traveler_no, f"JOB-{traveler_no}", part_id, "active", "Shopfloor", clock.iso()))
    jid = cur.lastrowid
    for i, (name, hold) in enumerate(clean, start=1):
        conn.execute("INSERT INTO job_ops(job_id, seq, name, hold_minutes) VALUES (?,?,?,?)",
                     (jid, i * 10, name, hold))
    audit(ctx, "job.create", "job", jid, None, {"traveler_no": traveler_no, "ops": len(clean)})
    return 201, job_detail(conn, jid)


def list_jobs(ctx):
    return many(ctx.conn, "SELECT j.id, j.traveler_no, j.barcode, j.status, j.location, p.part_no,"
                          " (SELECT COUNT(*) FROM job_ops o WHERE o.job_id=j.id AND o.status='done') AS ops_done,"
                          " (SELECT COUNT(*) FROM job_ops o WHERE o.job_id=j.id) AS ops_total"
                          " FROM jobs j JOIN parts p ON p.id=j.part_id ORDER BY j.id")


def get_job(ctx):
    return job_detail(ctx.conn, int(ctx.params["id"]))


def _op(conn, job_id, op_id):
    op = one(conn, "SELECT * FROM job_ops WHERE id=? AND job_id=?", op_id, job_id)
    need(op, 404, f"Operation {op_id} not found on job {job_id}")
    return op


def op_start(ctx):
    conn, job_id, op_id = ctx.conn, int(ctx.params["id"]), int(ctx.params["op_id"])
    job = get_or_404(conn, "jobs", job_id, "Job")
    need(job["status"] == "active", 409,
         f"Job is '{job['status']}' ({job['location']}); no operation may start")
    op = _op(conn, job_id, op_id)
    need(op["status"] == "pending", 409, f"Operation is already '{op['status']}'")
    prev = many(conn, "SELECT * FROM job_ops WHERE job_id=? AND seq<? ORDER BY seq", job_id, op["seq"])
    for p in prev:
        need(p["status"] == "done", 409, f"Op {p['seq']} ({p['name']}) must be completed first")
    now = clock.now()
    for p in prev:
        if p["hold_until"] and clock.parse(p["hold_until"]) > now:
            left = (clock.parse(p["hold_until"]) - now).total_seconds()
            raise ApiError(409, f"Mandatory Hold Active ({rules.format_remaining(left)} remaining) "
                                f"after Op {p['seq']} {p['name']}")
    conn.execute("UPDATE job_ops SET status='in_progress', started_at=?, started_by=? WHERE id=?",
                 (clock.iso(), ctx.user["id"], op_id))
    audit(ctx, "op.start", "job_op", op_id, {"status": "pending"}, {"status": "in_progress"})
    return job_detail(conn, job_id)


def op_complete(ctx):
    conn, job_id, op_id = ctx.conn, int(ctx.params["id"]), int(ctx.params["op_id"])
    job = get_or_404(conn, "jobs", job_id, "Job")
    need(job["status"] == "active", 409, f"Job is '{job['status']}'; operations are frozen")
    op = _op(conn, job_id, op_id)
    need(op["status"] == "in_progress", 409, f"Operation is '{op['status']}', it must be in progress")
    now = clock.now()
    hold_until = (now + timedelta(minutes=op["hold_minutes"])).isoformat() if op["hold_minutes"] > 0 else None
    conn.execute("UPDATE job_ops SET status='done', completed_at=?, completed_by=?, hold_until=? WHERE id=?",
                 (now.isoformat(), ctx.user["id"], hold_until, op_id))
    audit(ctx, "op.complete", "job_op", op_id, {"status": "in_progress"},
          {"status": "done", "hold_until": hold_until})
    return job_detail(conn, job_id)


# ----------------------------------------------------------------- inspections
def start_inspection(ctx):
    b, conn = ctx.body, ctx.conn
    qr = req_str(b, "machine_qr")
    barcode = req_str(b, "traveler_barcode")
    machine = one(conn, "SELECT * FROM machines WHERE qr_code=?", qr)
    need(machine, 404, "Unknown station QR code - scan the QR plate on the machine")
    job = one(conn, "SELECT * FROM jobs WHERE barcode=?", barcode)
    need(job, 404, "Unknown traveler barcode - scan the barcode on the Job Traveler sheet")
    need(job["status"] == "active", 409, f"Job is '{job['status']}' ({job['location']}); inspection is blocked")
    tpl = one(conn, "SELECT * FROM templates WHERE part_id=? AND status='released'", job["part_id"])
    need(tpl, 409, "No released inspection template exists for this part")
    open_one = one(conn, "SELECT id FROM inspections WHERE inspector_id=? AND status='in_progress'",
                   ctx.user["id"])
    need(open_one is None, 409, f"Finish and submit inspection #{open_one['id'] if open_one else ''} first - "
                                f"the tablet is locked to one job lot")
    cur = conn.execute("INSERT INTO inspections(job_id, template_id, machine_id, inspector_id, is_final, status,"
                       " created_at) VALUES (?,?,?,?,?,?,?)",
                       (job["id"], tpl["id"], machine["id"], ctx.user["id"], 1 if b.get("is_final") else 0,
                        "in_progress", clock.iso()))
    audit(ctx, "inspection.start", "inspection", cur.lastrowid, None,
          {"job": job["traveler_no"], "machine": machine["code"], "template_rev": tpl["rev"],
           "is_final": bool(b.get("is_final"))})
    return 201, inspection_detail(conn, cur.lastrowid)


def inspection_detail(conn, iid):
    insp = get_or_404(conn, "inspections", iid, "Inspection")
    fields = many(conn, "SELECT * FROM template_fields WHERE template_id=? ORDER BY seq", insp["template_id"])
    vals = {v["field_id"]: v for v in many(
        conn, "SELECT v.*, g.code AS gauge_code FROM inspection_values v LEFT JOIN gauges g ON g.id=v.gauge_id"
              " WHERE v.inspection_id=?", iid)}
    photos = {r["field_id"]: r["c"] for r in many(
        conn, "SELECT field_id, COUNT(*) AS c FROM inspection_photos WHERE inspection_id=? GROUP BY field_id", iid)}
    for f in fields:
        f["limits"] = rules.describe_limits(f)
        f["value"] = vals.get(f["id"])
        f["photo_count"] = photos.get(f["id"], 0)
    insp["fields"] = fields
    insp["job"] = one(conn, "SELECT id, traveler_no, status, location FROM jobs WHERE id=?", insp["job_id"])
    insp["inspector"] = one(conn, "SELECT name FROM users WHERE id=?", insp["inspector_id"])["name"]
    insp["concession"] = one(conn, "SELECT * FROM concessions WHERE inspection_id=?", iid)
    insp["fail_count"] = sum(1 for f in fields if f["value"] and f["value"]["result"] == "fail")
    insp["ctq_fail_count"] = sum(1 for f in fields if f["value"] and f["value"]["result"] == "fail" and f["ctq"])
    return insp


def get_inspection(ctx):
    return inspection_detail(ctx.conn, int(ctx.params["id"]))


def record_value(ctx):
    b, conn, iid = ctx.body, ctx.conn, int(ctx.params["id"])
    insp = get_or_404(conn, "inspections", iid, "Inspection")
    need(insp["inspector_id"] == ctx.user["id"], 403, "This inspection belongs to another inspector")
    need(insp["status"] == "in_progress", 409,
         f"Inspection is '{insp['status']}'; entries are locked")
    job = get_or_404(conn, "jobs", insp["job_id"], "Job")
    need(job["status"] == "active", 409, f"Job is '{job['status']}'; entries are blocked")
    field = one(conn, "SELECT * FROM template_fields WHERE id=? AND template_id=?",
                req_int(b, "field_id"), insp["template_id"])
    need(field, 404, "Field does not belong to this inspection's template")
    need("value" in b and b["value"] is not None and str(b["value"]).strip() != "", 400, "A value is required")

    gauge_id = None
    if field["kind"] != "visual":
        code = req_str(b, "gauge_code") if b.get("gauge_code") else None
        need(code, 400, "Scan or select the gauge serial number used for this measurement")
        gauge = one(conn, "SELECT * FROM gauges WHERE code=?", code)
        need(gauge, 400, f"Unknown gauge '{code}'")
        if gauge["cal_expiry"] < clock.now().date().isoformat():
            raise ApiError(400, f"Expired Tool Calibration: {code} expired on {gauge['cal_expiry']}. "
                                f"Obtain a calibrated tool from the tool crib.")
        gauge_id = gauge["id"]

    try:
        raw, passed = rules.evaluate(field, b["value"])
    except ValueError as e:
        raise ApiError(400, str(e))

    photos = b.get("photos") or []
    need(isinstance(photos, list), 400, "'photos' must be a list")
    if not passed:
        valid = [p for p in photos if isinstance(p, dict) and str(p.get("name") or "").strip()]
        need(len(valid) >= 2, 400,
             "Out of tolerance: at least two clear defect/gauge photos are required before continuing")
        for p in valid:
            need(len(str(p.get("data") or "")) <= MAX_PHOTO_CHARS, 400, "Photo too large")

    old = one(conn, "SELECT raw_value, result FROM inspection_values WHERE inspection_id=? AND field_id=?",
              iid, field["id"])
    conn.execute("INSERT INTO inspection_values(inspection_id, field_id, raw_value, result, gauge_id) "
                 "VALUES (?,?,?,?,?) ON CONFLICT(inspection_id, field_id) DO UPDATE SET "
                 "raw_value=excluded.raw_value, result=excluded.result, gauge_id=excluded.gauge_id",
                 (iid, field["id"], raw, "pass" if passed else "fail", gauge_id))
    if not passed:
        for p in valid:
            conn.execute("INSERT INTO inspection_photos(inspection_id, field_id, filename, data) VALUES (?,?,?,?)",
                         (iid, field["id"], str(p["name"]).strip(), p.get("data")))
    audit(ctx, "inspection.value", "inspection", iid, old,
          {"field": field["label"], "value": raw, "result": "pass" if passed else "fail",
           "gauge_id": gauge_id, "ctq": bool(field["ctq"])})

    frozen = False
    if not passed and field["ctq"]:
        conn.execute("UPDATE inspections SET status='frozen', submitted_at=? WHERE id=?", (clock.iso(), iid))
        alert(conn, "quality_manager",
              f"CTQ failure on job {job['traveler_no']}: {field['label']} = {raw} "
              f"(allowed {rules.describe_limits(field)}). Checklist frozen.")
        audit(ctx, "inspection.freeze", "inspection", iid, {"status": "in_progress"},
              {"status": "frozen", "reason": f"CTQ out of tolerance: {field['label']}"})
        frozen = True
    remaining = one(conn, "SELECT COUNT(*) AS c FROM template_fields f WHERE f.template_id=? AND NOT EXISTS "
                          "(SELECT 1 FROM inspection_values v WHERE v.inspection_id=? AND v.field_id=f.id)",
                    insp["template_id"], iid)["c"]
    return {"in_tolerance": passed, "result": "pass" if passed else "fail", "value": raw,
            "ctq": bool(field["ctq"]), "frozen": frozen, "remaining_fields": remaining,
            "inspection_status": "frozen" if frozen else "in_progress"}


def submit_inspection(ctx):
    conn, iid = ctx.conn, int(ctx.params["id"])
    insp = get_or_404(conn, "inspections", iid, "Inspection")
    need(insp["inspector_id"] == ctx.user["id"], 403, "This inspection belongs to another inspector")
    need(insp["status"] == "in_progress", 409, f"Inspection is '{insp['status']}' and cannot be submitted")
    missing = many(conn, "SELECT f.label FROM template_fields f WHERE f.template_id=? AND NOT EXISTS "
                         "(SELECT 1 FROM inspection_values v WHERE v.inspection_id=? AND v.field_id=f.id) "
                         "ORDER BY f.seq", insp["template_id"], iid)
    need(not missing, 400, "Cannot skip fields. Still to measure: " + ", ".join(m["label"] for m in missing))
    conn.execute("UPDATE inspections SET status='pending_review', submitted_at=? WHERE id=?", (clock.iso(), iid))
    audit(ctx, "inspection.submit", "inspection", iid, {"status": "in_progress"}, {"status": "pending_review"})
    return inspection_detail(conn, iid)


def review_queue(ctx):
    return many(ctx.conn, """
        SELECT i.id, i.status, i.is_final, i.submitted_at, j.traveler_no, u.name AS inspector,
          (SELECT COUNT(*) FROM inspection_values v WHERE v.inspection_id=i.id AND v.result='fail') AS fails,
          (SELECT COUNT(*) FROM inspection_values v JOIN template_fields f ON f.id=v.field_id
             WHERE v.inspection_id=i.id AND v.result='fail' AND f.ctq=1) AS ctq_fails
        FROM inspections i JOIN jobs j ON j.id=i.job_id JOIN users u ON u.id=i.inspector_id
        WHERE i.status IN ('pending_review','frozen') ORDER BY i.id""")


def review_inspection(ctx):
    b, conn, iid = ctx.body, ctx.conn, int(ctx.params["id"])
    insp = get_or_404(conn, "inspections", iid, "Inspection")
    need(insp["status"] in ("pending_review", "frozen"), 409,
         f"Inspection is '{insp['status']}' and is not awaiting review")
    need(insp["inspector_id"] != ctx.user["id"], 403, "You cannot review your own inspection")
    decision = b.get("decision")
    need(decision in ("approve", "concession", "reject"), 400, "decision must be approve, concession or reject")
    detail = inspection_detail(conn, iid)
    job = detail["job"]
    notes = (b.get("notes") or "").strip() or None

    if decision == "approve":
        need(job["status"] == "active", 409, f"Job is '{job['status']}'; it cannot be approved")
        need(insp["status"] == "pending_review", 409, "A frozen (CTQ failure) inspection can only be rejected")
        need(detail["fail_count"] == 0, 400,
             f"{detail['fail_count']} measurement(s) are out of tolerance: approval is not allowed. "
             f"Issue a concession (non-CTQ only) or reject the lot.")
        new_status = "approved"
    elif decision == "concession":
        need(detail["ctq_fail_count"] == 0 and insp["status"] != "frozen", 403,
             "CTQ failure: a concession is not permitted. The lot must be rejected.")
        need(detail["fail_count"] > 0, 400, "Nothing is out of tolerance, so no concession is needed - approve instead")
        need(job["status"] == "active", 409, f"Job is '{job['status']}'; it cannot be released")
        reason = b.get("reason_code")
        need(reason in CONCESSION_REASONS, 400, "reason_code must be one of " + ", ".join(CONCESSION_REASONS))
        just = (b.get("justification") or "").strip()
        need(len(just) >= 10, 400, "A detailed engineering justification (min 10 characters) is required")
        conn.execute("INSERT INTO concessions(inspection_id, reason_code, justification, granted_by, created_at)"
                     " VALUES (?,?,?,?,?)", (iid, reason, just, ctx.user["id"], clock.iso()))
        new_status = "conceded"
    else:
        new_status = "rejected"
        if job["status"] == "active":
            conn.execute("UPDATE jobs SET status='quarantined', location='Digital Rejection Holding Area' WHERE id=?",
                         (job["id"],))
            audit(ctx, "job.quarantine", "job", job["id"], {"status": "active"},
                  {"status": "quarantined", "location": "Digital Rejection Holding Area"})
    conn.execute("UPDATE inspections SET status=?, reviewed_by=?, reviewed_at=?, review_notes=? WHERE id=?",
                 (new_status, ctx.user["id"], clock.iso(), notes, iid))
    audit(ctx, f"inspection.{decision}", "inspection", iid, {"status": insp["status"]}, {"status": new_status})
    return inspection_detail(conn, iid)


# ------------------------------------------------------------------------- MRB
def _set_job(ctx, job, status, location, action, extra=None):
    ctx.conn.execute("UPDATE jobs SET status=?, location=? WHERE id=?", (status, location, job["id"]))
    audit(ctx, action, "job", job["id"], {"status": job["status"], "location": job["location"]},
          dict({"status": status, "location": location}, **(extra or {})))


def mrb_disposition(ctx):
    b, conn, jid = ctx.body, ctx.conn, int(ctx.params["id"])
    job = get_or_404(conn, "jobs", jid, "Job")
    need(job["status"] == "quarantined", 409, f"Job is '{job['status']}'; only quarantined lots go to MRB")
    d = b.get("disposition")
    need(d in MRB_DISPOSITIONS, 400, "disposition must be one of " + ", ".join(MRB_DISPOSITIONS))
    notes = (b.get("notes") or "").strip()
    need(notes, 400, "MRB notes are required")
    if d == "rework":
        last = one(conn, "SELECT MAX(seq) AS m FROM job_ops WHERE job_id=?", jid)["m"] or 0
        conn.execute("INSERT INTO job_ops(job_id, seq, name, hold_minutes) VALUES (?,?,?,0)",
                     (jid, last + 10, "Rework (MRB)"))
        _set_job(ctx, job, "active", "Rework Cell", "mrb.rework")
    elif d == "scrap":
        _set_job(ctx, job, "scrapped", "Scrap Bin", "mrb.scrap")
    elif d == "rtv":
        _set_job(ctx, job, "returned_to_vendor", "Dock - Vendor Return", "mrb.rtv")
    else:
        _set_job(ctx, job, "held_customer_approval", "Held - Awaiting Customer Approval", "mrb.customer_concession")
    conn.execute("INSERT INTO mrb_dispositions(job_id, disposition, notes, decided_by, created_at) VALUES (?,?,?,?,?)",
                 (jid, d, notes, ctx.user["id"], clock.iso()))
    return job_detail(conn, jid)


def customer_approval(ctx):
    conn, jid = ctx.conn, int(ctx.params["id"])
    job = get_or_404(conn, "jobs", jid, "Job")
    need(job["status"] == "held_customer_approval", 409, "Job is not waiting for customer approval")
    ref = req_str(ctx.body, "reference")
    conn.execute("INSERT INTO mrb_dispositions(job_id, disposition, notes, decided_by, created_at) VALUES (?,?,?,?,?)",
                 (jid, "customer_approval_received", ref, ctx.user["id"], clock.iso()))
    _set_job(ctx, job, "active", "Shopfloor", "mrb.customer_approval", {"reference": ref})
    return job_detail(conn, jid)


# --------------------------------------------------------------------- dispatch
def dispatch_check(conn, job):
    now = clock.now()
    ops = many(conn, "SELECT * FROM job_ops WHERE job_id=? ORDER BY seq", job["id"])
    pending = [f"Op {o['seq']} {o['name']}" for o in ops if o["status"] != "done"]
    if not ops:
        c1 = (False, "The traveler has no operations")
    elif pending:
        c1 = (False, "Operations not signed off: " + ", ".join(pending))
    else:
        c1 = (True, "All operations signed off")

    blockers = []
    for o in ops:
        if o["hold_until"] and clock.parse(o["hold_until"]) > now:
            left = (clock.parse(o["hold_until"]) - now).total_seconds()
            blockers.append(f"Op {o['seq']} {o['name']} hold active ({rules.format_remaining(left)} remaining)")
    if job["status"] != "active":
        blockers.append(f"Lot is '{job['status']}' ({job['location']})")
    for r in many(conn, "SELECT id FROM inspections WHERE job_id=? AND status='frozen'", job["id"]):
        blockers.append(f"Inspection #{r['id']} is frozen by a CTQ failure")
    c2 = (not blockers, "No outstanding holds" if not blockers else "; ".join(blockers))

    final = one(conn, "SELECT * FROM inspections WHERE job_id=? AND is_final=1 ORDER BY id DESC LIMIT 1", job["id"])
    if not final:
        c3 = (False, "No final PDI has been performed")
    elif final["status"] in ("approved", "conceded"):
        c3 = (True, f"Final PDI #{final['id']} {final['status']}")
    else:
        c3 = (False, f"Final PDI #{final['id']} is '{final['status']}', not approved")
    conds = [{"key": k, "label": lbl, "ok": ok, "detail": det} for (k, lbl), (ok, det) in zip(
        [("operations_complete", "All operations signed off"), ("holds_clear", "Hold times and quality holds clear"),
         ("final_pdi_approved", "Final PDI approved")], [c1, c2, c3])]
    return {"ok": all(c["ok"] for c in conds), "conditions": conds}


def get_dispatch_check(ctx):
    return dispatch_check(ctx.conn, get_or_404(ctx.conn, "jobs", int(ctx.params["id"]), "Job"))


def issue_gate_pass(ctx):
    conn, jid = ctx.conn, int(ctx.params["id"])
    job = get_or_404(conn, "jobs", jid, "Job")
    existing = one(conn, "SELECT * FROM gate_passes WHERE job_id=?", jid)
    need(existing is None, 409, f"Gate pass {existing['pass_no'] if existing else ''} was already issued")
    check = dispatch_check(conn, job)
    if not check["ok"]:
        unmet = [c["detail"] for c in check["conditions"] if not c["ok"]]
        raise ApiError(409, {"message": "Shipping Interlock - gate pass is locked", "unmet": unmet})
    pass_no = f"GP-{job['traveler_no']}"
    conn.execute("INSERT INTO gate_passes(job_id, pass_no, issued_by, issued_at) VALUES (?,?,?,?)",
                 (jid, pass_no, ctx.user["id"], clock.iso()))
    audit(ctx, "gate_pass.issue", "job", jid, None, {"pass_no": pass_no})
    return 201, job_detail(conn, jid)


# ------------------------------------------------------------------ audit/alerts
def list_audit(ctx):
    sql, args = "SELECT * FROM audit_log", []
    if ctx.query.get("entity"):
        sql += " WHERE entity=?"
        args.append(ctx.query["entity"])
    limit = min(int(ctx.query.get("limit", 200)), 1000)
    return many(ctx.conn, sql + " ORDER BY id DESC LIMIT ?", *args, limit)


def list_alerts(ctx):
    return many(ctx.conn, "SELECT * FROM alerts ORDER BY id DESC LIMIT 200")


# ---------------------------------------------------------------------- routing
ANY = "any"
R_ENG, R_QM, R_PM = ("quality_engineer",), ("quality_manager",), ("plant_manager",)
R_REVIEW = ("supervisor", "quality_manager")

ROUTES = [
    ("GET", r"/api/users", None, list_users),
    ("GET", r"/api/gauges", ANY, list_gauges),
    ("GET", r"/api/machines", ANY, list_machines),
    ("GET", r"/api/parts", ANY, list_parts),
    ("GET", r"/api/templates", ANY, list_templates),
    ("POST", r"/api/templates", R_ENG, create_template),
    ("GET", r"/api/templates/(?P<id>\d+)", ANY, get_template),
    ("POST", r"/api/templates/(?P<id>\d+)/release", R_QM, release_template),
    ("GET", r"/api/jobs", ANY, list_jobs),
    ("POST", r"/api/jobs", ("plant_manager", "supervisor"), create_job),
    ("GET", r"/api/jobs/(?P<id>\d+)", ANY, get_job),
    ("POST", r"/api/jobs/(?P<id>\d+)/ops/(?P<op_id>\d+)/start", ("operator", "supervisor"), op_start),
    ("POST", r"/api/jobs/(?P<id>\d+)/ops/(?P<op_id>\d+)/complete", ("operator", "supervisor"), op_complete),
    ("POST", r"/api/jobs/(?P<id>\d+)/mrb", ("quality_manager", "plant_manager"), mrb_disposition),
    ("POST", r"/api/jobs/(?P<id>\d+)/customer-approval", R_QM, customer_approval),
    ("GET", r"/api/jobs/(?P<id>\d+)/dispatch-check", ANY, get_dispatch_check),
    ("POST", r"/api/jobs/(?P<id>\d+)/gate-pass", ("warehouse_clerk",), issue_gate_pass),
    ("POST", r"/api/inspections", ("inspector",), start_inspection),
    ("GET", r"/api/inspections/(?P<id>\d+)", ANY, get_inspection),
    ("POST", r"/api/inspections/(?P<id>\d+)/values", ("inspector",), record_value),
    ("POST", r"/api/inspections/(?P<id>\d+)/submit", ("inspector",), submit_inspection),
    ("GET", r"/api/review-queue", R_REVIEW, review_queue),
    ("POST", r"/api/inspections/(?P<id>\d+)/review", R_REVIEW, review_inspection),
    ("GET", r"/api/audit", ("plant_manager", "quality_manager"), list_audit),
    ("GET", r"/api/alerts", ("plant_manager", "quality_manager"), list_alerts),
]
_COMPILED = [(m, re.compile(p), roles, fn) for m, p, roles, fn in ROUTES]


def _authenticate(conn, headers, ip):
    uid = (headers.get("x-user-id") or "").strip()
    if not uid.isdigit():
        raise ApiError(401, "Select a user (X-User-Id header is required)")
    u = one(conn, "SELECT id, name, role FROM users WHERE id=?", int(uid))
    need(u, 401, "Unknown user")
    u["ip"], u["device"] = ip, headers.get("x-device-serial")
    return u


def dispatch(method, path, query, body, headers, ip=None):
    """Run one request. Returns (status, json-serialisable payload)."""
    for m, rx, roles, fn in _COMPILED:
        mt = rx.fullmatch(path)
        if m != method or not mt:
            continue
        conn = db.connect()
        try:
            user = None
            if roles is not None:
                user = _authenticate(conn, headers, ip)
                if roles != ANY and user["role"] not in roles:
                    raise ApiError(403, f"Role '{user['role']}' is not allowed to do this")
            if method == "POST" and not isinstance(body, dict):
                raise ApiError(400, "JSON object body required")
            result = fn(Ctx(conn, user, body or {}, mt.groupdict(), query,
                            user and user["ip"], user and user["device"]))
            conn.commit()
            return result if isinstance(result, tuple) else (200, result)
        except ApiError as e:
            conn.rollback()
            return e.status, {"detail": e.detail}
        except sqlite3.IntegrityError as e:
            conn.rollback()
            return 409, {"detail": f"Data integrity rule violated: {e}"}
        finally:
            conn.close()
    return 404, {"detail": "Not found"}

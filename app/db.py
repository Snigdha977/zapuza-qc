"""SQLite schema, immutability triggers and seed data."""
import os
import sqlite3
from datetime import timedelta

from . import clock

SCHEMA = """
CREATE TABLE IF NOT EXISTS users(
  id INTEGER PRIMARY KEY, name TEXT UNIQUE NOT NULL, role TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS gauges(
  id INTEGER PRIMARY KEY, code TEXT UNIQUE NOT NULL, description TEXT, cal_expiry TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS machines(
  id INTEGER PRIMARY KEY, code TEXT UNIQUE NOT NULL, qr_code TEXT UNIQUE NOT NULL);
CREATE TABLE IF NOT EXISTS parts(
  id INTEGER PRIMARY KEY, part_no TEXT UNIQUE NOT NULL, name TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS templates(
  id INTEGER PRIMARY KEY, part_id INTEGER NOT NULL REFERENCES parts(id), rev TEXT NOT NULL,
  status TEXT NOT NULL CHECK(status IN ('draft','released','superseded')),
  created_by INTEGER REFERENCES users(id), approved_by INTEGER REFERENCES users(id),
  created_at TEXT, UNIQUE(part_id, rev));
CREATE TABLE IF NOT EXISTS template_fields(
  id INTEGER PRIMARY KEY, template_id INTEGER NOT NULL REFERENCES templates(id), seq INTEGER NOT NULL,
  label TEXT NOT NULL, kind TEXT NOT NULL CHECK(kind IN ('range','max','min','visual')),
  nominal TEXT, upper_tol TEXT, lower_tol TEXT, limit_value TEXT, unit TEXT,
  ctq INTEGER NOT NULL DEFAULT 0, UNIQUE(template_id, seq));
CREATE TABLE IF NOT EXISTS jobs(
  id INTEGER PRIMARY KEY, traveler_no TEXT UNIQUE NOT NULL, barcode TEXT UNIQUE NOT NULL,
  part_id INTEGER NOT NULL REFERENCES parts(id),
  status TEXT NOT NULL CHECK(status IN ('active','quarantined','scrapped','returned_to_vendor','held_customer_approval')),
  location TEXT NOT NULL DEFAULT 'Shopfloor', created_at TEXT);
CREATE TABLE IF NOT EXISTS job_ops(
  id INTEGER PRIMARY KEY, job_id INTEGER NOT NULL REFERENCES jobs(id), seq INTEGER NOT NULL,
  name TEXT NOT NULL, hold_minutes REAL NOT NULL DEFAULT 0,
  status TEXT NOT NULL DEFAULT 'pending' CHECK(status IN ('pending','in_progress','done')),
  started_at TEXT, completed_at TEXT, hold_until TEXT, started_by INTEGER, completed_by INTEGER,
  UNIQUE(job_id, seq));
CREATE TABLE IF NOT EXISTS inspections(
  id INTEGER PRIMARY KEY, job_id INTEGER NOT NULL REFERENCES jobs(id),
  template_id INTEGER NOT NULL REFERENCES templates(id), machine_id INTEGER NOT NULL REFERENCES machines(id),
  inspector_id INTEGER NOT NULL REFERENCES users(id), is_final INTEGER NOT NULL DEFAULT 0,
  status TEXT NOT NULL CHECK(status IN ('in_progress','frozen','pending_review','approved','conceded','rejected')),
  created_at TEXT, submitted_at TEXT, reviewed_by INTEGER, reviewed_at TEXT, review_notes TEXT);
CREATE TABLE IF NOT EXISTS inspection_values(
  id INTEGER PRIMARY KEY, inspection_id INTEGER NOT NULL REFERENCES inspections(id),
  field_id INTEGER NOT NULL REFERENCES template_fields(id), raw_value TEXT NOT NULL,
  result TEXT NOT NULL CHECK(result IN ('pass','fail')), gauge_id INTEGER REFERENCES gauges(id),
  UNIQUE(inspection_id, field_id));
CREATE TABLE IF NOT EXISTS inspection_photos(
  id INTEGER PRIMARY KEY, inspection_id INTEGER NOT NULL REFERENCES inspections(id),
  field_id INTEGER NOT NULL, filename TEXT NOT NULL, data TEXT);
CREATE TABLE IF NOT EXISTS concessions(
  id INTEGER PRIMARY KEY, inspection_id INTEGER NOT NULL REFERENCES inspections(id),
  reason_code TEXT NOT NULL, justification TEXT NOT NULL, granted_by INTEGER NOT NULL, created_at TEXT);
CREATE TABLE IF NOT EXISTS mrb_dispositions(
  id INTEGER PRIMARY KEY, job_id INTEGER NOT NULL REFERENCES jobs(id), disposition TEXT NOT NULL,
  notes TEXT, decided_by INTEGER NOT NULL, created_at TEXT);
CREATE TABLE IF NOT EXISTS alerts(
  id INTEGER PRIMARY KEY, ts TEXT, channel TEXT, recipient_role TEXT, message TEXT);
CREATE TABLE IF NOT EXISTS gate_passes(
  id INTEGER PRIMARY KEY, job_id INTEGER UNIQUE NOT NULL REFERENCES jobs(id),
  pass_no TEXT UNIQUE NOT NULL, issued_by INTEGER NOT NULL, issued_at TEXT);
CREATE TABLE IF NOT EXISTS audit_log(
  id INTEGER PRIMARY KEY, ts TEXT NOT NULL, user_id INTEGER, user_name TEXT, action TEXT NOT NULL,
  entity TEXT NOT NULL, entity_id TEXT, old_value TEXT, new_value TEXT, ip TEXT, device_serial TEXT);

-- Immutability: the audit log is append-only; submitted inspection data is frozen.
CREATE TRIGGER IF NOT EXISTS audit_no_update BEFORE UPDATE ON audit_log
  BEGIN SELECT RAISE(ABORT,'audit_log is append-only'); END;
CREATE TRIGGER IF NOT EXISTS audit_no_delete BEFORE DELETE ON audit_log
  BEGIN SELECT RAISE(ABORT,'audit_log is append-only'); END;
CREATE TRIGGER IF NOT EXISTS iv_no_delete BEFORE DELETE ON inspection_values
  BEGIN SELECT RAISE(ABORT,'inspection data cannot be deleted'); END;
CREATE TRIGGER IF NOT EXISTS iv_lock_update BEFORE UPDATE ON inspection_values
  WHEN (SELECT status FROM inspections WHERE id=OLD.inspection_id) != 'in_progress'
  BEGIN SELECT RAISE(ABORT,'inspection data is locked'); END;
CREATE TRIGGER IF NOT EXISTS iv_lock_insert BEFORE INSERT ON inspection_values
  WHEN (SELECT status FROM inspections WHERE id=NEW.inspection_id) != 'in_progress'
  BEGIN SELECT RAISE(ABORT,'inspection data is locked'); END;
CREATE TRIGGER IF NOT EXISTS ip_no_delete BEFORE DELETE ON inspection_photos
  BEGIN SELECT RAISE(ABORT,'inspection photos cannot be deleted'); END;
CREATE TRIGGER IF NOT EXISTS insp_no_delete BEFORE DELETE ON inspections
  BEGIN SELECT RAISE(ABORT,'inspections cannot be deleted'); END;
"""


def db_path() -> str:
    return os.environ.get("QC_DB", "qc.db")


def connect() -> sqlite3.Connection:
    conn = sqlite3.connect(db_path(), timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db(seed: bool = True) -> None:
    conn = connect()
    try:
        conn.executescript(SCHEMA)
        if seed and conn.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 0:
            seed_data(conn)
        conn.commit()
    finally:
        conn.close()


ROLES = [
    ("Priya (Plant Manager)", "plant_manager"),
    ("Ravi (Quality Engineer)", "quality_engineer"),
    ("Meera (Quality Manager)", "quality_manager"),
    ("Arjun (Shift Supervisor)", "supervisor"),
    ("Isha (QA Inspector)", "inspector"),
    ("Imran (QA Inspector)", "inspector"),
    ("Omar (Machine Operator)", "operator"),
    ("Wendy (Warehouse Clerk)", "warehouse_clerk"),
]

TRAVELER_OPS = [("Raw Material Sawing", 0), ("CNC Rough Turning", 0),
                ("Heat Treatment", 1), ("Precision Grinding", 0),
                ("Final PDI & Wash", 0), ("Packing", 0)]


def seed_data(conn: sqlite3.Connection) -> None:
    now = clock.now()
    today = now.date()
    iso = now.isoformat()
    conn.executemany("INSERT INTO users(name, role) VALUES (?,?)", ROLES)
    conn.executemany("INSERT INTO gauges(code, description, cal_expiry) VALUES (?,?,?)", [
        ("VC-104", "Vernier Caliper", (today + timedelta(days=180)).isoformat()),
        ("VC-12", "Vernier Caliper (EXPIRED)", (today - timedelta(days=20)).isoformat()),
        ("PG-04", "Thread Plug Gauge", (today + timedelta(days=90)).isoformat()),
        ("MIC-07", "Micrometer", (today + timedelta(days=365)).isoformat()),
    ])
    conn.executemany("INSERT INTO machines(code, qr_code) VALUES (?,?)", [
        ("CNC Lathe #04", "QR-MCH-CNC04"), ("VMC Station #02", "QR-MCH-VMC02")])
    conn.execute("INSERT INTO parts(part_no, name) VALUES ('SHAFT-25','Drive Shaft 25 mm')")
    conn.execute("INSERT INTO templates(part_id, rev, status, created_by, approved_by, created_at) "
                 "VALUES (1,'A','released',2,3,?)", (iso,))
    fields = [
        (1, "Shaft Diameter", "range", "25.00", "0.05", "-0.02", None, "mm", 1),
        (2, "Thread Pitch", "range", "1.50", "0.02", "-0.02", None, "mm", 0),
        (3, "Surface Roughness Ra", "max", None, None, None, "0.8", "um", 0),
        (4, "Runout", "max", None, None, None, "0.02", "mm", 0),
        (5, "Free of burrs", "visual", None, None, None, None, "", 0),
    ]
    conn.executemany("INSERT INTO template_fields(template_id, seq, label, kind, nominal, upper_tol, "
                     "lower_tol, limit_value, unit, ctq) VALUES (1,?,?,?,?,?,?,?,?,?)", fields)
    for n in (1001, 1002, 1003):
        conn.execute("INSERT INTO jobs(traveler_no, barcode, part_id, status, location, created_at) "
                     "VALUES (?,?,1,'active','Shopfloor',?)", (str(n), f"JOB-{n}", iso))
        for i, (name, hold) in enumerate(TRAVELER_OPS):
            conn.execute("INSERT INTO job_ops(job_id, seq, name, hold_minutes) VALUES (?,?,?,?)",
                         (n - 1000, (i + 1) * 10, name, hold))

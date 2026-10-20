# Zapuza QC - Factory Operations & Quality Control (take-home build)

A working slice of the Zapuza *Factory Operations & Quality Control* PRD (v5.0). I chose depth over breadth:
the features below are implemented end-to-end with server-side enforcement (every lock is enforced in the API,
not just by hiding buttons) and covered by 30 automated tests.

**Demo video:** `<ADD YOUR VIDEO LINK HERE>`

## What is implemented

| # | Feature (PRD section) | Rules enforced |
|---|---|---|
| 1 | **PDI templates + inspector workflow** (4, 5) | Versioned templates (Rev A/B); engineer drafts, a *different* Quality Manager releases; releasing a new revision supersedes the old one. Field types: range (nominal +upper/-lower), max, min, visual Yes/No, with CTQ flag. Two QR scans required (station + traveler) - no manual search. Tablet locks to one open inspection per inspector. No skipped fields. Instant green/red result. Out-of-tolerance needs **>= 2 photos**. Gauge serial mandatory and **expired calibration is blocked**. |
| 2 | **CTQ + supervisor review + rejection holding area** (4.3, 6) | CTQ failure **freezes** the checklist and raises an email/SMS alert record to the Quality Manager; a frozen inspection can only be rejected. Approve is impossible while anything is out of tolerance. **Concession** needs reason code + justification and is **refused for CTQ**. Reject moves the lot to *Quarantined / Digital Rejection Holding Area* and blocks all operations and inspections. MRB dispositions: rework, scrap, return-to-vendor, customer concession (+ approval upload). |
| 3 | **Digital traveler + hold timers** (7, 8) | Operations must be started/completed in numeric order. Completing a hold-designated op starts a countdown; starting the next op is refused with *"Mandatory Hold Active (1 hr 22 min remaining)"*. Live countdown in the UI. |
| 4 | **Shipping interlock** (10) | Gate pass is refused unless (a) all ops are signed off, (b) no active hold / quarantine / frozen inspection, (c) the latest **final** PDI is Approved (or conceded). The lockout lists the exact unmet requirement(s). |
| + | **Immutable audit trail** (13.3) | Every state change writes user, time, IP, device serial, old value, new value in the **same transaction**. SQLite triggers make the audit log append-only and block deleting/editing submitted inspection data. |

## Run it

Requires only **Python 3.10+** (standard library; no `pip install`).

```bash
python3 -m app.server          # http://127.0.0.1:8000  (PORT=9000 to change)
python3 -m unittest discover -s tests -t .   # run all 30 tests
```

The database (`qc.db`) is created and seeded on first start; delete the file to reset the demo.

### Seeded demo data
* Users (pick from the dropdown - the role decides what the API allows): Plant Manager, Quality Engineer, Quality Manager, Supervisor, 2 Inspectors, Operator, Warehouse Clerk.
* Station QR codes: `QR-MCH-CNC04`, `QR-MCH-VMC02`. Travelers: `JOB-1001`, `JOB-1002`, `JOB-1003` (Ops 10-60, a **1-minute** hold after Heat Treatment so it is demo-able).
* Gauges: `VC-104`, `PG-04`, `MIC-07` valid; **`VC-12` expired**.
* Template Rev A for `SHAFT-25`: Diameter 25.00 +0.05/-0.02 **(CTQ)**, Thread Pitch 1.50 +/-0.02, Ra max 0.8, Runout max 0.02, Free of burrs (Yes/No).

### Suggested demo script (about 5 min)
1. **Inspector** -> Inspect -> scan `QR-MCH-CNC04` + `JOB-1001`. Try gauge `VC-12` (blocked). Enter `25.06` for diameter (CTQ) with 2 photos -> frozen + alert.
2. **Supervisor** -> Review: concession is refused, reject -> lot appears quarantined. **Quality Manager** -> Jobs -> MRB -> rework.
3. New inspection on `JOB-1002`: pitch `1.60` (non-CTQ fail) -> submit -> concession with reason code.
4. **Operator** -> Jobs: try Op 20 before Op 10 (blocked); complete Op 30 and try Op 40 immediately (hold countdown).
5. **Warehouse Clerk** -> try the gate pass (locked, with reasons) -> finish ops + final PDI approval -> issue the gate pass.
6. **Plant Manager** -> Audit & Alerts.

## Design notes

* `app/rules.py` - pure tolerance maths using `Decimal` (so 25.05 vs a 25.00 +0.05 limit is exact), field validation, hold formatting.
* `app/api.py` - all business rules + routing; each request is one DB transaction, so a rejected action leaves no data and no audit row behind.
* `app/db.py` - schema, immutability triggers, seed data. `app/server.py` - thin `http.server` wrapper. `app/static/index.html` - tablet-style UI with large buttons and CTQ highlighted in orange.
* `app/clock.py` - injectable clock so the hold timers and calibration expiry are tested deterministically.
* No framework on purpose: the sandbox this was built in could not install packages, and it keeps setup to one command.

## Assumptions / decisions where the PRD is ambiguous

* **Frozen (CTQ) inspections** go straight to the supervisor queue and can only be *rejected*; the inspector cannot add more entries.
* A **conceded** final PDI counts as cleared for dispatch (a concession is a conditional *release*); strict "Approved only" is a one-line change in `dispatch_check`.
* A calibration is valid **through** its expiry date and blocked the day after.
* **Rework** appends a "Rework (MRB)" operation and returns the lot to active; a **new final PDI** is required afterwards because the latest final PDI decides the gate. The same applies after customer approval.
* Alerts (email/SMS) are stored as records; actual delivery is out of scope. QR scanning is simulated by typing/pasting the code.
* Templates are immutable once created (no edit endpoint): a drawing change means a new revision. A template author cannot approve their own template.
* The PRD's duplicated/misnumbered sections (two "5.1", stray "7.1/7.2") were read by intent.

## Not implemented (out of scope)

Raw-material/MTC intake, blueprint pinning (PDF drawing upload), machine PM lock, first-piece and hourly sampling prompts, shift handover, bottleneck heatmaps, forward/backward traceability queries, customer QR certificates, offline sync, real authentication (the demo uses a user picker via the `X-User-Id` header).

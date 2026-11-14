"""ITSM core: incident, problem and knowledge management (ITIL 4 practices)."""
import time

from . import config

OPEN = ("new", "acknowledged")
LEVELS = ["L1", "L2", "L3"]
PROBLEM_STATES = ("new", "investigating", "known_error", "resolved")


class KB:
    def __init__(self, db):
        self.db = db

    def add(self, title, body, tags=""):
        if not title or not body:
            raise ValueError("title and body are required")
        return self.db.x("INSERT INTO kb(title,body,tags,created_at) VALUES(?,?,?,?)",
                         (title, body, tags, time.time()))

    def search(self, q=""):
        q = (q or "").strip().lower()
        rows = self.db.q("SELECT * FROM kb ORDER BY id DESC")
        if not q:
            return rows
        return [r for r in rows if q in (r["title"] + " " + r["body"] + " " + r["tags"]).lower()]


class ITSM:
    def __init__(self, db, kb):
        self.db, self.kb = db, kb

    # ---- incidents ----------------------------------------------------------------
    def _log(self, iid, note):
        self.db.x("INSERT INTO incident_log(incident_id,ts,note) VALUES(?,?,?)", (iid, time.time(), note))

    def create_incident(self, title, description="", priority="P3", source="manual", auto=False):
        if not title:
            raise ValueError("title is required")
        if priority not in config.SLA_MINUTES:
            raise ValueError("priority must be P1, P2, P3 or P4")
        now = time.time()
        sla_min = config.SLA_MINUTES[priority]
        level = "L2" if priority == "P1" else "L1"
        iid = self.db.x(
            "INSERT INTO incidents(title,description,priority,status,source,level,created_at,sla_due,auto) "
            "VALUES(?,?,?,?,?,?,?,?,?)",
            (title, description, priority, "new", source, level, now,
             now + sla_min * 60 * config.SLA_SCALE, 1 if auto else 0))
        self._log(iid, "Logged as %s (SLA %d min), routed to %s%s"
                  % (priority, sla_min, level, " by automated monitoring" if auto else ""))
        if source != "manual":
            for a in self.kb.search(source)[:2]:
                self._log(iid, "Suggested KB article #%d: %s" % (a["id"], a["title"]))
            self._link_problem(iid, source)
        return iid

    def raise_or_update(self, source, title, description, priority, auto=True):
        """Deduplicate: one open incident per source (avoids alert storms)."""
        existing = self.db.one("SELECT id FROM incidents WHERE source=? AND status IN ('new','acknowledged')",
                               (source,))
        if existing:
            return existing["id"], False
        return self.create_incident(title, description, priority, source, auto), True

    def _link_problem(self, iid, source):
        prob = self.db.one("SELECT id FROM problems WHERE source=? AND status!='resolved'", (source,))
        if prob:
            self.db.x("UPDATE incidents SET problem_id=? WHERE id=?", (prob["id"], iid))
            self._log(iid, "Linked to open problem #%d" % prob["id"])
            return
        n = self.db.one("SELECT COUNT(*) c FROM incidents WHERE source=?", (source,))["c"]
        if n >= 3:  # repeat failure: start problem management instead of patching again
            pid = self.create_problem("Recurring incidents: %s" % source, source)
            self.db.x("UPDATE incidents SET problem_id=? WHERE source=? AND problem_id IS NULL",
                      (pid, source))
            self._log(iid, "Recurring failure detected: problem #%d opened for root cause analysis" % pid)

    def acknowledge(self, iid, by="agent"):
        inc = self._get(iid)
        if inc["status"] != "new":
            raise ValueError("incident is not new")
        self.db.x("UPDATE incidents SET status='acknowledged', acknowledged_at=? WHERE id=?",
                  (time.time(), iid))
        self._log(iid, "Acknowledged by %s" % by)

    def resolve(self, iid, resolution, by="agent"):
        inc = self._get(iid)
        if inc["status"] not in OPEN:
            raise ValueError("incident already resolved")
        if not resolution:
            raise ValueError("a resolution note is required")
        now = time.time()
        self.db.x("UPDATE incidents SET status='resolved', resolved_at=?, resolution=? WHERE id=?",
                  (now, resolution, iid))
        self._log(iid, "Resolved by %s (%s SLA): %s"
                  % (by, "within" if now <= inc["sla_due"] else "BREACHED", resolution))

    def auto_resolve(self, source, note):
        for r in self.db.q("SELECT id FROM incidents WHERE source=? AND auto=1 AND status IN ('new','acknowledged')",
                           (source,)):
            self.resolve(r["id"], note, by="monitoring")

    def escalate(self, iid, reason="manual escalation"):
        inc = self._get(iid)
        if inc["status"] not in OPEN:
            raise ValueError("incident is not open")
        idx = LEVELS.index(inc["level"])
        if idx == len(LEVELS) - 1:
            raise ValueError("already at L3")
        self.db.x("UPDATE incidents SET level=? WHERE id=?", (LEVELS[idx + 1], iid))
        self._log(iid, "Escalated %s -> %s (%s)" % (LEVELS[idx], LEVELS[idx + 1], reason))

    def escalation_sweep(self):
        """Escalation matrix with timers: L1->L2 at 50% of SLA elapsed, L2->L3 at 80%."""
        now, moved = time.time(), 0
        for r in self.db.q("SELECT id,level,created_at,sla_due FROM incidents WHERE status IN ('new','acknowledged')"):
            span = max(r["sla_due"] - r["created_at"], 1e-6)
            frac = (now - r["created_at"]) / span
            if r["level"] == "L1" and frac >= config.ESCALATE_L2_AT:
                self.escalate(r["id"], "%d%% of SLA elapsed" % (frac * 100))
                moved += 1
            elif r["level"] == "L2" and frac >= config.ESCALATE_L3_AT:
                self.escalate(r["id"], "%d%% of SLA elapsed" % (frac * 100))
                moved += 1
        return moved

    def _get(self, iid):
        inc = self.db.one("SELECT * FROM incidents WHERE id=?", (iid,))
        if inc is None:
            raise LookupError("incident %s not found" % iid)
        return inc

    def _decorate(self, r):
        now = time.time()
        if r["status"] == "resolved":
            r["breached"] = r["resolved_at"] > r["sla_due"]
            r["sla_remaining_s"] = None
        else:
            r["breached"] = now > r["sla_due"]
            r["sla_remaining_s"] = r["sla_due"] - now
        return r

    def list_incidents(self, status=None, limit=100):
        if status == "open":
            rows = self.db.q("SELECT * FROM incidents WHERE status IN ('new','acknowledged') "
                             "ORDER BY priority, created_at DESC LIMIT ?", (limit,))
        else:
            rows = self.db.q("SELECT * FROM incidents ORDER BY id DESC LIMIT ?", (limit,))
        return [self._decorate(r) for r in rows]

    def get_incident(self, iid):
        inc = self._decorate(self._get(iid))
        inc["log"] = self.db.q("SELECT ts,note FROM incident_log WHERE incident_id=? ORDER BY id", (iid,))
        return inc

    def sla_compliance(self):
        now, total, ok = time.time(), 0, 0
        for r in self.db.q("SELECT status,resolved_at,sla_due FROM incidents"):
            if r["status"] == "resolved":
                total += 1
                ok += r["resolved_at"] <= r["sla_due"]
            elif now > r["sla_due"]:
                total += 1
        return 100.0 * ok / total if total else None

    # ---- problems -----------------------------------------------------------------
    def create_problem(self, title, source="manual", root_cause="", workaround=""):
        if not title:
            raise ValueError("title is required")
        return self.db.x("INSERT INTO problems(title,source,status,root_cause,workaround,created_at) "
                         "VALUES(?,?,?,?,?,?)", (title, source, "new", root_cause, workaround, time.time()))

    def update_problem(self, pid, status=None, root_cause=None, workaround=None):
        p = self.db.one("SELECT * FROM problems WHERE id=?", (pid,))
        if p is None:
            raise LookupError("problem %s not found" % pid)
        status = status or p["status"]
        if status not in PROBLEM_STATES:
            raise ValueError("status must be one of %s" % ", ".join(PROBLEM_STATES))
        root_cause = p["root_cause"] if root_cause is None else root_cause
        workaround = p["workaround"] if workaround is None else workaround
        if status in ("known_error", "resolved") and not root_cause:
            raise ValueError("a root cause is required for known_error / resolved")
        resolved_at = time.time() if status == "resolved" else None
        self.db.x("UPDATE problems SET status=?, root_cause=?, workaround=?, resolved_at=? WHERE id=?",
                  (status, root_cause, workaround, resolved_at, pid))
        if status == "resolved" and p["status"] != "resolved":   # feed the knowledge base ("Improve")
            self.kb.add("Known error: " + p["title"], "Root cause: %s\nWorkaround: %s" % (root_cause, workaround or "n/a"),
                        p["source"])

    def list_problems(self):
        return self.db.q("SELECT p.*, (SELECT COUNT(*) FROM incidents i WHERE i.problem_id=p.id) AS incidents "
                         "FROM problems p ORDER BY id DESC")

    def get_problem(self, pid):
        p = self.db.one("SELECT * FROM problems WHERE id=?", (pid,))
        if p is None:
            raise LookupError("problem %s not found" % pid)
        p["incidents"] = self.db.q("SELECT id,title,priority,status FROM incidents WHERE problem_id=?", (pid,))
        return p

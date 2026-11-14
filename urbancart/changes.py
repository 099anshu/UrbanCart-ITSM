"""Change enablement: approval, blackout windows, canary rollout with automatic rollback."""
import time

STAGES = [5, 25, 50, 100]
RISKS = ("low", "medium", "high")
TYPES = ("standard", "normal", "emergency")


class ChangeMgmt:
    def __init__(self, db, itsm, health_gate, sale_active):
        self.db, self.itsm = db, itsm
        self.health_gate, self.sale_active = health_gate, sale_active

    def _log(self, cid, note):
        self.db.x("INSERT INTO change_log(change_id,ts,note) VALUES(?,?,?)", (cid, time.time(), note))

    def _set(self, cid, note, **fields):
        sets = ", ".join("%s=?" % k for k in fields)
        self.db.x("UPDATE changes SET %s, updated_at=? WHERE id=?" % sets, (*fields.values(), time.time(), cid))
        self._log(cid, note)

    def _get(self, cid):
        c = self.db.one("SELECT * FROM changes WHERE id=?", (cid,))
        if c is None:
            raise LookupError("change %s not found" % cid)
        return c

    def create(self, title, description="", risk="medium", ctype="normal", version="", requester="unknown"):
        if not title:
            raise ValueError("title is required")
        if risk not in RISKS or ctype not in TYPES:
            raise ValueError("risk must be %s; type must be %s" % ("/".join(RISKS), "/".join(TYPES)))
        now = time.time()
        cid = self.db.x("INSERT INTO changes(title,description,risk,type,version,status,rollout_pct,requester,"
                        "created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                        (title, description, risk, ctype, version, "draft", 0, requester, now, now))
        self._log(cid, "Change requested by %s (%s, %s risk)" % (requester, ctype, risk))
        return cid

    def submit(self, cid):
        c = self._get(cid)
        if c["status"] != "draft":
            raise ValueError("only draft changes can be submitted")
        if c["type"] == "standard" and c["risk"] == "low":
            self._set(cid, "Pre-approved standard change", status="approved", approver="pre-approved")
        else:
            self._set(cid, "Submitted to change board", status="pending_approval")

    def approve(self, cid, approver):
        c = self._get(cid)
        if c["status"] != "pending_approval":
            raise ValueError("change is not pending approval")
        if not approver or approver == c["requester"]:
            raise PermissionError("the approver must be someone other than the requester (four-eyes principle)")
        self._set(cid, "Approved by %s" % approver, status="approved", approver=approver)

    def reject(self, cid, approver, reason=""):
        c = self._get(cid)
        if c["status"] != "pending_approval":
            raise ValueError("change is not pending approval")
        self._set(cid, "Rejected by %s: %s" % (approver, reason or "no reason given"), status="rejected",
                  approver=approver)

    def start(self, cid):
        c = self._get(cid)
        if c["status"] != "approved":
            raise ValueError("production releases need an approved change (status is %s)" % c["status"])
        if self.sale_active() and c["type"] != "emergency":
            raise ValueError("blackout: no non-emergency releases while a sale is running")
        self._set(cid, "Canary rollout started at %d%%" % STAGES[0], status="rolling_out",
                  rollout_pct=STAGES[0])

    def advance(self, cid):
        c = self._get(cid)
        if c["status"] != "rolling_out":
            raise ValueError("change is not rolling out")
        ok, msg = self.health_gate()
        if not ok:
            self.rollback(cid, "health gate failed at %d%%: %s" % (c["rollout_pct"], msg), auto=True)
            return self._get(cid)
        nxt = next(s for s in STAGES if s > c["rollout_pct"])
        if nxt == 100:
            self._set(cid, "Rollout complete (health gate passed)", status="completed", rollout_pct=100)
            if c["version"]:
                self.db.kv_set("app_version", c["version"])
        else:
            self._set(cid, "Health gate passed, widened to %d%%" % nxt, rollout_pct=nxt)
        return self._get(cid)

    def rollback(self, cid, reason="manual rollback", auto=False):
        c = self._get(cid)
        if c["status"] != "rolling_out":
            raise ValueError("only a rolling-out change can be rolled back")
        self._set(cid, "ROLLED BACK%s: %s" % (" automatically" if auto else "", reason),
                  status="rolled_back", rollout_pct=0)
        if auto:
            self.itsm.create_incident("Change #%d auto-rolled back: %s" % (cid, c["title"]), reason,
                                      "P2", source="change_rollback", auto=True)

    def failure_rate(self):
        r = self.db.one("SELECT SUM(status='rolled_back') bad, SUM(status IN ('completed','rolled_back')) done "
                        "FROM changes")
        return 100.0 * (r["bad"] or 0) / r["done"] if r["done"] else None

    def list(self):
        return self.db.q("SELECT * FROM changes ORDER BY id DESC")

    def get(self, cid):
        c = self._get(cid)
        c["log"] = self.db.q("SELECT ts,note FROM change_log WHERE change_id=? ORDER BY id", (cid,))
        return c

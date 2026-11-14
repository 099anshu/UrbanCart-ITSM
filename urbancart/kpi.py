"""KPI engine: computes every KPI from the deck's Performance Measurement slide."""
from . import config


def compute(app):
    db, out = app.db, []

    def add(key, name, value, unit, target, op, basis):
        if value is None:
            status = "no data"
        elif op == ">=":
            status = "met" if value >= target else "not met"
        elif op == "<":
            status = "met" if value < target else "not met"
        else:
            status = "info"
        out.append({"key": key, "name": name, "value": None if value is None else round(value, 2),
                    "unit": unit, "target": target, "op": op, "status": status, "basis": basis})

    av = db.one("SELECT COUNT(*) n, SUM(ok) ok FROM availability")
    uptime = 100.0 * av["ok"] / av["n"] if av["n"] else None
    add("uptime", "Website uptime", uptime, "%", 99.9, ">=", "Case")

    a = db.one("SELECT SUM(outcome='ok') ok, SUM(outcome='error') err FROM attempts WHERE sale=1")
    if not ((a["ok"] or 0) + (a["err"] or 0)):
        a = db.one("SELECT SUM(outcome='ok') ok, SUM(outcome='error') err FROM attempts")
    n = (a["ok"] or 0) + (a["err"] or 0)
    add("checkout", "Checkout success (sale traffic)", 100.0 * a["ok"] / n if n else None, "%", 98, ">=", "Case")

    best = float(db.kv_get("loadtest_best", 0) or 0)
    add("scalability", "Load handled without degradation", best if best else None, "x normal", 5, ">=", "Proposed")
    add("sla", "Incidents resolved within SLA", app.itsm.sla_compliance(), "%", 94, ">=", "Case")

    d = db.one("SELECT AVG(detection_s) d FROM alerts WHERE detection_s IS NOT NULL")["d"]
    add("mttd", "API failure detection time (MTTD)", d, "s", 300, "<", "Case / Proposed")
    add("cfr", "Change failure rate", app.changes.failure_rate(), "%", 15, "<", "Proposed")

    checked, mism = int(db.kv_get("inv_checked", 0)), int(db.kv_get("inv_mismatched", 0))
    red = (1 - (mism / checked) / config.BASELINE_MISMATCH_RATE) * 100 if checked else None
    add("inventory", "Inventory mismatch reduction", red, "%", 70, ">=", "Case (baseline assumed)")

    ids = [r["id"] for r in db.q("SELECT id FROM orders WHERE customer NOT LIKE 'loadtest-%' "
                                  "ORDER BY created_at DESC LIMIT 30")]
    if ids:
        same = 0
        for oid in ids:
            views = [app.orders.support_answer(c, oid) for c in ("chat", "email", "call")]
            same += len({(v["status"], v["tracking"], v["last_update"]) for v in views}) == 1
        tracking = 100.0 * same / len(ids)
    else:
        tracking = None
    add("tracking", "Order status consistent across channels", tracking, "%", 85, ">=", "Case")

    add("csat", "Customer satisfaction (CSAT)", db.one("SELECT AVG(rating) r FROM csat")["r"], "/5", 4.6, ">=", "Case")

    c = db.one("SELECT COUNT(DISTINCT customer) n, COUNT(*) o FROM orders "
               "WHERE customer NOT LIKE 'loadtest-%' AND status IN ('CONFIRMED','SHIPPED','DELIVERED')")
    rep = db.one("SELECT COUNT(*) n FROM (SELECT customer FROM orders WHERE customer NOT LIKE 'loadtest-%' "
                 "AND status IN ('CONFIRMED','SHIPPED','DELIVERED') GROUP BY customer HAVING COUNT(*)>1)")["n"]
    add("repeat", "Repeat purchase rate", 100.0 * rep / c["n"] if c["n"] else None, "%", "+25% vs baseline", "info", "Case")

    by = {k["key"]: k["value"] for k in out}
    milestones = [
        {"phase": 1, "text": "Outages detected in minutes", "done": by["mttd"] is not None and by["mttd"] < 300},
        {"phase": 2, "text": "Stable at 3x+ peak traffic", "done": best >= 3},
        {"phase": 3, "text": "99.9% uptime and 94% SLA",
         "done": (by["uptime"] or 0) >= 99.9 and (by["sla"] or 0) >= 94},
    ]
    return {"kpis": out, "milestones": milestones}

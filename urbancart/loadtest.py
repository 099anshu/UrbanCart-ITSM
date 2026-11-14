"""Flash-sale load test: N shoppers hit checkout at the same instant (Phase 1: load-test before every sale)."""
import json
import threading
import time

from . import config
from .checkout import TERMINAL_OK


def run(app, users):
    users = max(1, min(int(users), 500))
    was_sale = app.sale_active()
    app.set_sale(True)
    app.db.kv_set("loadtest_running", "1")
    results, lock = [], threading.Lock()
    barrier = threading.Barrier(users)

    def shopper(i):
        barrier.wait()
        t0 = time.time()
        oid = app.checkout.submit("loadtest-%d" % i, "LOAD-001", 1, track=True)
        app.checkout.wait(oid, 30)
        status = app.orders.row(oid)["status"]
        with lock:
            results.append((status, time.time() - t0))

    try:
        threads = [threading.Thread(target=shopper, args=(i,)) for i in range(users)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
    finally:
        app.set_sale(was_sale)
        app.db.kv_set("loadtest_running", "0")

    ok = sum(1 for s, _ in results if s in TERMINAL_OK)
    success = 100.0 * ok / len(results)
    lat = sorted(t for _, t in results)
    res = {"users": users, "multiplier": round(users / config.BASELINE_USERS, 1), "success_pct": round(success, 1),
           "p95_ms": round(lat[int(0.95 * (len(lat) - 1))] * 1000), "passed": success >= 98.0,
           "mode": app.checkout.stats()["mode"], "workers_peak": app.checkout.active, "ts": time.time()}
    app.db.kv_set("loadtest", json.dumps(res))
    if res["passed"] and res["multiplier"] > float(app.db.kv_get("loadtest_best", 0) or 0):
        app.db.kv_set("loadtest_best", res["multiplier"])
    return res

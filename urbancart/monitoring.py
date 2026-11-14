"""Proactive monitoring: probes APIs and checkout health, raises incidents automatically."""
import threading
import time

from . import config

TITLES = {"payment_api": "Payment gateway API", "logistics_api": "Logistics API",
          "checkout_errors": "Checkout error rate", "checkout_queue": "Checkout queue depth"}


class Monitor:
    def __init__(self, db, itsm, payment, backup, logistics, checkout):
        self.db, self.itsm, self.checkout = db, itsm, checkout
        self.payment, self.backup, self.logistics = payment, backup, logistics
        self.fails, self.oks = {}, {}
        self.ticks = 0
        self.running = False
        self.last = {}
        self._stop = threading.Event()

    def start(self):
        self.running = True
        threading.Thread(target=self._loop, daemon=True).start()

    def stop(self):
        self._stop.set()
        self.running = False

    def _loop(self):
        while not self._stop.wait(config.MONITOR_INTERVAL):
            try:
                self.tick()
            except Exception as e:  # monitoring must never die silently
                print("monitor error:", e)

    @staticmethod
    def _probe(svc):
        h = svc.health()
        if not h["ok"]:
            return False, "health check failed"
        if h["latency_ms"] > config.LATENCY_ALERT_MS:
            return False, "latency %d ms above %d ms" % (h["latency_ms"], config.LATENCY_ALERT_MS)
        return True, "healthy (%d ms)" % h["latency_ms"]

    def tick(self):
        pay_ok, pay_msg = self._probe(self.payment)
        bak_ok, _ = self._probe(self.backup)
        log_ok, log_msg = self._probe(self.logistics)
        rate, n = self.checkout.recent_error_rate(30)
        depth = self.checkout.q.qsize()
        err_ok = not (n >= 10 and rate >= config.ERROR_RATE_ALERT)
        results = {
            "payment_api": (pay_ok, pay_msg, "P2" if bak_ok else "P1"),   # backup up = degraded, not down
            "logistics_api": (log_ok, log_msg, "P2"),
            "checkout_errors": (err_ok, "error rate %.0f%% over %d attempts" % (rate * 100, n), "P1"),
            "checkout_queue": (depth < config.QUEUE_ALERT_DEPTH, "queue depth %d" % depth, "P3"),
        }
        for name, (ok, msg, prio) in results.items():
            self._handle(name, ok, msg, prio)
        self.db.x("INSERT INTO availability(ts,ok) VALUES(?,?)", (time.time(), 1 if ((pay_ok or bak_ok) and err_ok) else 0))
        self.checkout.retry_shipments()
        self.itsm.escalation_sweep()
        self.ticks += 1
        self.last = {k: {"ok": v[0], "msg": v[1]} for k, v in results.items()}
        return self.last

    def _handle(self, name, ok, msg, prio):
        if ok:
            self.oks[name] = self.oks.get(name, 0) + 1
            self.fails[name] = 0
            if self.oks[name] >= 2:
                self.itsm.auto_resolve(name, "Auto-recovered: %s" % msg)
            return
        self.fails[name] = self.fails.get(name, 0) + 1
        self.oks[name] = 0
        if self.fails[name] < config.CONSECUTIVE_FAILS:
            return
        iid, created = self.itsm.raise_or_update(name, "[AUTO] %s failing" % TITLES[name], msg, prio)
        if created:
            svc = {"payment_api": self.payment, "logistics_api": self.logistics}.get(name)
            det = time.time() - svc.fault_since if svc is not None and svc.fault_since else None
            self.db.x("INSERT INTO alerts(ts,check_name,severity,message,incident_id,detection_s) "
                      "VALUES(?,?,?,?,?,?)", (time.time(), name, prio, msg, iid, det))

    def alerts(self, limit=30):
        return self.db.q("SELECT * FROM alerts ORDER BY id DESC LIMIT ?", (limit,))

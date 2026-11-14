"""Queued, auto-scaling checkout with payment fallback (circuit breaker).

Modern mode  : orders go into a queue; a worker pool scales with queue depth, so a flash
               sale never pushes more concurrent calls at the payment gateway than it can take.
Legacy mode  : the "before" state from the case: every request calls the gateway directly,
               no queue, no fallback gateway. Above ~3x normal load the gateway falls over.
"""
import collections
import queue
import threading
import time

from .sim import ServiceDown

TERMINAL_OK = ("CONFIRMED", "SHIPPED", "DELIVERED")


class Checkout:
    def __init__(self, db, inventory, orders, payment, backup, logistics,
                 legacy=False, min_workers=2, max_workers=32):
        self.db, self.inv, self.orders = db, inventory, orders
        self.payment, self.backup, self.logistics = payment, backup, logistics
        self.legacy, self.min_workers, self.max_workers = legacy, min_workers, max_workers
        self.q = queue.Queue()
        self.active = 0
        self.target = min_workers
        self.sale = False
        self.events = {}
        self.scale_events = collections.deque(maxlen=10)
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._breaker_fail = 0
        self._breaker_until = 0.0

    # ---- lifecycle -------------------------------------------------------------------
    def start(self):
        if self.legacy:
            return
        self._resize(self.min_workers, "startup")
        threading.Thread(target=self._autoscale, daemon=True).start()

    def stop(self):
        self._stop.set()

    def _resize(self, n, why):
        with self._lock:
            old = self.target
            self.target = n
            while self.active < n:
                self.active += 1
                threading.Thread(target=self._worker, daemon=True).start()
        if n != old:
            self.scale_events.append({"ts": time.time(), "workers": n, "why": why})

    def _worker(self):
        while not self._stop.is_set():
            with self._lock:
                if self.active > self.target:
                    self.active -= 1
                    return
            try:
                oid, t0 = self.q.get(timeout=0.3)
            except queue.Empty:
                continue
            self._run(oid, t0)

    def _autoscale(self):
        idle = 0
        while not self._stop.wait(0.5):
            depth = self.q.qsize()
            if depth > 5 * self.active and self.active < self.max_workers:
                self._resize(min(self.max_workers, max(self.active + 2, self.active * 2)),
                             "queue depth %d" % depth)
                idle = 0
            elif depth == 0 and self.active > self.min_workers:
                idle += 1
                if idle >= 10:
                    self._resize(self.active - 1, "idle")
                    idle = 0
            else:
                idle = 0

    # ---- public API ------------------------------------------------------------------
    def submit(self, customer, sku, qty=1, track=False):
        prod = self.db.one("SELECT price FROM products WHERE sku=?", (sku,))
        if prod is None:
            raise LookupError("unknown product %s" % sku)
        qty = int(qty)
        if qty < 1 or qty > 10:
            raise ValueError("quantity must be 1-10")
        oid = self.orders.create(customer or "guest", sku, qty, prod["price"] * qty)
        if track:
            self.events[oid] = threading.Event()
        t0 = time.time()
        if self.legacy:
            self._run(oid, t0)
        else:
            self.q.put((oid, t0))
        return oid

    def wait(self, oid, timeout=30):
        ev = self.events.get(oid)
        if ev:
            ev.wait(timeout)

    def stats(self):
        return {"mode": "legacy" if self.legacy else "scalable", "workers": self.active,
                "queue": self.q.qsize(), "breaker_open": time.time() < self._breaker_until,
                "scale_events": list(self.scale_events)}

    def recent_error_rate(self, window=30):
        r = self.db.one("SELECT SUM(outcome='ok') ok, SUM(outcome='error') err FROM attempts WHERE ts>?",
                        (time.time() - window,))
        ok, err = r["ok"] or 0, r["err"] or 0
        n = ok + err
        return (err / n if n else 0.0), n

    # ---- processing ------------------------------------------------------------------
    def _run(self, oid, t0):
        try:
            self._process(oid, t0)
        except Exception as e:  # never lose an order silently
            self.orders.set_status(oid, "FAILED", "Unexpected error: %s" % e)
            self._record("error", t0)
        finally:
            ev = self.events.pop(oid, None)
            if ev:
                ev.set()

    def _process(self, oid, t0):
        o = self.orders.row(oid)
        self.orders.set_status(oid, "PROCESSING", "Order picked up for processing")
        hub = self.inv.reserve(o["sku"], o["qty"])
        if hub is None:
            self.orders.set_status(oid, "OUT_OF_STOCK",
                                   "Item not available at any active hub. You were not charged.")
            return self._record("out_of_stock", t0)
        self.orders.set_status(oid, "PROCESSING", "Stock reserved at %s hub" % hub, hub=hub)
        try:
            ref, gw = self._pay(oid, o["amount"])
        except ServiceDown as e:
            self.inv.release(o["sku"], hub, o["qty"])
            self.orders.set_status(oid, "FAILED", "Payment failed (%s). You were not charged." % e)
            return self._record("error", t0)
        self.inv.commit(o["sku"], hub, o["qty"])
        self.orders.set_status(oid, "CONFIRMED", "Payment confirmed via %s gateway (%s)" % (gw, ref),
                               gateway=gw)
        self._record("ok", t0)
        self._ship(oid)

    def _pay(self, oid, amount):
        if self.legacy:
            return self.payment.charge(oid, amount), "primary"
        gateways = [("primary", self.payment), ("backup", self.backup)]
        if time.time() < self._breaker_until:
            gateways = gateways[1:]                    # circuit open: skip the failing gateway
        last = None
        for name, gw in gateways:
            try:
                ref = gw.charge(oid, amount)
                if name == "primary":
                    self._breaker_fail = 0
                return ref, name
            except ServiceDown as e:
                last = e
                if name == "primary":
                    with self._lock:
                        self._breaker_fail += 1
                        if self._breaker_fail >= 5:
                            self._breaker_until = time.time() + 15
                            self._breaker_fail = 0
        raise last

    def _ship(self, oid, first=True):
        try:
            trk = self.logistics.create_shipment(oid)
        except ServiceDown:
            if first:
                self.orders.add_note(oid, "Shipment booking pending: logistics partner unavailable. "
                                          "Retrying automatically.")
            return False
        self.orders.set_status(oid, "SHIPPED", "Shipped from hub. Tracking %s" % trk, tracking=trk)
        return True

    def retry_shipments(self):
        rows = self.db.q("SELECT id FROM orders WHERE status='CONFIRMED' AND updated_at<? LIMIT 50",
                         (time.time() - 3,))
        for r in rows:
            self._ship(r["id"], first=False)

    def _record(self, outcome, t0):
        self.db.x("INSERT INTO attempts(ts,outcome,latency_ms,sale) VALUES(?,?,?,?)",
                  (time.time(), outcome, (time.time() - t0) * 1000, 1 if self.sale else 0))

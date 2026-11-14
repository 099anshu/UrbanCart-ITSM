"""Wires every component together (the 'platform')."""
import threading
import time

from . import config, kpi, loadtest
from .changes import ChangeMgmt
from .checkout import Checkout
from .db import DB
from .inventory import Inventory
from .itsm import ITSM, KB
from .monitoring import Monitor
from .orders import OrderHub
from .sim import LogisticsAPI, PaymentGateway

PRODUCTS = [
    ("FSH-001", "Linen Kurta", "fashion", 1299), ("FSH-002", "Denim Jacket", "fashion", 2499),
    ("ELC-001", "Wireless Earbuds", "electronics", 1999), ("ELC-002", "Smart Watch", "electronics", 4499),
    ("HOM-001", "Ceramic Dinner Set", "home", 3299), ("HOM-002", "Cotton Bedsheet", "home", 899),
    ("GRO-001", "Cold-pressed Olive Oil 1L", "groceries", 749), ("GRO-002", "Filter Coffee 500g", "groceries", 399),
    ("LOAD-001", "Load-test item", "loadtest", 100),
]
ACTIVE_HUBS = ["Mumbai", "Delhi", "Bengaluru"]
PLANNED_HUBS = ["Pune", "Hyderabad", "Chennai", "Kolkata", "Ahmedabad"]   # the 5 new city hubs
KB_SEED = [
    ("Payment gateway failing or timing out", "1. Check the gateway status page.\n2. Confirm checkout is using the "
     "backup gateway (circuit breaker).\n3. If both fail, post a customer status update and escalate to L3.",
     "payment_api,checkout_errors"),
    ("Logistics API unavailable", "Orders stay CONFIRMED and shipments are retried automatically every monitor "
     "cycle. Tell customers shipping is delayed, not cancelled.", "logistics_api"),
    ("Checkout queue growing", "Check the worker count on the dashboard. The autoscaler adds workers as depth "
     "grows. If depth keeps rising, run a load test and review gateway capacity.", "checkout_queue"),
    ("Item shows in stock but is unavailable", "Run a hub scan (Hubs tab). Mismatches are corrected "
     "automatically and logged as incidents.", "inventory_mismatch"),
    ("Where is my order? (all channels)", "Use the order ID in the support lookup. Chat, email and call all show "
     "the same status from the order hub. Never quote status from memory.", "support"),
    ("Change rolled back", "A canary health gate failed. Review the change log, fix, and resubmit through "
     "the change board.", "change_rollback"),
]


class App:
    def __init__(self, db_path=None, legacy=None, min_workers=None):
        self.db = DB(db_path or config.DB_PATH)
        legacy = config.LEGACY_MODE if legacy is None else legacy
        self.payment = PaymentGateway("payment gateway", config.GATEWAY_MAX_CONCURRENT)
        self.backup = PaymentGateway("backup gateway", config.GATEWAY_MAX_CONCURRENT)
        self.logistics = LogisticsAPI("logistics API", config.GATEWAY_MAX_CONCURRENT)
        self.kb = KB(self.db)
        self.itsm = ITSM(self.db, self.kb)
        self.inventory = Inventory(self.db, on_mismatch=self._on_mismatch)
        self.orders = OrderHub(self.db)
        self.checkout = Checkout(self.db, self.inventory, self.orders, self.payment, self.backup, self.logistics,
                                 legacy=legacy, min_workers=min_workers or config.MIN_WORKERS,
                                 max_workers=config.MAX_WORKERS)
        self.monitor = Monitor(self.db, self.itsm, self.payment, self.backup, self.logistics, self.checkout)
        self.changes = ChangeMgmt(self.db, self.itsm, self.health_gate, self.sale_active)
        self._seed()
        self.checkout.sale = self.sale_active()

    def _seed(self):
        if self.db.one("SELECT 1 FROM products LIMIT 1"):
            return
        now = time.time()
        for h in ACTIVE_HUBS:
            self.db.x("INSERT INTO hubs VALUES(?,?,?)", (h, "active", now))
        for h in PLANNED_HUBS:
            self.db.x("INSERT INTO hubs VALUES(?,?,?)", (h, "planned", now))
        for sku, name, cat, price in PRODUCTS:
            self.db.x("INSERT INTO products VALUES(?,?,?,?)", (sku, name, cat, price))
            for h in ACTIVE_HUBS:
                self.db.x("INSERT INTO inventory(sku,hub,on_hand,reserved) VALUES(?,?,?,0)",
                          (sku, h, 1_000_000 if cat == "loadtest" else 200))
        for title, body, tags in KB_SEED:
            self.kb.add(title, body, tags)

    # ---- lifecycle ----
    def start(self):
        self.checkout.start()
        self.monitor.start()

    def stop(self):
        self.checkout.stop()
        self.monitor.stop()

    # ---- helpers ----
    def sale_active(self):
        return self.db.kv_get("sale", "0") == "1"

    def set_sale(self, on):
        self.db.kv_set("sale", "1" if on else "0")
        self.checkout.sale = bool(on)

    def health_gate(self):
        rate, n = self.checkout.recent_error_rate(30)
        if n >= 5 and rate >= 0.05:
            return False, "checkout error rate %.0f%%" % (rate * 100)
        p1 = self.db.one("SELECT COUNT(*) c FROM incidents WHERE priority='P1' AND status IN ('new','acknowledged')")["c"]
        if p1:
            return False, "%d open P1 incident(s)" % p1
        return True, "healthy"

    def _on_mismatch(self, m):
        self.itsm.raise_or_update(
            "inventory_mismatch", "[AUTO] Inventory mismatch at %s hub" % m["hub"],
            "%s: system said %d, physical count %d. Corrected from hub scan." % (m["sku"], m["system_qty"], m["physical_qty"]),
            "P3")

    def set_fault(self, service, mode):
        svc = {"payment": self.payment, "backup": self.backup, "logistics": self.logistics}.get(service)
        if svc is None:
            raise ValueError("service must be payment, backup or logistics")
        svc.set_mode(mode)

    def hubs(self):
        return self.db.q("SELECT * FROM hubs ORDER BY status, name")

    def onboard_hub(self, name):
        hub = self.db.one("SELECT status FROM hubs WHERE name=?", (name,))
        if hub is None:
            raise LookupError("hub %s not found" % name)
        if hub["status"] == "active":
            raise ValueError("hub already active")
        problems = []
        if self.monitor.ticks == 0:
            problems.append("Phase 1: monitoring has not run yet")
        if float(self.db.kv_get("loadtest_best", 0) or 0) < 3:
            problems.append("Phase 2: no passing load test at 3x or more of normal load")
        if self.db.one("SELECT COUNT(*) c FROM incidents WHERE priority='P1' AND status IN ('new','acknowledged')")["c"]:
            problems.append("an open P1 incident must be resolved first")
        if problems:
            raise ValueError("hub not ready: " + "; ".join(problems))
        self.db.x("UPDATE hubs SET status='active' WHERE name=?", (name,))

    def health(self):
        return {"checks": self.monitor.last, "checkout": self.checkout.stats(), "sale": self.sale_active(),
                "version": self.db.kv_get("app_version", "1.0.0"), "monitor_ticks": self.monitor.ticks,
                "services": {"payment": self.payment.mode, "backup": self.backup.mode,
                             "logistics": self.logistics.mode}}

    def kpis(self):
        return kpi.compute(self)

    def start_loadtest(self, users):
        if self.db.kv_get("loadtest_running", "0") == "1":
            raise ValueError("a load test is already running")
        self.db.kv_set("loadtest_running", "1")
        threading.Thread(target=loadtest.run, args=(self, users), daemon=True).start()

    def loadtest_status(self):
        return {"running": self.db.kv_get("loadtest_running", "0") == "1",
                "last": self.db.kv_json("loadtest"), "best_multiplier": float(self.db.kv_get("loadtest_best", 0) or 0)}

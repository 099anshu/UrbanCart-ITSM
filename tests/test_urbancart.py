import threading
import time
import unittest

from urbancart import config, loadtest
from urbancart.app import App

DONE = ("CONFIRMED", "SHIPPED", "DELIVERED", "FAILED", "OUT_OF_STOCK")


def order(app, sku="FSH-001", customer="tester", qty=1):
    oid = app.checkout.submit(customer, sku, qty, track=True)
    app.checkout.wait(oid, 15)
    return oid


class Base(unittest.TestCase):
    def setUp(self):
        self.app = App(":memory:", min_workers=8)
        self.app.checkout.start()

    def tearDown(self):
        self.app.stop()


class TestCheckoutAndInventory(Base):
    def test_happy_path_ships_and_decrements_stock(self):
        oid = order(self.app)
        self.assertEqual(self.app.orders.view(oid)["status"], "SHIPPED")
        total = self.app.db.one("SELECT SUM(on_hand) s FROM inventory WHERE sku='FSH-001'")["s"]
        self.assertEqual(total, 599)

    def test_no_overselling_under_concurrency(self):
        self.app.db.x("INSERT INTO products VALUES('TEST-1','Rare item','home',10)")
        self.app.db.x("INSERT INTO inventory VALUES('TEST-1','Mumbai',5,0)")
        ids, lock = [], threading.Lock()

        def buy():
            oid = order(self.app, "TEST-1")
            with lock:
                ids.append(oid)
        ts = [threading.Thread(target=buy) for _ in range(20)]
        [t.start() for t in ts]
        [t.join() for t in ts]
        st = [self.app.orders.row(i)["status"] for i in ids]
        self.assertEqual(sum(s in ("CONFIRMED", "SHIPPED") for s in st), 5)
        self.assertEqual(st.count("OUT_OF_STOCK"), 15)
        row = self.app.db.one("SELECT on_hand, reserved FROM inventory WHERE sku='TEST-1'")
        self.assertEqual((row["on_hand"], row["reserved"]), (0, 0))

    def test_payment_fallback_to_backup_gateway(self):
        self.app.set_fault("payment", "down")
        oid = order(self.app)
        o = self.app.orders.row(oid)
        self.assertEqual((o["status"], o["gateway"]), ("SHIPPED", "backup"))

    def test_total_payment_failure_releases_stock(self):
        self.app.set_fault("payment", "down")
        self.app.set_fault("backup", "down")
        oid = order(self.app)
        self.assertEqual(self.app.orders.row(oid)["status"], "FAILED")
        self.assertEqual(self.app.db.one("SELECT SUM(reserved) s FROM inventory WHERE sku='FSH-001'")["s"], 0)

    def test_logistics_outage_keeps_order_then_retries(self):
        self.app.set_fault("logistics", "down")
        oid = order(self.app)
        self.assertEqual(self.app.orders.row(oid)["status"], "CONFIRMED")
        self.app.set_fault("logistics", "ok")
        self.app.db.x("UPDATE orders SET updated_at=updated_at-10")
        self.app.checkout.retry_shipments()
        self.assertEqual(self.app.orders.row(oid)["status"], "SHIPPED")

    def test_all_channels_give_identical_answers(self):
        oid = order(self.app)
        views = [self.app.orders.support_answer(c, oid) for c in ("chat", "email", "call")]
        for v in views:
            v.pop("channel")
        self.assertTrue(views[0] == views[1] == views[2])
        k = {x["key"]: x for x in self.app.kpis()["kpis"]}
        self.assertEqual(k["tracking"]["value"], 100.0)

    def test_hub_scan_corrects_and_raises_incident(self):
        res = self.app.inventory.hub_sync("Mumbai", {"FSH-001": 190, "FSH-002": 200})
        self.assertEqual(len(res), 1)
        self.assertEqual(self.app.db.one("SELECT on_hand FROM inventory WHERE sku='FSH-001' AND hub='Mumbai'")["on_hand"], 190)
        inc = self.app.itsm.list_incidents("open")
        self.assertEqual(inc[0]["source"], "inventory_mismatch")


class TestMonitoringAndIncidents(Base):
    def test_outage_detected_deduplicated_and_auto_resolved(self):
        self.app.set_fault("payment", "down")
        m = self.app.monitor
        m.tick()
        self.assertEqual(self.app.itsm.list_incidents("open"), [])      # one blip is not an alert
        m.tick()
        m.tick()
        inc = self.app.itsm.list_incidents("open")
        self.assertEqual(len(inc), 1)                                    # deduplicated
        self.assertEqual(inc[0]["priority"], "P2")                       # backup is healthy: degraded
        self.assertIsNotNone(self.app.monitor.alerts()[0]["detection_s"])
        self.app.set_fault("payment", "ok")
        m.tick()
        m.tick()
        self.assertEqual(self.app.itsm.list_incidents("open"), [])
        self.assertEqual(self.app.itsm.list_incidents()[0]["status"], "resolved")

    def test_both_gateways_down_is_p1_and_starts_at_l2(self):
        self.app.set_fault("payment", "down")
        self.app.set_fault("backup", "down")
        for _ in range(3):
            self.app.monitor.tick()
        p1 = [i for i in self.app.itsm.list_incidents("open") if i["source"] == "payment_api"][0]
        self.assertEqual((p1["priority"], p1["level"]), ("P1", "L2"))

    def test_escalation_matrix_timers(self):
        iid = self.app.itsm.create_incident("Slow search", priority="P3")
        now = time.time()
        self.app.db.x("UPDATE incidents SET created_at=?, sla_due=? WHERE id=?", (now - 600, now + 300, iid))
        self.app.itsm.escalation_sweep()
        self.assertEqual(self.app.itsm.get_incident(iid)["level"], "L2")
        self.app.db.x("UPDATE incidents SET created_at=?, sla_due=? WHERE id=?", (now - 900, now + 100, iid))
        self.app.itsm.escalation_sweep()
        self.assertEqual(self.app.itsm.get_incident(iid)["level"], "L3")

    def test_sla_compliance(self):
        a = self.app.itsm.create_incident("A", priority="P2")
        b = self.app.itsm.create_incident("B", priority="P2")
        self.app.itsm.resolve(a, "fixed")
        self.app.db.x("UPDATE incidents SET sla_due=? WHERE id=?", (time.time() - 5, b))
        self.app.itsm.resolve(b, "fixed late")
        self.assertEqual(self.app.itsm.sla_compliance(), 50.0)

    def test_recurring_incidents_open_a_problem_and_resolution_feeds_kb(self):
        for _ in range(3):
            i = self.app.itsm.create_incident("Checkout timeout", priority="P2", source="checkout_errors", auto=True)
            self.app.itsm.resolve(i, "restarted")
        probs = self.app.itsm.list_problems()
        self.assertEqual(len(probs), 1)
        self.assertEqual(probs[0]["incidents"], 3)
        with self.assertRaises(ValueError):
            self.app.itsm.update_problem(probs[0]["id"], status="resolved")
        before = len(self.app.kb.search())
        self.app.itsm.update_problem(probs[0]["id"], status="resolved", root_cause="Gateway cap at 3x load",
                                     workaround="Use backup gateway")
        self.assertEqual(len(self.app.kb.search()), before + 1)


class TestChangeManagement(Base):
    def _approved(self, **kw):
        c = self.app.changes
        cid = c.create("Release 2.0", "new checkout", "high", "normal", "2.0.0", "dev")
        c.submit(cid)
        c.approve(cid, "board")
        return cid

    def test_four_eyes_and_blackout_and_full_rollout(self):
        c = self.app.changes
        cid = c.create("Release 2.0", "", "high", "normal", "2.0.0", "dev")
        with self.assertRaises(ValueError):
            c.start(cid)                                   # no direct-to-production
        c.submit(cid)
        with self.assertRaises(PermissionError):
            c.approve(cid, "dev")
        c.approve(cid, "board")
        self.app.set_sale(True)
        with self.assertRaises(ValueError):
            c.start(cid)                                   # blackout during sale
        self.app.set_sale(False)
        c.start(cid)
        for _ in range(3):
            c.advance(cid)
        self.assertEqual(c.get(cid)["status"], "completed")
        self.assertEqual(self.app.db.kv_get("app_version"), "2.0.0")

    def test_standard_low_risk_change_is_pre_approved(self):
        cid = self.app.changes.create("Config tweak", "", "low", "standard", "", "dev")
        self.app.changes.submit(cid)
        self.assertEqual(self.app.changes.get(cid)["status"], "approved")

    def test_canary_health_gate_triggers_auto_rollback(self):
        cid = self._approved()
        self.app.changes.start(cid)
        self.app.set_fault("payment", "down")
        self.app.set_fault("backup", "down")
        for _ in range(8):
            order(self.app)
        ch = self.app.changes.advance(cid)
        self.assertEqual(ch["status"], "rolled_back")
        self.assertTrue(any(i["source"] == "change_rollback" for i in self.app.itsm.list_incidents()))
        self.assertEqual(self.app.changes.failure_rate(), 100.0)


class TestScaleAndGovernance(Base):
    def test_flash_sale_load_test_passes_with_queue_and_autoscaling(self):
        res = loadtest.run(self.app, 100)
        self.assertTrue(res["passed"], res)
        self.assertGreaterEqual(res["success_pct"], 98)
        self.assertEqual(res["multiplier"], 100 / config.BASELINE_USERS)

    def test_hub_onboarding_is_gated_on_readiness(self):
        with self.assertRaises(ValueError) as cm:
            self.app.onboard_hub("Pune")
        self.assertIn("Phase 1", str(cm.exception))
        self.app.monitor.tick()
        loadtest.run(self.app, 60)
        self.app.onboard_hub("Pune")
        self.assertEqual(self.app.db.one("SELECT status FROM hubs WHERE name='Pune'")["status"], "active")

    def test_kpis_cover_the_deck(self):
        order(self.app)
        self.app.monitor.tick()
        keys = {k["key"] for k in self.app.kpis()["kpis"]}
        self.assertEqual(keys, {"uptime", "checkout", "scalability", "sla", "mttd", "cfr", "inventory",
                                "tracking", "csat", "repeat"})


if __name__ == "__main__":
    unittest.main()

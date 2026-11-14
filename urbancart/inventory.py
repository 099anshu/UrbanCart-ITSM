"""Central real-time inventory: one owner of stock, atomic reservation, hub reconciliation."""
import time


class Inventory:
    def __init__(self, db, on_mismatch=None):
        self.db = db
        self.on_mismatch = on_mismatch

    def reserve(self, sku, qty, hub=None):
        """Atomically reserve stock at the hub with most availability. Returns hub or None."""
        with self.db.tx():
            rows = self.db.q(
                "SELECT i.hub, i.on_hand - i.reserved AS avail FROM inventory i "
                "JOIN hubs h ON h.name=i.hub WHERE i.sku=? AND h.status='active' "
                "AND i.on_hand - i.reserved >= ? ORDER BY avail DESC", (sku, qty))
            if hub:
                rows = [r for r in rows if r["hub"] == hub]
            if not rows:
                return None
            chosen = rows[0]["hub"]
            self.db.x("UPDATE inventory SET reserved=reserved+? WHERE sku=? AND hub=?", (qty, sku, chosen))
            return chosen

    def commit(self, sku, hub, qty):
        self.db.x("UPDATE inventory SET on_hand=on_hand-?, reserved=reserved-? WHERE sku=? AND hub=?",
                  (qty, qty, sku, hub))

    def release(self, sku, hub, qty):
        self.db.x("UPDATE inventory SET reserved=reserved-? WHERE sku=? AND hub=?", (qty, sku, hub))

    def availability(self):
        return self.db.q(
            "SELECT p.sku,p.name,p.category,p.price, "
            "COALESCE(SUM(CASE WHEN h.status='active' THEN i.on_hand-i.reserved END),0) AS available "
            "FROM products p LEFT JOIN inventory i ON i.sku=p.sku LEFT JOIN hubs h ON h.name=i.hub "
            "WHERE p.category!='loadtest' GROUP BY p.sku ORDER BY p.category, p.name")

    def stock_by_hub(self):
        return self.db.q("SELECT i.sku, p.name, i.hub, i.on_hand, i.reserved FROM inventory i "
                         "JOIN products p ON p.sku=i.sku WHERE p.category!='loadtest' "
                         "ORDER BY i.sku, i.hub")

    def hub_sync(self, hub, counts):
        """Physical count from a hub scan. Differences are logged, corrected and raised as incidents."""
        h = self.db.one("SELECT status FROM hubs WHERE name=?", (hub,))
        if h is None:
            raise LookupError("hub %s not found" % hub)
        if h["status"] != "active":
            raise ValueError("hub %s is not active" % hub)
        mismatches = []
        with self.db.tx():
            for sku, physical in counts.items():
                physical = int(physical)
                if not self.db.one("SELECT 1 FROM products WHERE sku=?", (sku,)):
                    raise LookupError("unknown sku %s" % sku)
                row = self.db.one("SELECT on_hand FROM inventory WHERE sku=? AND hub=?", (sku, hub))
                self.db.kv_incr("inv_checked")
                if row is None:
                    self.db.x("INSERT INTO inventory(sku,hub,on_hand,reserved) VALUES(?,?,?,0)",
                              (sku, hub, physical))
                elif row["on_hand"] != physical:
                    self.db.x("INSERT INTO inventory_mismatches(ts,sku,hub,system_qty,physical_qty) "
                              "VALUES(?,?,?,?,?)", (time.time(), sku, hub, row["on_hand"], physical))
                    self.db.x("UPDATE inventory SET on_hand=? WHERE sku=? AND hub=?", (physical, sku, hub))
                    self.db.kv_incr("inv_mismatched")
                    mismatches.append({"sku": sku, "hub": hub, "system_qty": row["on_hand"],
                                       "physical_qty": physical})
        if self.on_mismatch:
            for m in mismatches:
                self.on_mismatch(m)
        return mismatches

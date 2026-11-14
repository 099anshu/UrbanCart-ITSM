"""Order hub: the single source of truth for order status across every support channel."""
import time
import uuid

CUSTOMER_MESSAGES = {
    "QUEUED": "We have your order and it is in the checkout queue.",
    "PROCESSING": "We are confirming stock and payment.",
    "CONFIRMED": "Payment received. Your order is being prepared for shipping.",
    "SHIPPED": "Your order has shipped. Use the tracking number to follow it.",
    "DELIVERED": "Delivered. Thank you for shopping with UrbanCart.",
    "FAILED": "We could not complete the payment. You have not been charged.",
    "OUT_OF_STOCK": "Sorry, this item just sold out. You have not been charged.",
}


class OrderHub:
    def __init__(self, db):
        self.db = db

    def create(self, customer, sku, qty, amount):
        oid = "UC-" + uuid.uuid4().hex[:8].upper()
        now = time.time()
        with self.db.tx():
            self.db.x("INSERT INTO orders(id,customer,sku,qty,amount,status,created_at,updated_at) "
                      "VALUES(?,?,?,?,?,?,?,?)", (oid, customer, sku, qty, amount, "QUEUED", now, now))
            self.db.x("INSERT INTO order_events(order_id,ts,status,note) VALUES(?,?,?,?)",
                      (oid, now, "QUEUED", "Order received"))
        return oid

    def set_status(self, oid, status, note, **fields):
        now = time.time()
        sets = "status=?, updated_at=?" + "".join(", %s=?" % k for k in fields)
        with self.db.tx():
            self.db.x("UPDATE orders SET %s WHERE id=?" % sets, (status, now, *fields.values(), oid))
            self.db.x("INSERT INTO order_events(order_id,ts,status,note) VALUES(?,?,?,?)",
                      (oid, now, status, note))

    def add_note(self, oid, note):
        o = self.row(oid)
        self.db.x("INSERT INTO order_events(order_id,ts,status,note) VALUES(?,?,?,?)",
                  (oid, time.time(), o["status"], note))

    def row(self, oid):
        o = self.db.one("SELECT * FROM orders WHERE id=?", (oid,))
        if o is None:
            raise LookupError("order %s not found" % oid)
        return o

    def view(self, oid):
        o = self.row(oid)
        events = self.db.q("SELECT ts,status,note FROM order_events WHERE order_id=? ORDER BY id", (oid,))
        return {"order_id": oid, "status": o["status"], "customer": o["customer"], "sku": o["sku"],
                "qty": o["qty"], "amount": o["amount"], "hub": o["hub"], "tracking": o["tracking"],
                "last_update": o["updated_at"], "message": CUSTOMER_MESSAGES.get(o["status"], ""),
                "timeline": events}

    def support_answer(self, channel, oid):
        """Chat, email and call all read the same record, so answers cannot conflict."""
        if channel not in ("chat", "email", "call"):
            raise ValueError("channel must be chat, email or call")
        return dict(self.view(oid), channel=channel)

    def deliver(self, oid):
        o = self.row(oid)
        if o["status"] != "SHIPPED":
            raise ValueError("only shipped orders can be marked delivered")
        self.set_status(oid, "DELIVERED", "Delivered to customer")

    def recent(self, limit=25):
        return self.db.q("SELECT id,customer,sku,qty,amount,status,hub,tracking,gateway,updated_at "
                         "FROM orders WHERE customer NOT LIKE 'loadtest-%' ORDER BY created_at DESC LIMIT ?",
                         (limit,))

    def feedback(self, oid, rating):
        self.row(oid)
        rating = int(rating)
        if not 1 <= rating <= 5:
            raise ValueError("rating must be 1-5")
        self.db.x("INSERT INTO csat(order_id,rating,ts) VALUES(?,?,?) "
                  "ON CONFLICT(order_id) DO UPDATE SET rating=excluded.rating, ts=excluded.ts",
                  (oid, rating, time.time()))

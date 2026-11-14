"""Simulated third-party services (payment gateway, logistics) with fault injection.

The case says these are weakly monitored third-party APIs. We simulate them so the
monitoring, fallback and ITSM processes can be demonstrated end to end.
"""
import random
import threading
import time


class ServiceDown(Exception):
    pass


class SimService:
    def __init__(self, name, max_concurrent=60):
        self.name = name
        self.max_concurrent = max_concurrent
        self.mode = "ok"            # ok | down | slow | flaky
        self.fault_since = None     # used to compute detection time (MTTD)
        self._inflight = 0
        self._lock = threading.Lock()

    def set_mode(self, mode):
        if mode not in ("ok", "down", "slow", "flaky"):
            raise ValueError("mode must be ok, down, slow or flaky")
        self.mode = mode
        self.fault_since = None if mode == "ok" else time.time()

    def health(self):
        """What a monitoring probe sees."""
        if self.mode == "down" or (self.mode == "flaky" and random.random() < 0.5):
            return {"ok": False, "latency_ms": None}
        return {"ok": True, "latency_ms": 450 if self.mode == "slow" else random.randint(20, 60)}

    def _call(self):
        with self._lock:
            self._inflight += 1
            overloaded = self._inflight > self.max_concurrent
        try:
            if self.mode == "down":
                raise ServiceDown(self.name + " unavailable")
            if overloaded:
                raise ServiceDown(self.name + " overloaded (concurrency cap exceeded)")
            if self.mode == "flaky" and random.random() < 0.5:
                raise ServiceDown(self.name + " returned an error")
            time.sleep(random.uniform(0.03, 0.08) * (6 if self.mode == "slow" else 1))
        finally:
            with self._lock:
                self._inflight -= 1


class PaymentGateway(SimService):
    def charge(self, order_id, amount):
        self._call()
        return "PAY-%08X" % random.getrandbits(32)


class LogisticsAPI(SimService):
    def create_shipment(self, order_id):
        self._call()
        return "TRK%010d" % random.randint(0, 9999999999)

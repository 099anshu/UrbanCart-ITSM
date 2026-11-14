#!/usr/bin/env python3
"""Fire a flash-sale load test at a running server:  python3 load_test.py --users 100"""
import argparse
import json
import time
import urllib.request

p = argparse.ArgumentParser()
p.add_argument("--users", type=int, default=100, help="concurrent shoppers (normal load is 20)")
p.add_argument("--url", default="http://localhost:8000")
a = p.parse_args()


def call(path, body=None):
    req = urllib.request.Request(a.url + path, data=json.dumps(body).encode() if body is not None else None,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req) as r:
        return json.load(r)


call("/api/loadtest", {"users": a.users})
print("Load test started with %d simultaneous shoppers..." % a.users)
while True:
    time.sleep(1)
    s = call("/api/loadtest")
    if not s["running"]:
        break
r = s["last"]
print("Mode: %(mode)s | %(multiplier)sx normal load | success %(success_pct)s%% | p95 %(p95_ms)s ms | peak workers %(workers_peak)s" % r)
print("RESULT:", "PASS (>=98% checkout success)" if r["passed"] else "FAIL (below 98% checkout success)")

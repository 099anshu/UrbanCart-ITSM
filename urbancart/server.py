"""HTTP API + dashboard, standard library only (no installs needed)."""
import json
import os
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

STATIC = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "static")


def build_routes(app):
    R = []

    def add(method, pattern, fn):
        R.append((method, re.compile("^" + pattern + "$"), fn))

    def qs(q, k, d=None):
        return q.get(k, [d])[0]

    # --- overview
    add("GET", "/api/health", lambda m, b, q: app.health())
    add("GET", "/api/kpis", lambda m, b, q: app.kpis())
    add("GET", "/api/alerts", lambda m, b, q: app.monitor.alerts())
    # --- shop, orders, support
    add("GET", "/api/products", lambda m, b, q: app.inventory.availability())
    add("GET", "/api/stock", lambda m, b, q: app.inventory.stock_by_hub())
    add("POST", "/api/checkout", lambda m, b, q: (202, {"order_id": app.checkout.submit(
        b.get("customer"), b.get("sku"), b.get("qty", 1))}))
    add("GET", "/api/orders", lambda m, b, q: app.orders.recent())
    add("GET", r"/api/orders/([\w-]+)", lambda m, b, q: app.orders.view(m[0]))
    add("POST", r"/api/orders/([\w-]+)/deliver", lambda m, b, q: (app.orders.deliver(m[0]), {"ok": True})[1])
    add("POST", r"/api/orders/([\w-]+)/feedback", lambda m, b, q: (app.orders.feedback(m[0], b.get("rating")), {"ok": True})[1])
    add("GET", r"/api/support/(\w+)/orders/([\w-]+)", lambda m, b, q: app.orders.support_answer(m[0], m[1]))
    # --- hubs & inventory
    add("GET", "/api/hubs", lambda m, b, q: app.hubs())
    add("POST", r"/api/hubs/([\w ]+)/onboard", lambda m, b, q: (app.onboard_hub(m[0]), {"ok": True})[1])
    add("POST", "/api/inventory/sync", lambda m, b, q: {"mismatches": app.inventory.hub_sync(b.get("hub"), b.get("counts") or {})})
    # --- incidents
    add("GET", "/api/incidents", lambda m, b, q: app.itsm.list_incidents(qs(q, "status")))
    add("POST", "/api/incidents", lambda m, b, q: (201, app.itsm.get_incident(app.itsm.create_incident(
        b.get("title"), b.get("description", ""), b.get("priority", "P3")))))
    add("GET", r"/api/incidents/(\d+)", lambda m, b, q: app.itsm.get_incident(int(m[0])))
    add("POST", r"/api/incidents/(\d+)/ack", lambda m, b, q: (app.itsm.acknowledge(int(m[0]), b.get("by", "agent")), {"ok": True})[1])
    add("POST", r"/api/incidents/(\d+)/resolve", lambda m, b, q: (app.itsm.resolve(int(m[0]), b.get("resolution"), b.get("by", "agent")), {"ok": True})[1])
    add("POST", r"/api/incidents/(\d+)/escalate", lambda m, b, q: (app.itsm.escalate(int(m[0]), b.get("reason", "manual escalation")), {"ok": True})[1])
    # --- problems
    add("GET", "/api/problems", lambda m, b, q: app.itsm.list_problems())
    add("POST", "/api/problems", lambda m, b, q: (201, {"id": app.itsm.create_problem(b.get("title"))}))
    add("GET", r"/api/problems/(\d+)", lambda m, b, q: app.itsm.get_problem(int(m[0])))
    add("POST", r"/api/problems/(\d+)/update", lambda m, b, q: (app.itsm.update_problem(
        int(m[0]), b.get("status"), b.get("root_cause"), b.get("workaround")), {"ok": True})[1])
    # --- changes
    add("GET", "/api/changes", lambda m, b, q: app.changes.list())
    add("POST", "/api/changes", lambda m, b, q: (201, {"id": app.changes.create(
        b.get("title"), b.get("description", ""), b.get("risk", "medium"), b.get("type", "normal"),
        b.get("version", ""), b.get("requester", "unknown"))}))
    add("GET", r"/api/changes/(\d+)", lambda m, b, q: app.changes.get(int(m[0])))
    add("POST", r"/api/changes/(\d+)/submit", lambda m, b, q: (app.changes.submit(int(m[0])), {"ok": True})[1])
    add("POST", r"/api/changes/(\d+)/approve", lambda m, b, q: (app.changes.approve(int(m[0]), b.get("approver")), {"ok": True})[1])
    add("POST", r"/api/changes/(\d+)/reject", lambda m, b, q: (app.changes.reject(int(m[0]), b.get("approver"), b.get("reason", "")), {"ok": True})[1])
    add("POST", r"/api/changes/(\d+)/start", lambda m, b, q: (app.changes.start(int(m[0])), {"ok": True})[1])
    add("POST", r"/api/changes/(\d+)/advance", lambda m, b, q: app.changes.advance(int(m[0])))
    add("POST", r"/api/changes/(\d+)/rollback", lambda m, b, q: (app.changes.rollback(int(m[0]), b.get("reason", "manual rollback")), {"ok": True})[1])
    # --- knowledge base
    add("GET", "/api/kb", lambda m, b, q: app.kb.search(qs(q, "q", "")))
    add("POST", "/api/kb", lambda m, b, q: (201, {"id": app.kb.add(b.get("title"), b.get("body"), b.get("tags", ""))}))
    # --- simulation controls
    add("POST", "/api/sim/fault", lambda m, b, q: (app.set_fault(b.get("service"), b.get("mode")), {"ok": True})[1])
    add("POST", "/api/sim/sale", lambda m, b, q: (app.set_sale(bool(b.get("on"))), {"ok": True})[1])
    add("POST", "/api/loadtest", lambda m, b, q: (202, (app.start_loadtest(b.get("users", 100)), {"started": True})[1]))
    add("GET", "/api/loadtest", lambda m, b, q: app.loadtest_status())
    return R


def make_handler(app):
    routes = build_routes(app)

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _send(self, code, payload, ctype="application/json"):
            body = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _dispatch(self, method):
            url = urlparse(self.path)
            if method == "GET" and url.path in ("/", "/index.html"):
                with open(os.path.join(STATIC, "index.html"), "rb") as f:
                    return self._send(200, f.read(), "text/html; charset=utf-8")
            body = {}
            if method == "POST":
                n = int(self.headers.get("Content-Length") or 0)
                try:
                    body = json.loads(self.rfile.read(n) or b"{}")
                except ValueError:
                    return self._send(400, {"error": "invalid JSON"})
            for meth, rx, fn in routes:
                m = rx.match(url.path)
                if meth == method and m:
                    try:
                        res = fn(m.groups(), body, parse_qs(url.query))
                        code, data = res if isinstance(res, tuple) else (200, res)
                        return self._send(code, data)
                    except PermissionError as e:
                        return self._send(403, {"error": str(e)})
                    except LookupError as e:
                        return self._send(404, {"error": str(e).strip("'\"")})
                    except ValueError as e:
                        return self._send(400, {"error": str(e)})
                    except RuntimeError as e:
                        return self._send(409, {"error": str(e)})
            self._send(404, {"error": "not found"})

        def do_GET(self):
            self._dispatch("GET")

        def do_POST(self):
            self._dispatch("POST")

    return Handler


def serve(app, port):
    srv = ThreadingHTTPServer(("127.0.0.1", port), make_handler(app))
    srv.daemon_threads = True
    srv.request_queue_size = 256
    return srv

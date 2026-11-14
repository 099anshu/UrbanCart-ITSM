#!/usr/bin/env python3
"""Start the UrbanCart ITSM platform:  python3 run.py [--port 8000] [--legacy] [--reset]"""
import argparse
import os

p = argparse.ArgumentParser(description=__doc__)
p.add_argument("--port", type=int, default=int(os.environ.get("PORT", 8000)))
p.add_argument("--db", default=os.environ.get("DB_PATH", "urbancart.db"))
p.add_argument("--legacy", action="store_true", help="run the 'before' state (no queue, no fallback gateway)")
p.add_argument("--reset", action="store_true", help="delete the database and start fresh")
args = p.parse_args()

os.environ["DB_PATH"] = args.db
os.environ["LEGACY_MODE"] = "1" if args.legacy else "0"

from urbancart.app import App          # noqa: E402  (after env is set)
from urbancart.server import serve     # noqa: E402

if args.reset:
    for suffix in ("", "-wal", "-shm"):
        if os.path.exists(args.db + suffix):
            os.remove(args.db + suffix)

app = App(args.db, legacy=args.legacy)
app.start()
srv = serve(app, args.port)
print("UrbanCart ITSM running in %s mode" % ("LEGACY (before)" if args.legacy else "SCALABLE (after)"))
print("Dashboard: http://localhost:%d   (Ctrl+C to stop)" % args.port)
try:
    srv.serve_forever()
except KeyboardInterrupt:
    print("\nStopping...")
finally:
    app.stop()
    srv.server_close()

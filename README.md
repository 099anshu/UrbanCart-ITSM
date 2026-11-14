# UrbanCart ITSM Platform

A working implementation of the solution proposed in the **UrbanCart Retail ITSM Case Study (Case Study 5)**:
scalable checkout, a single real-time inventory/order hub, proactive monitoring, and an ITSM toolset
(incident, problem, change, knowledge) with the KPIs from the presentation.

**Zero dependencies.** Python 3.9+ standard library only (SQLite + built-in HTTP server). Runs the same on
Apple Silicon (M1/M2/M3), Intel Macs, Linux and Windows.

---

## 1. Run it on a Mac (M1)

1. Open **Terminal** (Cmd+Space, type "Terminal").
2. Check Python: `python3 --version`.
   - If it prints 3.9 or higher, you are fine.
   - If macOS offers to install "command line developer tools", click **Install**, wait, then retry.
   - Optional (newer Python): install [Homebrew](https://brew.sh), then `brew install python`.
3. Unzip the project and go into the folder:
   ```bash
   cd ~/Downloads/urbancart-itsm
   ```
4. Run the tests (about 3 seconds, 18 tests):
   ```bash
   python3 -m unittest discover -s tests -v
   ```
5. Start the platform:
   ```bash
   python3 run.py
   ```
6. Open **http://localhost:8000** in your browser. Stop with `Ctrl+C`.

Other options: `python3 run.py --port 9000`, `--reset` (wipe the database), `--legacy` (the "before" state).

---

## 2. Upload to GitHub (so you can share it)

**A. Create an empty repo.** Go to https://github.com/new, name it `urbancart-itsm`, leave "Add a README" and
".gitignore" **unchecked**, click **Create repository**.

**B. Push from Terminal** (inside the project folder). Replace `YOUR-USERNAME`:
```bash
git init
git add .
git commit -m "UrbanCart ITSM platform: initial version"
git branch -M main
git remote add origin https://github.com/YOUR-USERNAME/urbancart-itsm.git
git push -u origin main
```
- If `git` asks to install developer tools, accept. First time only, set your identity:
  `git config --global user.name "Your Name"` and `git config --global user.email "you@example.com"`.
- GitHub does **not** accept your account password. When prompted, use a **Personal Access Token**
  (GitHub → Settings → Developer settings → Personal access tokens → generate with `repo` scope) as the password,
  or the easier route: `brew install gh && gh auth login`, then `gh repo create urbancart-itsm --public --source=. --push`.

**C. Share** the link `https://github.com/YOUR-USERNAME/urbancart-itsm`. Teammates run steps 1.3 to 1.6 after
`git clone https://github.com/YOUR-USERNAME/urbancart-itsm.git`.

---

## 3. Five-minute demo script (follows the deck)

| Step | Do this in the dashboard | Deck slide it proves |
|---|---|---|
| 1 | **Overview**: KPI cards vs targets, roadmap milestones | KPIs, Expected Outcomes |
| 2 | **Shop** → Buy an item → **Track**: chat, email and call show the same status | Single source of truth |
| 3 | **Overview** → set `payment` to *down*. Within seconds an incident appears with P2, KB suggestions and a detection time. Orders still succeed via the backup gateway. Restore it and the incident auto-resolves | Proactive monitoring, event/incident management |
| 4 | Also set `backup` to *down*: P1, routed straight to L2, checkout fails safely and stock is released | Escalation matrix |
| 5 | Run the **load test** at 100 shoppers (5x normal): passes, workers autoscale. Compare: stop, run `python3 run.py --legacy`, run the same test and see ~60% success | Scalability root cause, 78% → 98% |
| 6 | **Changes**: create → submit → approve (different person) → start canary → advance. Start a flash sale and the release is blocked. Break both gateways during a canary and advancing triggers an automatic rollback | Change enablement, staged releases |
| 7 | **Incidents**: resolve the same auto-incident 3 times and a **Problem** opens automatically. Add a root cause and resolve it: a known-error article appears in the **Knowledge Base** | Problem management, continual improvement |
| 8 | **Hubs**: try to onboard *Pune* (blocked). Run a load test, then onboard. Submit a hub scan with a wrong count: mismatch corrected and logged | Roadmap dependency, unified inventory |

Terminal load test: `python3 load_test.py --users 100` (server must be running).
To watch SLA escalation live: `SLA_SCALE=0.01 python3 run.py` then create a P3 incident.

---

## 4. How the deck maps to the code

| Deck element | Where it lives |
|---|---|
| Auto-scaling, queued checkout, load testing | `urbancart/checkout.py` (worker pool + autoscaler), `urbancart/loadtest.py` |
| Fallback gateway for unreliable third-party APIs | `checkout.py` `_pay()` (circuit breaker), `urbancart/sim.py` |
| Central real-time inventory and order hub | `urbancart/inventory.py` (atomic reservation, hub scans), `urbancart/orders.py` |
| One order status on every channel | `orders.py` `support_answer()`, `/api/support/<channel>/orders/<id>` |
| Monitoring, alert thresholds, auto-created tickets | `urbancart/monitoring.py` |
| Incident mgmt: priority, SLA clock, L1→L2→L3 escalation | `urbancart/itsm.py` |
| Problem mgmt: RCA, known errors | `itsm.py` (auto-opened after 3 repeats) |
| Change mgmt: change board, canary releases, rollback | `urbancart/changes.py` |
| Shared knowledge base, self-service order status | `itsm.py` `KB`, dashboard Shop tab |
| KPI dashboard | `urbancart/kpi.py` |
| Phase 3 hubs only after stabilisation | `app.py` `onboard_hub()` |
| "Before" state (78% checkout) | `python3 run.py --legacy` |

## 5. Assumptions (be upfront about these in your presentation)

- Payment, logistics and the backup gateway are **simulated** (`sim.py`). Fault injection stands in for real outages.
- Hosting model and tooling are not given in the case, so this is a self-contained reference build, not
  ServiceNow/Datadog. The designs map directly onto those tools.
- Targets marked proposed in the deck (5x headroom, <15% change failure rate) are used as-is.
- Inventory-mismatch reduction uses an **assumed 10% legacy mismatch rate** (`BASELINE_MISMATCH_RATE`).
- "Order tracking accuracy" is measured as cross-channel consistency. CSAT and repeat purchase need real
  feedback data; the dashboard shows "no data" until orders and ratings exist.
- No authentication: this is a local demo. Add auth before exposing it beyond localhost.

## 6. Configuration

Environment variables (all optional): `PORT`, `DB_PATH`, `LEGACY_MODE`, `MIN_WORKERS`, `MAX_WORKERS`,
`BASELINE_USERS`, `GATEWAY_MAX_CONCURRENT`, `MONITOR_INTERVAL`, `SLA_SCALE`. See `urbancart/config.py`.

## 7. Project layout

```
run.py            start the server          load_test.py   CLI flash-sale test
static/index.html dashboard                 tests/         18 unit/integration tests
urbancart/        app, checkout, inventory, orders, monitoring, itsm, changes, kpi, loadtest, sim, server
```

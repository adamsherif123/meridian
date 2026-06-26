# Meridian — Walking Skeleton

A thin vertical slice that proves a single button click travels through all four stack layers and back.
No product features — just the wiring.

---

## Prerequisites

| Tool | Version | Install |
|------|---------|---------|
| Python | **3.11+** | `brew install python@3.11` |
| Node | 18+ | ✓ already installed |
| Temporal CLI | any | `brew install temporal` |

> **Python 3.9 is installed but 3.11+ is required.** Run `brew install python@3.11`, then create the venv with `python3.11 -m venv .venv`.

---

## Secrets — fill these in before running

Copy `backend/.env.example` to `backend/.env` and paste in three values:

| Variable | Where to get it |
|----------|-----------------|
| `SUPABASE_URL` | [Supabase dashboard](https://supabase.com) → project → Settings → API → Project URL |
| `SUPABASE_SERVICE_KEY` | Same page → `service_role` secret key |
| `COMPOSIO_API_KEY` | [app.composio.dev](https://app.composio.dev) → Settings → API Keys |

`TEMPORAL_ADDRESS` defaults to `localhost:7233` — no change needed for local dev.

**Also run the schema once:** open the Supabase SQL Editor, paste `supabase/schema.sql`, and click Run.

---

## One-time setup

```bash
# Python virtualenv (use python3.11 once installed)
cd backend
python3.11 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# Frontend deps (already done if you cloned and ran npm install)
cd ../web
npm install
```

---

## Starting the four processes (in order)

Open four terminal tabs:

```bash
# 1. Temporal dev server + UI
temporal server start-dev
# → UI at http://localhost:8233

# 2. FastAPI (in backend/ with .venv active)
cd backend && source .venv/bin/activate
uvicorn backend.api.main:app --reload --port 8000

# 3. Temporal worker (in backend/ with .venv active)
cd backend && source .venv/bin/activate
python -m backend.worker.run

# 4. Vite dev server
cd web && npm run dev
# → http://localhost:5173
```

Open **http://localhost:5173**, click **"Run walking skeleton"**, and watch four status cards update.

---

## What a green run looks like

All four cards show ✓:

| Card | Green means |
|------|-------------|
| API | FastAPI returned HTTP 200 |
| Temporal | Workflow started and completed; shows workflow ID |
| Composio | SDK connected; shows tool/app count |
| Supabase | Row inserted and read back; shows UUID + timestamp |

Cards that show ⚙ `not_configured` mean the secret for that leg is missing — the other legs still run.

---

## Architecture — the click path

```
React button
  └─ POST /api/v1/skeleton/run
       └─ FastAPI (Temporal CLIENT)
            └─ execute_workflow(SkeletonWorkflow)   ← Temporal server at :7233
                 └─ Temporal WORKER polls task queue "meridian-skeleton"
                      └─ SkeletonWorkflow.run()
                           └─ execute_activity(run_skeleton_checks)
                                ├─ Composio: verify SDK + API key
                                └─ Supabase: insert row, read it back
                      returns {"composio": {...}, "supabase": {...}}
  ← FastAPI wraps in {"temporal": {...}, ...} and returns
React renders per-leg status cards
```

The Temporal app runs as **two separate processes** against a shared Temporal server:
- **Worker** (`backend/worker/run.py`) — hosts workflow + activity code, polls for tasks
- **Client** (`backend/api/main.py`) — FastAPI, calls `execute_workflow` per request

All I/O (Composio, Supabase, env reads) lives in `backend/activities/checks.py`.
Workflows stay deterministic — minimal imports, no network calls.

---

## Repo structure

```
meridian_takehome/
├── backend/
│   ├── api/           FastAPI app (Temporal client endpoint)
│   ├── worker/        Temporal worker entrypoint
│   ├── workflows/     Workflow definitions (minimal imports)
│   ├── activities/    Activity definitions (all I/O here)
│   ├── spec/          Product + engineering specs (stub)
│   ├── codegen/       Code-generation utilities (stub)
│   ├── skeleton/      Skeleton-generation logic (stub)
│   ├── evals/         Evaluation harnesses (stub)
│   ├── tools/         Composio tool wrappers (stub)
│   ├── requirements.txt
│   └── .env.example
├── web/               Vite React + TypeScript frontend
├── supabase/          schema.sql + setup guide
├── docs/              Architecture docs (stub)
├── README.md
└── .gitignore
```

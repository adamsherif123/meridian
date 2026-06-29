# Meridian

An AI-powered shipment pre-alert compliance agent. Receives pharma shipment emails, classifies attached documents (COAs, invoices, manifests, packing lists), validates regulatory codes and line-item counts against spec, and produces an 8-column CSV result — all orchestrated through a Temporal workflow with a React canvas UI.

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

Copy `backend/.env.example` to `backend/.env` and paste in the values:

| Variable | Where to get it |
|----------|-----------------|
| `SUPABASE_URL` | [Supabase dashboard](https://supabase.com) → project → Settings → API → Project URL |
| `SUPABASE_SERVICE_KEY` | Same page → `service_role` secret key |
| `COMPOSIO_API_KEY` | [app.composio.dev](https://app.composio.dev) → Settings → API Keys |
| `COMPOSIO_CONNECTED_ACCOUNT_ID` | Composio → Gmail connection → account ID |
| `COMPOSIO_USER_ID` | Composio → user ID |
| `ANTHROPIC_API_KEY` | [console.anthropic.com](https://console.anthropic.com) → API Keys |

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

# Frontend deps
cd ../web
npm install
```

---

## Starting the system (in order)

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
backend/.venv/bin/python -m backend.runtime.worker

# 4. Vite dev server
cd web && npm run dev
# → http://localhost:5173
```

Open **http://localhost:5173** to use the canvas UI.

---

## Optional: background inbox listener

Polls Gmail every 5 minutes and automatically runs the agent on new shipment emails:

```bash
cd backend && source .venv/bin/activate
backend/.venv/bin/python -m backend.listener
```

Set `REPORT_EMAIL_ON_LISTENER=true` and `REPORT_EMAIL_TO=your@email.com` in `.env` to also email the CSV result after each auto-run.

---

## Architecture

```
React canvas UI (board builder + run panel)
  └─ POST /api/v1/boards/{id}/run-live
       └─ FastAPI (Temporal client)
            └─ execute_workflow(GeneratedAgentWorkflow)   ← Temporal :7233
                 └─ Temporal worker (backend/runtime/worker.py)
                      └─ Activities:
                           ├─ fetch_email_and_attachments  (Composio/Gmail)
                           ├─ extract_email_facts          (LLM)
                           ├─ classify_documents           (LLM + vision)
                           ├─ match_documents              (LLM)
                           ├─ validate_fields              (LLM + regex)
                           ├─ tally                        (deterministic)
                           └─ emit_report / send_report    (CSV + Gmail)
  ← FastAPI returns {subject, csv_content, result_json}
React renders result card with 8-column CSV download
```

The worker auto-discovers generated agents in `backend/agents/generated/agent_*.py`.
Boards and their spec are stored in Supabase (`boards` + `frozen_specs` tables).
Results are persisted to `agent_runs`; dedup is via `processed_emails`.

---

## Repo structure

```
meridian_takehome/
├── backend/
│   ├── api/              FastAPI routes (boards, run-live, codegen, etc.)
│   ├── runtime/
│   │   ├── activities/   All Temporal activities (LLM, Composio, Supabase I/O)
│   │   └── worker.py     Worker entry point — auto-discovers generated agents
│   ├── workflows/        Workflow definitions (skeleton.py + generated base)
│   ├── worker/           Skeleton workflow worker entry point
│   ├── agents/
│   │   └── generated/    Generated agent_<uuid>.py files (one per board)
│   ├── codegen/          Prompt + code-generation pipeline
│   ├── evals/            Evaluation harness + test cases (boards 7, 9, 10)
│   ├── selfheal/         Runtime self-repair loop for generated agents
│   ├── listener.py       Background Gmail poller
│   ├── requirements.txt
│   └── .env.example
├── web/                  Vite + React + TypeScript canvas UI
├── supabase/             schema.sql
├── docs/                 Architecture writeup
├── README.md
└── .gitignore
```
# meridian

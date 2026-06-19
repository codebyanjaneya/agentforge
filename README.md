# 🚀 AgentForge

**Multi-Agent AI Operating System** — a startup-in-a-box powered by autonomous LLM agents that collaborate, remember, act, and improve themselves.

Built with **FastAPI**, **Next.js**, **Groq (Llama 3.3 70B)**, **Redis**, and **PostgreSQL**.

---

## ✨ Features

- **7 specialized agents** — CEO, Sales, Marketing, Support, Research, Finance, and Hiring — each with its own system prompt and persona.
- **Memory** — short-term conversation memory in Redis and long-term summaries in PostgreSQL, so agents stay consistent over time.
- **CEO delegation** — the CEO agent breaks a goal into subtasks and automatically delegates them to the relevant agents.
- **Autonomous execution** — agents turn responses into concrete, tracked actions (email drafts, reports, strategies, leads, analyses).
- **Task scheduler** — schedule recurring agent tasks (stored in Redis).
- **Self-improving agents** — an AI performance analyst reviews each agent's history and produces strengths, weaknesses, improvements, and an updated strategy.
- **Digital Twin dashboard** — a live startup simulation with KPIs, a revenue chart, an agent activity feed, and CEO forecasts.

---

## 🧱 Architecture

```
startup-os/
├── backend/                  # FastAPI + Groq
│   ├── main.py               # API + WebSocket entrypoint
│   ├── orchestrator.py       # Routes tasks to agents, CEO delegation
│   ├── autonomous.py         # Autonomous executor (response → actions)
│   ├── agents/               # ceo, sales, marketing, support, research, finance, hiring
│   ├── memory/
│   │   ├── short_term.py      # Redis-backed recent messages
│   │   └── long_term.py       # PostgreSQL summaries (SQLAlchemy)
│   ├── tasks/
│   │   └── scheduler.py       # Recurring task scheduling (Redis)
│   ├── improvement/
│   │   ├── analyzer.py        # Analyzes agent history with Groq
│   │   └── self_improver.py   # Stores improvement reports (Redis)
│   └── requirements.txt
└── frontend/                 # Next.js + Tailwind dashboard
    └── app/
        ├── page.tsx          # Agent control center (7 cards)
        ├── twin/page.tsx     # Digital Twin simulation
        ├── autonomous/page.tsx
        └── improve/page.tsx  # Self-improving agents
```

---

## 🛠️ Prerequisites

- Python 3.11+
- Node.js 18+
- Redis (default `localhost:6379`)
- PostgreSQL (database `agentforge`)
- A free [Groq API key](https://console.groq.com)

Redis and PostgreSQL can be run via Docker:

```bash
docker run -d --name agentforge-redis -p 6379:6379 redis
docker run -d --name agentforge-postgres -e POSTGRES_PASSWORD=agentforge123 -e POSTGRES_DB=agentforge -p 5433:5432 postgres
```

---

## ⚙️ Setup

### Backend

```bash
cd backend
python -m venv venv
venv\Scripts\activate          # Windows
# source venv/bin/activate     # macOS/Linux
pip install -r requirements.txt
```

Create `backend/.env`:

```env
GROQ_API_KEY=your_groq_api_key_here
MODEL_NAME=llama-3.3-70b-versatile
REDIS_URL=redis://localhost:6379
DATABASE_URL=postgresql://postgres:agentforge123@localhost:5433/agentforge
```

Run the API:

```bash
uvicorn main:app --reload --port 8000
```

### Frontend

```bash
cd frontend
npm install
npm run dev
```

Open [http://localhost:3000](http://localhost:3000).

---

## 🔌 API Reference

| Method | Endpoint                  | Description                                  |
| ------ | ------------------------- | -------------------------------------------- |
| GET    | `/health`                 | Health check                                 |
| WS     | `/ws`                     | Real-time agent chat (`{agent, task}`)       |
| POST   | `/autonomous/run`         | Run an agent and extract actions             |
| POST   | `/tasks/schedule`         | Schedule a recurring task                    |
| GET    | `/tasks`                  | List scheduled tasks                         |
| DELETE | `/tasks/{task_id}`        | Remove a scheduled task                      |
| POST   | `/improve/{agent_name}`   | Run self-improvement analysis                |
| GET    | `/improve/{agent_name}`   | Get the latest improvement report            |

---

## 🤖 Agents

| Agent     | Focus                                                        |
| --------- | ----------------------------------------------------------- |
| CEO       | Goal setting, delegation, strategy, revenue forecasting     |
| Sales     | Lead generation, outreach, closing deals                    |
| Marketing | Content, campaigns, SEO, social, branding                   |
| Support   | Support replies, complaint resolution, FAQs                 |
| Research  | Market research, competitor analysis, trends                |
| Finance   | Expenses, revenue, cashflow, burn-rate alerts               |
| Hiring    | Resume screening, interviews, candidate ranking             |

---

## 📄 License

MIT

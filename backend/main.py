import asyncio
import logging
import os
from contextlib import asynccontextmanager

from dotenv import load_dotenv
from fastapi import FastAPI, Request, Response, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from autonomous import AutonomousExecutor
from improvement.self_improver import SelfImprover
from memory.long_term import LongTermMemory
from orchestrator import AgentOrchestrator
from tasks.scheduler import TaskScheduler
from telegram_bot import start_telegram_bot
from voice_handler import build_incoming_response, process_recording

load_dotenv()

logger = logging.getLogger("uvicorn.error")


@asynccontextmanager
async def lifespan(app: FastAPI):
    bot_task: asyncio.Task | None = None
    if os.getenv("TELEGRAM_BOT_TOKEN"):
        bot_task = asyncio.create_task(start_telegram_bot())
        logger.info("Telegram bot launched as a background task.")
    else:
        logger.warning(
            "TELEGRAM_BOT_TOKEN not set; skipping Telegram bot startup."
        )
    try:
        yield
    finally:
        if bot_task is not None:
            bot_task.cancel()
            try:
                await bot_task
            except asyncio.CancelledError:
                pass


app = FastAPI(title="Multi-Agent Startup OS", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:3000"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

orchestrator = AgentOrchestrator()
executor = AutonomousExecutor(orchestrator)
scheduler = TaskScheduler()
improver = SelfImprover()
long_term = LongTermMemory()


class AutonomousRequest(BaseModel):
    agent: str
    task: str
    user_email: str


class ScheduleRequest(BaseModel):
    agent: str
    task: str
    interval_minutes: int


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.post("/autonomous/run")
async def autonomous_run(payload: AutonomousRequest):
    return await executor.execute(
        payload.agent, payload.task, payload.user_email
    )


@app.post("/tasks/schedule")
async def schedule_task(payload: ScheduleRequest):
    return scheduler.add_task(
        payload.agent, payload.task, payload.interval_minutes
    )


@app.get("/tasks")
async def list_tasks():
    return scheduler.get_scheduled_tasks()


@app.delete("/tasks/{task_id}")
async def delete_task(task_id: str):
    removed = scheduler.remove_task(task_id)
    return {"removed": removed, "id": task_id}


@app.post("/improve/{agent_name}")
async def run_improvement(agent_name: str):
    return await improver.improve(agent_name)


@app.get("/improve/{agent_name}")
async def get_improvement(agent_name: str):
    return improver.get_improvements(agent_name)


@app.get("/history/{user_email}")
async def get_history(user_email: str):
    """Return all stored conversations for a user, newest first."""
    return long_term.get_history(user_email)


@app.post("/voice/incoming")
async def voice_incoming():
    return Response(
        content=build_incoming_response(),
        media_type="application/xml",
    )


@app.post("/voice/process")
async def voice_process(request: Request):
    form = await request.form()
    recording_url = form.get("RecordingUrl", "")
    twiml = await process_recording(recording_url, orchestrator)
    return Response(content=twiml, media_type="application/xml")


@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    await websocket.accept()
    try:
        while True:
            data = await websocket.receive_json()

            if data.get("type") == "ping":
                await websocket.send_json({"type": "pong"})
                continue

            agent = data.get("agent")
            task = data.get("task")
            user_email = data.get("user_email")

            if not user_email:
                await websocket.send_json(
                    {
                        "error": "Missing 'user_email'. Please log in before "
                        "sending tasks."
                    }
                )
                continue

            if not agent or not task:
                await websocket.send_json(
                    {"error": "Both 'agent' and 'task' are required."}
                )
                continue

            try:
                if agent == "ceo":
                    result = await orchestrator.run_with_delegation(
                        agent, task, user_email
                    )
                    await websocket.send_json(
                        {
                            "agent": agent,
                            "result": result["primary_response"],
                            "primary_agent": result["primary_agent"],
                            "primary_response": result["primary_response"],
                            "delegations": result["delegations"],
                        }
                    )
                else:
                    result = await orchestrator.run_agent(
                        agent, task, user_email
                    )
                    await websocket.send_json({"agent": agent, "result": result})
            except ValueError as exc:
                await websocket.send_json({"agent": agent, "error": str(exc)})
            except Exception as exc:  # noqa: BLE001
                await websocket.send_json(
                    {"agent": agent, "error": f"Agent failed: {exc}"}
                )
    except WebSocketDisconnect:
        pass

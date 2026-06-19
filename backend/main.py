from dotenv import load_dotenv
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from autonomous import AutonomousExecutor
from improvement.self_improver import SelfImprover
from orchestrator import AgentOrchestrator
from tasks.scheduler import TaskScheduler

load_dotenv()

app = FastAPI(title="Multi-Agent Startup OS")

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


class AutonomousRequest(BaseModel):
    agent: str
    task: str


class ScheduleRequest(BaseModel):
    agent: str
    task: str
    interval_minutes: int


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.post("/autonomous/run")
async def autonomous_run(payload: AutonomousRequest):
    return await executor.execute(payload.agent, payload.task)


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


@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    await websocket.accept()
    try:
        while True:
            data = await websocket.receive_json()
            agent = data.get("agent")
            task = data.get("task")

            if not agent or not task:
                await websocket.send_json(
                    {"error": "Both 'agent' and 'task' are required."}
                )
                continue

            try:
                if agent == "ceo":
                    result = await orchestrator.run_with_delegation(agent, task)
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
                    result = await orchestrator.run_agent(agent, task)
                    await websocket.send_json({"agent": agent, "result": result})
            except ValueError as exc:
                await websocket.send_json({"agent": agent, "error": str(exc)})
            except Exception as exc:  # noqa: BLE001
                await websocket.send_json(
                    {"agent": agent, "error": f"Agent failed: {exc}"}
                )
    except WebSocketDisconnect:
        pass

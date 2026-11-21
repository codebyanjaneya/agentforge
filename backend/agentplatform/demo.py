"""DEMO MODE and the starter workforce (M5 product sprint).

DEMO MODE (``AGENTFORGE_DEMO_MODE=1``, refused in production) lets anyone run the whole AgentForge journey WITHOUT a model credential and without any outside service.
It changes exactly two things, and says so everywhere it is visible (``GET /demo``, a banner in the Studio):

* the MODEL is a SCRIPTED stand-in (``DemoProvider``). It plans one workflow (launch readiness) and, when an agent holds tools, asks for them in a fixed order with the
  numbers it reads from the user's own objective. It is not intelligent and it is not a live model; an objective it does not understand is declined, not improvised.
* the SENDING tool is a SIMULATION (``demo.send_note``: nothing is sent anywhere). It is registered only in demo mode and needs a human approval for every call.

Everything else is the real product: the planner's validation, plan approval, the run scheduler, durable suspension, the REAL shipped tools (unit economics, date arithmetic,
text analysis compute real results from the real inputs), exact-action approval, the event history and the retry machinery. A demonstration must never present the scripted
model as a live one or the simulated send as a live integration.

THE STARTER WORKFORCE is a product feature in every mode: three specialists created from templates in one step."""

import json
import re
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException

from agentplatform.providers import ProviderResponse, ToolCall
from agentplatform.schema import AgentDefinition
from agentplatform.service import AgentService, SlugTaken
from agentplatform.templates import list_templates
from auth.dependencies import get_session_factory
from authz.dependencies import get_scope

STARTER = ("unit-economics-analyst", "launch-planner", "launch-comms")
SIMULATED_TOOL = "demo.send_note"
EXAMPLE_OBJECTIVE = ("We are launching a $49/month plan to our first customers. Check whether the unit economics work at an 80% gross margin, 3% monthly churn and a $400 CAC, "
                     "set a launch date 10 business days from today, and send the first customer (first.customer@example.com) a launch note.")
DEFAULTS = {"price": 49.0, "margin": 80.0, "churn": 3.0, "cac": 400.0}
RECIPIENT = "first.customer@example.com"
_PLAN_WORDS = ("launch", "pricing", "price", "unit economics", "first customer", "go live")


def _num(text: str) -> str:
    n = float(text)
    return str(int(n)) if n == int(n) else str(n)


def read_inputs(objective: str) -> dict:
    """The numbers the user wrote in their objective (price, margin, churn, CAC) and the recipient; documented defaults for the ones they did not."""
    low = objective.lower()
    found = dict(DEFAULTS)
    for key, pattern in (("price", r"\$\s*(\d+(?:\.\d+)?)\s*(?:/|per)\s*(?:month|mo)"), ("margin", r"(\d+(?:\.\d+)?)\s*%\s*(?:gross\s*)?margin"),
                         ("churn", r"(\d+(?:\.\d+)?)\s*%\s*(?:monthly\s*)?churn"), ("cac", r"\$\s*(\d+(?:\.\d+)?)\s*cac|cac\s*(?:of|is|=)?\s*\$?\s*(\d+(?:\.\d+)?)")):
        m = re.search(pattern, low)
        if m:
            found[key] = float(next(g for g in m.groups() if g))
    mail = re.search(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}", objective)
    found["recipient"] = mail.group(0) if mail else RECIPIENT
    return found


def _healthy(i: dict) -> bool:
    """The scripted planner's own read of the numbers (the same tool the specialist will call): the note is only REQUIRED to be sent when the economics are healthy; an unhealthy
    launch must not be announced, so there the task may legitimately end without sending."""
    try:
        from agentplatform.tools.builtin import UnitEconomicsIn, unit_economics

        return unit_economics(UnitEconomicsIn(monthly_price=i["price"], gross_margin_pct=i["margin"], monthly_churn_pct=i["churn"], cac=i["cac"])).assessment == "healthy"
    except Exception:  # noqa: BLE001 - unreadable numbers: do not require a send
        return False


def plan_json(objective: str, catalog_text: str) -> dict:
    missing = [slug for slug in STARTER if slug not in catalog_text]
    if not any(w in objective.lower() for w in _PLAN_WORDS):
        return {"feasible": False, "interpretation": "Demo mode understands one workflow: checking a launch price and telling the first customer.", "tasks": [],
                "unsupported": [{"requirement": "Planning an objective that is not a launch-readiness review", "reason": "The demo model is a script, not a live model. Connect a model key to plan anything else."}]}
    if missing:
        return {"feasible": False, "interpretation": "The launch workflow needs the starter workforce.", "tasks": [],
                "unsupported": [{"requirement": "The starter workforce: " + ", ".join(missing), "reason": "Add the starter workforce (one click on the overview), then delegate this objective again."}]}
    i = read_inputs(objective)
    today = datetime.now(timezone.utc).date().isoformat()
    inputs = f"Inputs: price={_num(str(i['price']))}; margin={_num(str(i['margin']))}; churn={_num(str(i['churn']))}; cac={_num(str(i['cac']))}."
    return {
        "feasible": True,
        "interpretation": "Check that the price is sustainable, fix a launch date by counting business days, then draft, check and (with your approval) send the first customer a launch note.",
        "tasks": [
            {"id": "economics", "title": "Check the unit economics", "description": f"Work out LTV, LTV:CAC and payback with the unit economics tool and give a verdict. {inputs}",
             "expected_output": "A verdict with the exact figures and the rule behind it.", "agent": "unit-economics-analyst", "rationale": "It computes unit economics exactly with a tool instead of estimating.",
             "depends_on": [], "complexity": "low", "priority": "high"},
            {"id": "dates", "title": "Set the launch date", "description": f"Count 10 business days from today with the date tool. start={today}; days=10.",
             "expected_output": "The launch date and its weekday.", "agent": "launch-planner", "rationale": "It counts business days exactly with a tool.",
             "depends_on": [], "complexity": "low", "priority": "high"},
            {"id": "announce", "title": "Tell the first customer", "description": f"Draft a short launch note from the date and the verdict, check its readability and send it. Recipient: {i['recipient']}.",
             "expected_output": "The note, its readability and whether it was sent.", "agent": "launch-comms", "rationale": "It checks readability with a tool and asks a person before sending.",
             "depends_on": ["economics", "dates"], "dependency_reason": "The note quotes the launch date and the verdict on the numbers.", "complexity": "low", "priority": "normal",
             "required_tool": "demo.send_note@1" if _healthy(i) else None},  # M6b: for a healthy launch the send is a REQUIRED action (the task is not done unless the simulated send was really invoked)
        ],
        "unsupported": [],
    }


def _tool_results(request) -> list:
    out = []
    for m in request.messages:
        if m.role == "tool":
            try:
                out.append(json.loads(m.content))
            except ValueError:
                out.append({})
    return out


def _call(name: str, args: dict, text: str) -> ProviderResponse:
    return ProviderResponse(text=text, model="demo-script", tool_calls=(ToolCall("c1", name, json.dumps(args)),))


def _say(text: str) -> ProviderResponse:
    return ProviderResponse(text=text, model="demo-script")


def agent_reply(request) -> ProviderResponse:
    task = request.messages[0].content if request.messages else ""
    system = request.system
    results = _tool_results(request)
    if "Unit economics analyst" in system:
        if not results:
            m = {k: re.search(rf"{k}=([\d.]+)", task) for k in ("price", "margin", "churn", "cac")}
            v = {k: float(m[k].group(1)) if m[k] else DEFAULTS[k] for k in m}
            return _call("finance__unit_economics__v1", {"monthly_price": v["price"], "gross_margin_pct": v["margin"], "monthly_churn_pct": v["churn"], "cac": v["cac"]},
                         "I will compute this with the unit economics tool instead of estimating.")
        r = results[-1]
        if "error" in r:
            return _say(f"I could not compute the unit economics ({r['error']}).")
        return _say(f"Unit economics: LTV ${r['ltv']:.0f}, LTV:CAC {r['ltv_to_cac']:.1f}x, CAC payback {r['cac_payback_months']:.1f} months, gross profit ${r['gross_profit_per_month']:.2f} per customer per month. "
                    f"Assessment: {r['assessment']}. Rule applied: {r['rule']} These assume constant churn and margin.")
    if "Launch planner" in system:
        if not results:
            start = re.search(r"start=(\d{4}-\d{2}-\d{2})", task)
            days = re.search(r"days=(\d+)", task)
            return _call("time__date_math__v1", {"operation": "add_business_days", "start": start.group(1) if start else datetime.now(timezone.utc).date().isoformat(),
                                                 "days": int(days.group(1)) if days else 10}, "I will count the business days with the date tool.")
        r = results[-1]
        if "error" in r:
            return _say(f"I could not set the date ({r['error']}).")
        return _say(f"Launch date: {r['result_date']} ({r['weekday']}). {r['explanation']}")
    if "Launch communications lead" in system:
        date = re.search(r"Launch date: (\d{4}-\d{2}-\d{2}) \((\w+)\)", task)
        verdict = re.search(r"LTV:CAC ([\d.]+)x", task)
        assess = re.search(r"Assessment: (\w+)", task)
        recipient = re.search(r"Recipient: (\S+?)\.?(?:\s|$)", task)
        when = f"{date.group(1)} ({date.group(2)})" if date else "the planned date"
        why = (f"We checked the numbers: LTV:CAC is {verdict.group(1)}x ({assess.group(1) if assess else 'checked'}), so the price holds." if verdict else "We checked the numbers behind the price.")
        draft = f"Hi, your plan goes live on {when}. {why} Reply to this note if you want to be among the first to start."
        if assess and assess.group(1) != "healthy" and len(results) == 0:
            # the workforce uses judgment on REAL numbers: it does not tell customers a price holds when the unit economics say otherwise, and it does not ask a person to approve that
            return _say(f"Not sent. The unit economics are {assess.group(1)} (LTV:CAC {verdict.group(1) + 'x' if verdict else 'unknown'}), so telling customers the price holds would not be honest. "
                        f"Review the price or the acquisition cost first. The planned launch date is {when}.")
        if len(results) == 0:
            return _call("text__analyze__v1", {"text": draft, "top_keywords": 3}, "I will check how easy the draft is to read before anything is sent.")
        if len(results) == 1:
            read = results[0]
            return _call("demo__send_note__v1", {"recipient": recipient.group(1) if recipient else RECIPIENT, "subject": f"Your plan launches {date.group(1) if date else 'soon'}", "body": draft},
                         f"The first customer should hear about the date and that the numbers hold up. The draft is {read.get('words', '?')} words and reads as {str(read.get('readability', 'unchecked')).replace('_', ' ')} (Flesch reading ease {read.get('flesch_reading_ease')}).")
        last = results[-1]
        if "error" in last:
            return _say(f"The note was NOT sent: {last.get('message', last['error'])} Draft: {draft}")
        return _say(f"Draft: {draft} Readability checked. The note was sent to {recipient.group(1) if recipient else RECIPIENT} (simulated: demo mode sends nothing anywhere). Receipt {last.get('receipt')}.")
    return _say("In demo mode this agent has no scripted behaviour. Connect a model key to use it.")


class DemoProvider:
    """The SCRIPTED model of demo mode. Deterministic, offline, and named as such in every response (``model='demo-script'``)."""

    name = "groq"  # it answers to the provider name the templates use, so agents need no special casing

    async def complete(self, request) -> ProviderResponse:
        if not request.system.startswith("You are an AI agent running inside AgentForge"):  # the planner
            objective = request.messages[-1].content if request.messages else ""
            return _say(json.dumps(plan_json(objective, request.system)))  # the objective is the user message; the agent catalog is in the system prompt
        return agent_reply(request)


def starter_definitions(demo_mode: bool) -> list:
    """(slug, definition, template) of the starter workforce. In demo mode the communications lead also holds the SIMULATED sending tool (every call needs approval)."""
    by_id = {t.id: t for t in list_templates()}
    out = []
    for slug in STARTER:
        template = by_id[slug]
        definition = template.definition
        if demo_mode and slug == "launch-comms":
            data = definition.model_dump()
            data["allowed_tools"] = [*data["allowed_tools"], SIMULATED_TOOL]
            data["capabilities"] = [*data["capabilities"], "send a note simulated with approval"]
            definition = AgentDefinition.model_validate(data)
        out.append((slug, definition, template))
    return out


def build_demo_router(demo_mode: bool) -> APIRouter:
    router = APIRouter()

    @router.get("/demo")
    async def demo_info(scope=Depends(get_scope)):
        from agentplatform.schema import DEFAULT_MODEL

        try:
            from config import get_settings

            model_name = None if demo_mode else get_settings().model_name
        except Exception:  # noqa: BLE001 - settings can be unavailable (tests); the default model is then the honest answer
            model_name = None if demo_mode else DEFAULT_MODEL

        # ``model_name`` is the configured default model (not a secret); ``live_tools`` are the tools that really reach a third party (M6), whichever mode runs
        return {"demo": demo_mode, "model": "scripted" if demo_mode else "live", "model_name": model_name, "live_tools": ["calendar.public_holidays"],
                "simulated_tools": [SIMULATED_TOOL] if demo_mode else [],
                "example_objective": EXAMPLE_OBJECTIVE, "starter": list(STARTER)}

    @router.post("/starter-workforce")
    async def starter_workforce(scope=Depends(get_scope), session_factory=Depends(get_session_factory)):
        """Create the starter specialists that do not exist yet (idempotent by slug). Never changes an existing agent."""
        import asyncio

        service = AgentService(session_factory)

        def create():
            existing = {a.slug for a in service.list_agents(scope, "active")}
            out = []
            for slug, definition, template in starter_definitions(demo_mode):
                made = slug not in existing
                if made:
                    try:
                        service.create(scope, slug, definition, change_note="Starter workforce", template_id=template.id, template_version=template.version)
                    except SlugTaken:
                        made = False
                out.append({"slug": slug, "name": definition.name, "role": definition.role, "tools": list(definition.allowed_tools), "created": made})
            return out

        try:
            return {"agents": await asyncio.to_thread(create)}
        except ValueError:
            raise HTTPException(status_code=409, detail={"code": "starter_unavailable"}) from None

    return router

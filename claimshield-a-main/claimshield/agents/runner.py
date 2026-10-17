"""Run one audit end to end, through ADK when available, otherwise with a plain Python loop
that executes the same steps in the same order (audit and benchmark concurrently)."""

from __future__ import annotations

import asyncio
import hashlib
import logging
import traceback
from typing import Any

from ..services import get_services
from ..trace import TRACES, Trace
from .steps import STEPS

log = logging.getLogger("claimshield.runner")

APP_NAME = "claimshield"


async def _run_with_adk(initial: dict[str, Any], trace: Trace) -> dict[str, Any]:
    from google.adk.runners import InMemoryRunner
    from google.genai import types

    from .adk_agents import root_agent

    runner = InMemoryRunner(agent=root_agent, app_name=APP_NAME)
    user_id = hashlib.sha256(f"{initial.get('tenant_id', 'local')}:{initial.get('owner_id', 'cli')}".encode()).hexdigest()
    session = await runner.session_service.create_session(app_name=APP_NAME, user_id=user_id, state=initial)
    trace.emit("orchestrator", "info", f"ADK {root_agent.name}: delegating to {' -> '.join(_names(root_agent))}")
    message = types.Content(role="user", parts=[types.Part(text=f"Audit case {initial['case_id']}")])
    async for event in runner.run_async(user_id=user_id, session_id=session.id, new_message=message):
        if event.actions and event.actions.state_delta:
            trace.emit("orchestrator", "info", f"ADK event from {event.author}: state updated ({', '.join(sorted(event.actions.state_delta))})")
    final = await runner.session_service.get_session(app_name=APP_NAME, user_id=user_id, session_id=session.id)
    return dict(final.state)


def _names(agent) -> list[str]:
    if not agent.sub_agents:
        return [agent.name]
    if type(agent).__name__ == "ParallelAgent":
        return ["(" + " || ".join(a.name for a in agent.sub_agents) + ")"]
    return [n for a in agent.sub_agents for n in _names(a)]


async def _run_plain(initial: dict[str, Any], trace: Trace) -> dict[str, Any]:
    svc = get_services()
    state = dict(initial)
    trace.emit("orchestrator", "info", "Plain runner: intake -> policy -> (audit || benchmark) -> reconciliation -> dispute")
    for name in ("intake_agent", "policy_agent"):
        state.update(await asyncio.to_thread(STEPS[name], state, trace, svc))
    audit, bench = await asyncio.gather(
        asyncio.to_thread(STEPS["audit_agent"], dict(state), trace, svc),
        asyncio.to_thread(STEPS["benchmark_agent"], dict(state), trace, svc),
    )
    state.update(audit)
    state.update(bench)
    for name in ("reconciliation_agent", "dispute_agent"):
        state.update(await asyncio.to_thread(STEPS[name], state, trace, svc))
    return state


async def run_audit(case_id: str, bill_key: str, policy_key: str, bill_name: str, policy_name: str,
                    *, owner_id: str = "local-cli", tenant_id: str = "local-cli",
                    analysis_case_id: str | None = None, claim_context: str | None = None) -> dict[str, Any]:
    svc = get_services()
    trace = TRACES.get(case_id)
    trace.emit("orchestrator", "start", "ClaimShield audit started", stack=svc.describe())
    for w in svc.warnings:
        trace.emit("orchestrator", "warn", w)
    initial = {"case_id": case_id, "bill_key": bill_key, "policy_key": policy_key, "bill_name": bill_name,
               "policy_name": policy_name, "owner_id": owner_id, "tenant_id": tenant_id,
               "analysis_case_id": analysis_case_id or case_id}
    if claim_context:
        initial['claim_context'] = claim_context
    try:
        use_adk = svc.settings.use_adk
        if use_adk:
            try:
                import google.adk  # noqa: F401
            except ImportError:
                trace.emit("orchestrator", "warn", "google-adk not installed; using the plain runner")
                use_adk = False
        state = await (_run_with_adk(initial, trace) if use_adk else _run_plain(initial, trace))
        state["status"] = "complete"
        trace.emit("orchestrator", "done", "Audit complete")
    except Exception as exc:  # noqa: BLE001 - report any failure to the UI instead of hanging the stream
        log.error("audit %s failed: %s", case_id, traceback.format_exc())
        state = {**initial, "status": "failed", "error": "Audit could not be completed. Please retry the upload."}
        trace.emit("orchestrator", "error", "Audit failed; diagnostic details were recorded in the server log.")
    finally:
        trace.finish()
    state["trace"] = trace.events
    state["stack"] = svc.describe()
    return state

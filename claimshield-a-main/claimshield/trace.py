"""Per-case agent trace. Every agent step writes here; the UI streams it over SSE and the
final case record keeps a copy. Each event is also logged as one JSON line so Cloud Run
ships it to Cloud Logging unchanged."""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from typing import Any, Literal

log = logging.getLogger("claimshield.trace")

# Local mode finishes in about a second, too fast to follow in the UI. CLAIMSHIELD_DEMO_PACE
# (seconds) adds a short pause after each tool/result event. Defaults to 0; cloud calls already
# take long enough on their own. Read per call so a .env loaded after import still applies.
def _pace() -> float:
    return float(os.getenv("CLAIMSHIELD_DEMO_PACE", "0") or 0)


Kind = Literal["start", "tool", "info", "result", "warn", "error", "done"]


class Trace:
    def __init__(self, case_id: str) -> None:
        self.case_id = case_id
        self.events: list[dict[str, Any]] = []
        self.finished = False
        self._lock = threading.Lock()
        self._t0 = time.time()

    def emit(self, agent: str, kind: Kind, message: str, **detail: Any) -> None:
        with self._lock:
            event = {
                "seq": len(self.events),
                "t": round(time.time() - self._t0, 3),
                "agent": agent,
                "kind": kind,
                "message": message,
            }
            if detail:
                event["detail"] = detail
            self.events.append(event)
        log.info(json.dumps({"case_id": self.case_id, **event}, default=str))
        if kind in ("tool", "result") and (pace := _pace()):
            time.sleep(pace)

    def since(self, cursor: int) -> list[dict[str, Any]]:
        with self._lock:
            return self.events[cursor:]

    def finish(self) -> None:
        self.finished = True


class TraceRegistry:
    """Live traces by case id. ADK session state must stay JSON-serialisable, so agents look
    up their trace here by the case_id held in state rather than carrying it in state."""

    def __init__(self) -> None:
        self._traces: dict[str, Trace] = {}
        self._lock = threading.Lock()

    def create(self, case_id: str) -> Trace:
        with self._lock:
            trace = Trace(case_id)
            self._traces[case_id] = trace
            return trace

    def discard(self, case_id: str) -> None:
        with self._lock:
            self._traces.pop(case_id, None)

    def get(self, case_id: str) -> Trace:
        with self._lock:
            trace = self._traces.get(case_id)
        return trace if trace is not None else self.create(case_id)


TRACES = TraceRegistry()

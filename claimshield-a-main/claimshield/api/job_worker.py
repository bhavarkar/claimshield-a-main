"""Queue worker mechanics shared by the HTTP application lifecycle.

The API module supplies the application-specific processor and service accessors. Keeping
polling, lease renewal, retries, and worker shutdown here leaves the route module focused on
HTTP while preserving the existing queue implementation and its single-host semantics.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable
from typing import Any


async def run_job_worker(
    queue: Any,
    process_job: Callable[[str, dict[str, Any]], Awaitable[None]],
    get_services: Callable[[], Any],
    traces: Any,
    logger: logging.Logger,
) -> None:
    """Poll and process jobs until cancelled by the application lifespan."""
    while True:
        try:
            job = await asyncio.to_thread(queue.claim)
        except Exception:
            logger.exception("Could not access claim queue; retrying")
            await asyncio.sleep(2)
            continue
        if not job:
            await asyncio.sleep(0.2)
            continue

        async def heartbeat() -> None:
            while True:
                await asyncio.sleep(30)
                await asyncio.to_thread(queue.renew, job)

        beat = asyncio.create_task(heartbeat())
        try:
            await process_job(job["id"], job["payload"])
            await asyncio.to_thread(queue.finish, job)
        except asyncio.CancelledError:
            # Leave the lease in place. A replacement worker recovers after expiry.
            raise
        except Exception:
            logger.exception("Claim job %s failed", job["id"])
            try:
                if job["attempts"] < 3:
                    await asyncio.to_thread(queue.finish, job, retry=True)
                else:
                    failed = {
                        "case_id": job["id"],
                        "status": "failed",
                        "error": "Processing could not finish after retries. Please retry your upload.",
                    }
                    await asyncio.to_thread(
                        get_services().storage.put,
                        f"cases/{job['id']}/case.json",
                        json.dumps(failed).encode(),
                        "application/json",
                    )
                    traces.discard(job["id"])
                    await asyncio.to_thread(queue.finish, job)
            except Exception:
                # An unavailable disk/storage must not permanently kill a worker.
                # Leave the lease for recovery when storage becomes available.
                logger.exception("Could not save claim job failure; lease retained")
        finally:
            beat.cancel()
            await asyncio.gather(beat, return_exceptions=True)

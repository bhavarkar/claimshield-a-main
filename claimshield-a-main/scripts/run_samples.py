"""Run the full agent pipeline on every synthetic bill and print a summary.

    python scripts/run_samples.py            # all bills
    python scripts/run_samples.py BILL-001   # one bill, with the full trace
"""

from __future__ import annotations

import asyncio
import json
import sys
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from claimshield.agents.runner import run_audit  # noqa: E402
from claimshield.money import inr  # noqa: E402
from claimshield.services import get_services  # noqa: E402


async def run_one(bill_id: str, verbose: bool) -> dict:
    svc = get_services()
    samples = svc.settings.data_dir / "samples"
    catalog = json.loads((samples / "catalog.json").read_text())
    bill = catalog["bills"][bill_id]
    policy = catalog["policies"][bill["policy"]]
    case_id = f"{bill_id.lower()}-{uuid.uuid4().hex[:6]}"
    bill_key, policy_key = f"cases/{case_id}/bill.pdf", f"cases/{case_id}/policy.pdf"
    svc.storage.put(bill_key, (samples / bill["file"]).read_bytes(), "application/pdf")
    svc.storage.put(policy_key, (samples / policy["file"]).read_bytes(), "application/pdf")
    state = await run_audit(case_id, bill_key, policy_key, Path(bill["file"]).name, Path(policy["file"]).name)
    if verbose:
        for e in state["trace"]:
            print(f"  {e['t']:6.2f}s  {e['agent']:<22} {e['kind']:<6} {e['message']}")
    return state


async def main(ids: list[str]) -> int:
    svc = get_services()
    catalog = json.loads((svc.settings.data_dir / "samples" / "catalog.json").read_text())
    ids = ids or list(catalog["bills"])
    print("Stack:", svc.describe())
    failed = False
    for bill_id in ids:
        state = await run_one(bill_id, verbose=len(ids) == 1)
        print(f"\n{bill_id}: {state['status']}" + (f" - {state.get('error')}" if state["status"] != "complete" else ""))
        if state["status"] != "complete":
            failed = True
            continue
        for f in state["findings"]:
            print(f"  {f['finding_id']:<4} {f['confidence']:<16} {inr(f['impact']):>10}  {f['type']:<18} lines={f['bill_lines']}  {f['title']}")
        r = state["reconciliation"]
        print(f"  paid {inr(r['patient_paid'])} -> should owe {inr(r['fair_patient_payable_high'])}; likely recovery "
              f"{inr(r['recovery_likely'])} (up to {inr(r['recovery_upper'])}); letters: {[l['recipient'] for l in state['letters']]}")
    return 1 if failed else 0


if __name__ == "__main__":
    # Redirected Windows consoles can default to cp1252, which cannot encode rupees.
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    raise SystemExit(asyncio.run(main(sys.argv[1:])))

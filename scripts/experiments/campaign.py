"""Run the M13 lab comparison: every case under every policy, three repetitions each.

    uv run python scripts/experiments/campaign.py [--policies fcfs,priority,backfill] [--reps 3]

For each policy the Scheduler is switched once (lab.py apply); then the repetitions run, each
going through every case. Before a run the lab must be free (no other users' LabFlux jobs, no
GPU in use outside LabFlux); otherwise the campaign waits and never cancels anyone's work.
Experiment accounts rotate through four sets, and a set is reused only after the fairness
score's usage window has passed, so one run's usage never leaks into the next. Finished runs
(their jobs.json exists) are skipped, so an interrupted campaign continues where it stopped.
Under priority the urgent case is also run with nothing preemptible (the 13.7 preemption
comparison without another restart).
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import traceback
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from analyze import print_run, summarize_run  # noqa: E402
from common import EVIDENCE  # noqa: E402
from lab import USAGE_WINDOW_S, USER_SETS, Lab  # noqa: E402
from replay import replay  # noqa: E402

STATE = EVIDENCE / "raw" / "campaign-state.json"
CASES = ("gpu", "contention", "users", "urgent")


def load_state() -> dict:
    if STATE.exists():
        return json.loads(STATE.read_text())
    return {"policy": None, "sets": {}}


def save_state(state: dict) -> None:
    STATE.parent.mkdir(parents=True, exist_ok=True)
    STATE.write_text(json.dumps(state, indent=1) + "\n")


def pick_set(state: dict) -> int:
    """The user set idle longest; waits until its usage has left the window."""
    sets = {int(k): v for k, v in state["sets"].items()}
    choice = min(range(1, USER_SETS + 1), key=lambda n: sets.get(n, 0))
    ready = sets.get(choice, 0) + USAGE_WINDOW_S + 15
    if time.time() < ready:
        print(f"waiting {ready - time.time():.0f} s for user set {choice} to age out", flush=True)
        time.sleep(ready - time.time())
    return choice


def wait_free(lab: Lab) -> None:
    while True:
        problems = lab.check()
        if not problems:
            return
        print("lab busy, waiting: " + "; ".join(problems), flush=True)
        time.sleep(30)


def runs(policies, reps, cases):
    for policy in policies:
        for rep in range(1, reps + 1):
            for case in cases:
                yield policy, case, rep, None
            if policy == "priority" and "urgent" in cases:
                yield policy, "urgent", rep, False


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawTextHelpFormatter
    )
    parser.add_argument("--policies", default="fcfs,priority,backfill")
    parser.add_argument("--reps", type=int, default=3)
    parser.add_argument("--cases", default=",".join(CASES))
    args = parser.parse_args()
    lab = Lab()
    state = load_state()
    policies = args.policies.split(",")
    cases = args.cases.split(",")
    for policy, case, rep, preemptible in runs(policies, args.reps, cases):
        variant = "-nopreempt" if preemptible is False else ""
        out = EVIDENCE / "raw" / case / f"{policy}{variant}" / f"rep{rep}"
        if (out / "jobs.json").exists():
            continue
        if state["policy"] != policy:
            wait_free(lab)
            lab.apply(policy)
            state["policy"] = policy
            save_state(state)
        user_set = pick_set(state)
        wait_free(lab)
        print(
            f"{time.strftime('%H:%M:%S')} start {case} {policy}{variant} rep{rep} set{user_set}",
            flush=True,
        )
        try:
            replay(lab, case, policy, rep, user_set, out, preemptible_override=preemptible)
        except Exception:
            traceback.print_exc()
            print(
                f"{time.strftime('%H:%M:%S')} FAILED {case} {policy}{variant} rep{rep}", flush=True
            )
            cancelled = lab.cancel_experiment_jobs()
            print(f"cancelled {cancelled} experiment jobs; stopping the campaign", flush=True)
            state["sets"][str(user_set)] = time.time()
            save_state(state)
            sys.exit(1)
        state["sets"][str(user_set)] = time.time()
        save_state(state)
        print(f"{time.strftime('%H:%M:%S')} done", end=" ", flush=True)
        print_run(summarize_run(out))
        time.sleep(5)
    print(f"{time.strftime('%H:%M:%S')} CAMPAIGN COMPLETE", flush=True)


if __name__ == "__main__":
    main()

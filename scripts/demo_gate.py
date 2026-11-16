"""Container workload for scene 3: wait for this run's queued job, then finish normally.

Uses only Python's standard library and the submitting user's Scheduler API access.
The scene runner supplies a fresh marker for every run, so earlier jobs cannot trigger it.
"""

import json
import os
import time
from urllib.error import HTTPError, URLError
from urllib.request import ProxyHandler, Request, build_opener


def main():
    request = Request(
        os.environ["LABFLUX_DEMO_URL"].rstrip("/") + "/api/jobs",
        headers={"Authorization": "Bearer " + os.environ["LABFLUX_DEMO_TOKEN"]},
    )
    marker = os.environ["LABFLUX_DEMO_GATE"]
    opener = build_opener(ProxyHandler({}))
    print("Waiting for the presenter to submit the queued jobs.", flush=True)
    while True:
        try:
            with opener.open(request, timeout=5) as response:
                jobs = json.load(response)
            if any(
                job.get("spec", {}).get("env", {}).get("LABFLUX_DEMO_SIGNAL") == marker
                for job in jobs
            ):
                break
        except HTTPError as exc:
            if exc.code in (401, 403):
                raise SystemExit("Demo workload could not authenticate to the Scheduler.") from None
        except (URLError, TimeoutError, OSError):
            pass  # The Worker's hard runtime limit also covers waiting for the Scheduler.
        time.sleep(0.25)
    print("Queued job detected; completing in 10 seconds.", flush=True)
    time.sleep(10)
    print("Demo workload completed normally.", flush=True)


if __name__ == "__main__":
    main()

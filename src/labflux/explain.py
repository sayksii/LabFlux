"""Explanations of scheduling decisions (M12): why a queued job cannot start now, and why a
dispatched job went to the node it did.

Pure functions over the facts the Scheduler decides with (online nodes, their allocations and
the held cards), so the queue view, the CLI and the Dashboard read the same explanation as the
decision itself instead of recomputing it.
"""

from __future__ import annotations

from labflux.models import MODEL_KIND
from labflux.policy import short
from labflux.session_protocol import SESSION_KIND

# The criteria the Scheduler picks a node by, in the order of its best-fit key (_schedule).
PLACEMENT_CRITERIA = (
    "it holds the project's files",
    "fewest spare matching GPUs",
    "least spare CPU",
    "smallest CPU capacity",
    "least spare RAM",
    "node name",
)


def who(job: dict) -> str:
    """The job as a decision text names it."""
    if job.get("kind") == MODEL_KIND:
        return f"model {job['service']}"
    if job.get("kind") == SESSION_KIND:
        return f"session {short(job['id'])}"
    return f"job {short(job['id'])}"


def gpu_fit(node: dict, request: dict, held: set[str]) -> dict:
    """The node's cards that match the request's model and VRAM filters, counted by why they are
    or are not free. `free` is what Scheduler._eligible_gpus would return: pooled, not held,
    reported by the latest heartbeat and not used outside LabFlux."""
    observed = {gpu["uuid"]: gpu for gpu in node["observed"].get("gpus", [])}
    model = (request["gpu_model"] or "").lower()
    fit = {"node": node["id"], "match": 0, "free": 0, "held": 0, "external": 0, "unreported": 0}
    for gpu in node["capacity"].get("gpus", []):
        if model not in gpu["name"].lower() or gpu["memory_total_mib"] < (
            request["min_vram_mib"] or 0
        ):
            continue
        fit["match"] += 1
        seen = observed.get(gpu["uuid"])
        if gpu["uuid"] in held:
            fit["held"] += 1
        elif seen is None:
            fit["unreported"] += 1
        elif seen["external_use"]:
            fit["external"] += 1
        else:
            fit["free"] += 1
    return fit


def blocker(
    request: dict,
    datasets: list[str],
    available: list[dict],
    allocated: dict[str, dict],
    held: set[str],
    max_jobs: int,
) -> dict | None:
    """Why the request cannot start on any available node now; None when it fits now.

    The checks of Scheduler._candidates against the same free capacity, one at a time: online
    nodes, datasets, GPUs, CPU, RAM, the job limit, and finally all of them on one node. Each
    of the last four looks only at the nodes that passed the dataset and GPU checks. Ties name
    the first node by id.
    """
    if not available:
        return {"kind": "no_worker"}
    nodes = sorted(
        (node for node in available if set(datasets).issubset(node["capacity"]["datasets"])),
        key=lambda node: node["id"],
    )
    if not nodes:
        return {"kind": "dataset", "datasets": list(datasets)}
    gpu_ok = {node["id"] for node in nodes}
    need = request["gpu"]
    if need:
        fits = [gpu_fit(node, request, held) for node in nodes]
        enough = [fit for fit in fits if fit["match"] >= need]
        if not any(fit["free"] >= need for fit in enough):
            # The node with the most free matching cards; none when no node has enough cards.
            best = max(enough, key=lambda fit: fit["free"], default=None)
            return {
                "kind": "gpu",
                "need": need,
                "model": request["gpu_model"],
                "min_vram_mib": request["min_vram_mib"],
                "best_node": best["node"] if best else None,
                **{
                    key: best[key] if best else 0
                    for key in ("free", "held", "external", "unreported")
                },
            }
        gpu_ok = {fit["node"] for fit in fits if fit["free"] >= need}
    # Of a GPU job, only the nodes with enough free matching cards: the CPU, RAM and job-limit
    # checks then never name a node that cannot run it.
    free = [
        {
            "node": node["id"],
            "cpu": node["capacity"]["cpu"] - allocated[node["id"]]["cpu"],
            "memory_mib": node["capacity"]["memory_mib"] - allocated[node["id"]]["memory_mib"],
            "jobs": allocated[node["id"]]["jobs"],
        }
        for node in nodes
        if node["id"] in gpu_ok
    ]
    cpu_ok = {item["node"] for item in free if request["cpu"] <= item["cpu"] + 1e-9}
    memory_ok = {item["node"] for item in free if request["memory_mib"] <= item["memory_mib"]}
    jobs_ok = {item["node"] for item in free if item["jobs"] < max_jobs}
    if not cpu_ok:
        best = max(free, key=lambda item: item["cpu"])
        return {
            "kind": "cpu",
            "need": request["cpu"],
            "best_free": max(0.0, best["cpu"]),
            "best_node": best["node"],
        }
    if not memory_ok:
        best = max(free, key=lambda item: item["memory_mib"])
        return {
            "kind": "memory",
            "need_mib": request["memory_mib"],
            "best_free_mib": max(0, best["memory_mib"]),
            "best_node": best["node"],
        }
    if not jobs_ok:
        return {"kind": "max_jobs", "best_node": min(free, key=lambda item: item["jobs"])["node"]}
    if not cpu_ok & memory_ok & jobs_ok & gpu_ok:
        return {"kind": "combination"}
    return None


def placement(ranked: list[tuple[tuple, str]], gpu: bool, project: bool) -> str:
    """Which nodes fit and why the chosen one won, for the dispatch note.

    ranked: (best-fit key, node id) of every candidate, best first. The reason lists the
    criteria up to the first one that tells the chosen node from the runner-up; the project
    criterion only when it decided, the GPU one only for a GPU job.
    """
    nodes = sorted(node_id for _, node_id in ranked)
    if len(nodes) == 1:
        return f"only {nodes[0]} fits"
    (best, chosen), (runner_up, _) = ranked[0], ranked[1]
    reasons = []
    for index, criterion in enumerate(PLACEMENT_CRITERIA):
        decides = best[index] != runner_up[index]
        if (index == 0 and not (project and decides)) or (index == 1 and not gpu):
            continue
        reasons.append(criterion)
        if decides:
            break
    return f"fits on {', '.join(nodes)}; chose {chosen}: {', then '.join(reasons)}"

import sys
from types import SimpleNamespace

import pytest
from conftest import register, submit, user_headers, worker_headers
from pydantic import ValidationError
from test_worker import ControlledExecutor

from labflux.config import Settings
from labflux.gpu import GpuProbe
from labflux.models import JobSpec
from labflux.worker import Worker, measure_capacity


def uuid(number):
    return f"GPU-{number:08x}-0000-0000-0000-000000000000"


class FakeNVMLError(Exception):
    pass


class FakeNVML:
    """The subset of pynvml that GpuProbe uses; handles are card indexes."""

    NVMLError = FakeNVMLError
    nvmlMemory_v2 = object()

    def __init__(self, cards, *, available=True):
        self.cards = cards
        self.available = available
        self.inits = self.shutdowns = 0

    def nvmlInit(self):
        if not self.available:
            raise FakeNVMLError("NVML Shared Library Not Found")
        self.inits += 1

    def nvmlShutdown(self):
        self.shutdowns += 1

    def nvmlSystemGetDriverVersion(self):
        return "535.309.01"

    def nvmlSystemGetCudaDriverVersion(self):
        return 12020

    def nvmlDeviceGetHandleByIndex(self, index):
        if not 0 <= index < len(self.cards):
            raise FakeNVMLError("Invalid Argument")
        return index

    def nvmlDeviceGetHandleByUUID(self, value):
        for index, card in enumerate(self.cards):
            if card["uuid"] == value:
                return index
        raise FakeNVMLError("Not Found")

    def nvmlDeviceGetUUID(self, handle):
        return self.cards[handle]["uuid"].encode()  # Older bindings return bytes.

    def nvmlDeviceGetIndex(self, handle):
        return handle

    def nvmlDeviceGetName(self, handle):
        return self.cards[handle]["name"]

    def nvmlDeviceGetMemoryInfo(self, handle, version=None):
        card = self.cards[handle]
        if card.get("lost"):
            raise FakeNVMLError("GPU is lost")
        return SimpleNamespace(total=card["total"] * 1024**2, used=card["used"] * 1024**2)

    def nvmlDeviceGetUtilizationRates(self, handle):
        return SimpleNamespace(gpu=self.cards[handle]["util"])

    def nvmlDeviceGetComputeRunningProcesses(self, handle):
        return [SimpleNamespace(pid=pid) for pid in self.cards[handle]["processes"]]


def card(number, name="NVIDIA GeForce RTX 3090", total=24576, used=12, util=0, processes=()):
    return {
        "uuid": uuid(number),
        "name": name,
        "total": total,
        "used": used,
        "util": util,
        "processes": list(processes),
    }


def kayle():
    return FakeNVML([card(0), card(1, "NVIDIA RTX A5000", 24564)])


def test_index_and_uuid_entries_resolve_to_pooled_devices(tmp_path):
    nvml = kayle()
    probe = GpuProbe([1, uuid(0)], 1024, nvml=nvml)
    assert [(d.index, d.uuid, d.name, d.memory_total_mib) for d in probe.devices] == [
        (0, uuid(0), "NVIDIA GeForce RTX 3090", 24576),
        (1, uuid(1), "NVIDIA RTX A5000", 24564),
    ]
    assert (probe.driver_version, probe.cuda_driver_version) == ("535.309.01", "12.2")
    capacity = measure_capacity(Settings(data_dir=tmp_path), probe)
    assert [gpu.uuid for gpu in capacity.gpus] == [uuid(0), uuid(1)]
    assert capacity.cuda_driver_version == "12.2"
    probe.close()
    probe.close()
    assert nvml.shutdowns == 1


@pytest.mark.parametrize("selectors", [[5], [uuid(9)], [0, uuid(0)], [1, 1]])
def test_unknown_or_repeated_cards_refuse_to_start(selectors):
    nvml = kayle()
    with pytest.raises(ValueError):
        GpuProbe(selectors, 1024, nvml=nvml)
    assert nvml.inits == nvml.shutdowns == 1


def test_configured_gpus_without_nvml_refuse_to_start():
    with pytest.raises(RuntimeError, match="NVML is unavailable"):
        GpuProbe([0], 1024, nvml=FakeNVML([], available=False))


def test_worker_without_pooled_gpus_never_loads_nvml(monkeypatch, tmp_path):
    monkeypatch.setitem(sys.modules, "pynvml", None)  # Any import attempt would fail.
    probe = GpuProbe([], 1024)
    assert probe.devices == [] and probe.observe(set()) == []
    worker = Worker(Settings(data_dir=tmp_path), executor=ControlledExecutor())
    try:
        assert worker.gpus.devices == []
        assert measure_capacity(worker.settings, worker.gpus).gpus == []
        assert worker.heartbeat_payload()["gpus"] == []
    finally:
        worker.client.close()
        worker.pool.shutdown()


def test_external_use_needs_a_process_or_the_memory_threshold():
    nvml = FakeNVML(
        [
            card(0, used=100, processes=[4242]),
            card(1, used=1024),
            card(2, used=1023, util=37),
            card(3, used=8000, processes=[777]),
        ]
    )
    probe = GpuProbe([0, 1, 2, 3], 1024, nvml=nvml)
    observed = probe.observe({uuid(3)})  # Card 3 belongs to this worker's own attempt.
    assert [o.external_use for o in observed] == [True, True, False, False]
    assert (observed[2].memory_used_mib, observed[2].utilization_percent) == (1023, 37)


def test_unreadable_card_is_left_out_of_the_heartbeat():
    nvml = kayle()
    probe = GpuProbe([0, 1], 1024, nvml=nvml)
    nvml.cards[1]["lost"] = True
    assert [o.uuid for o in probe.observe(set())] == [uuid(0)]


def test_worker_reports_its_own_cards_and_rejects_foreign_ones(client, tmp_path):
    nvml = kayle()
    probe = GpuProbe([0, 1], 1024, nvml=nvml)
    executor = ControlledExecutor()
    worker = Worker(
        Settings(data_dir=tmp_path / "worker", worker_id="vm-a"), executor=executor, gpus=probe
    )
    worker.instance_id = register(client, "vm-a", gpus=[d.model_dump() for d in probe.devices])
    worker.client.close()
    worker.client = client
    worker.client.headers["Authorization"] = "Bearer dev-worker-token"

    def beat():
        payload = worker.heartbeat_payload()
        response = client.post("/internal/workers/vm-a/heartbeat", json=payload)
        assert response.status_code == 200, response.text
        return {gpu["uuid"]: gpu["external_use"] for gpu in payload["gpus"]}

    try:
        assert beat() == {uuid(0): False, uuid(1): False}
        job = submit(client, resources={"gpu": 1})
        (command,) = client.app.state.scheduler.poll_commands("vm-a", worker.instance_id)
        held = command["payload"]["gpu_uuids"]
        assert held == [uuid(1)]  # The A5000 is the smaller card.
        worker.accept(command)
        nvml.cards[1].update(used=9000, processes=[31337])  # The job's own container.
        assert beat() == {uuid(0): False, uuid(1): False}
        executor.release.set()
        worker.handles[command["attempt_id"]]["future"].result(timeout=5)
        worker.flush()
        assert client.get(f"/api/jobs/{job['id']}", headers=user_headers()).json()["state"] == (
            "SUCCEEDED"
        )
        # Once the attempt has a result, a process left on the card is someone else's.
        assert beat() == {uuid(0): False, uuid(1): True}

        foreign = {
            **command,
            "payload": {**command["payload"], "attempt_id": "a" * 32, "gpu_uuids": [uuid(7)]},
        }
        with pytest.raises(ValueError, match="outside this worker's pool"):
            worker.accept(foreign)
        assert not (worker.root / ("a" * 32)).exists()
    finally:
        executor.release.set()
        worker.pool.shutdown(wait=True)


def test_nvidia_visible_devices_cannot_be_set_by_jobs(client):
    with pytest.raises(ValidationError, match="resources.gpu"):
        JobSpec(image="ubuntu", argv=["true"], env={"NVIDIA_VISIBLE_DEVICES": "all"})
    response = client.post(
        "/api/jobs",
        headers=user_headers(),
        json={"image": "ubuntu", "argv": ["true"], "env": {"NVIDIA_VISIBLE_DEVICES": "0"}},
    )
    assert response.status_code == 422
    assert client.get("/api/jobs", headers=worker_headers()).status_code == 401


@pytest.mark.parametrize("value", [[-1], ["0"], [True], [1.5], ["gpu-0"]])
def test_gpus_setting_accepts_only_indexes_and_uuids(value):
    with pytest.raises(ValidationError):
        Settings(gpus=value)
    assert Settings(gpus=[0, uuid(1)]).gpus == [0, uuid(1)]

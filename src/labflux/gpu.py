"""NVML probing for pooled GPUs; a worker without pooled GPUs never loads NVML."""

from __future__ import annotations

import logging
from collections.abc import Collection

from labflux.models import GpuDevice, GpuObservation

log = logging.getLogger("labflux.gpu")


def _text(value) -> str:
    return value.decode() if isinstance(value, bytes) else value


class GpuProbe:
    def __init__(self, selectors: list[int | str], external_memory_mib: int, nvml=None):
        self.external_memory_mib = external_memory_mib
        self.devices: list[GpuDevice] = []
        self.driver_version: str | None = None
        self.cuda_driver_version: str | None = None
        self.nvml = None
        self._handles: dict[str, object] = {}
        if not selectors:
            return
        if nvml is None:
            import pynvml as nvml
        try:
            nvml.nvmlInit()
        except nvml.NVMLError as exc:
            raise RuntimeError(f"gpus are configured but NVML is unavailable: {exc}") from exc
        # NVML stays initialised for the worker's lifetime; a sample costs about 1 ms of CPU.
        self.nvml = nvml
        try:
            for selector in selectors:
                self._add(selector)
            self.driver_version = _text(nvml.nvmlSystemGetDriverVersion())
            cuda = nvml.nvmlSystemGetCudaDriverVersion()
            self.cuda_driver_version = f"{cuda // 1000}.{cuda % 1000 // 10}"
        except BaseException:
            self.close()
            raise
        self.devices.sort(key=lambda device: device.index)

    def _add(self, selector: int | str) -> None:
        nvml = self.nvml
        try:
            if isinstance(selector, int):
                handle = nvml.nvmlDeviceGetHandleByIndex(selector)
            else:
                handle = nvml.nvmlDeviceGetHandleByUUID(selector)
        except nvml.NVMLError as exc:
            raise ValueError(f"configured GPU does not exist: {selector!r} ({exc})") from exc
        uuid = _text(nvml.nvmlDeviceGetUUID(handle))
        if uuid in self._handles:
            raise ValueError(f"GPU is listed more than once: {selector!r} is {uuid}")
        self._handles[uuid] = handle
        self.devices.append(
            GpuDevice(
                uuid=uuid,
                index=nvml.nvmlDeviceGetIndex(handle),
                name=_text(nvml.nvmlDeviceGetName(handle)),
                memory_total_mib=self._memory(handle).total // 1024**2,
            )
        )

    def _memory(self, handle):
        # v2 excludes driver-reserved memory from "used", matching nvidia-smi on idle cards.
        try:
            return self.nvml.nvmlDeviceGetMemoryInfo(handle, version=self.nvml.nvmlMemory_v2)
        except (AttributeError, TypeError, self.nvml.NVMLError):
            return self.nvml.nvmlDeviceGetMemoryInfo(handle)

    def observe(self, assigned: Collection[str]) -> list[GpuObservation]:
        """Sample each pooled card; cards held by this worker's unfinished attempts are not
        external. A card that cannot be read is left out, so the scheduler will not use it."""
        observations = []
        for device in self.devices:
            handle = self._handles[device.uuid]
            try:
                used = self._memory(handle).used // 1024**2
                utilization = self.nvml.nvmlDeviceGetUtilizationRates(handle).gpu
                processes = self.nvml.nvmlDeviceGetComputeRunningProcesses(handle)
            except self.nvml.NVMLError as exc:
                log.warning("GPU %s cannot be read: %s", device.uuid, exc)
                continue
            observations.append(
                GpuObservation(
                    uuid=device.uuid,
                    memory_used_mib=used,
                    utilization_percent=utilization,
                    external_use=device.uuid not in assigned
                    and (bool(processes) or used >= self.external_memory_mib),
                )
            )
        return observations

    def close(self) -> None:
        if self.nvml is not None:
            nvml, self.nvml = self.nvml, None
            try:
                nvml.nvmlShutdown()
            except nvml.NVMLError:
                pass

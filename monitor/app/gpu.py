"""Read the GPU's counters through NVML, the library nvidia-smi uses.

Under WSL2 NVML cannot tell which process holds which VRAM, so ``vram_used_mb``
counts everything on the card: Ollama's models, Visia's SigLIP, Lexia's Docling,
Vibia's CLAP, and the Windows desktop. Ollama's own share comes from its
``/api/ps`` instead.
"""

from __future__ import annotations

import logging
import threading
from typing import Any

logger = logging.getLogger(__name__)
MB = 1024 * 1024


class GpuReader:
    """One NVIDIA GPU's counters, or None for each one the driver does not report."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._nvml: Any = None
        self._handle: Any = None
        self.name: str | None = None
        self.problem: str | None = None

    def _open(self) -> bool:
        if self._handle is not None:
            return True
        try:
            import pynvml

            pynvml.nvmlInit()
            self._nvml = pynvml
            self._handle = pynvml.nvmlDeviceGetHandleByIndex(0)
            name = pynvml.nvmlDeviceGetName(self._handle)
            self.name = name.decode() if isinstance(name, bytes) else str(name)
            self.problem = None
            return True
        except Exception as error:  # noqa: BLE001 - no GPU is a state the page shows
            if self.problem is None:
                logger.warning("No GPU counters: %s", error)
            self.problem = f"No GPU counters: {error}"
            self._handle = None
            return False

    def _value(self, read: Any) -> Any:
        try:
            return read()
        except Exception:  # noqa: BLE001 - a counter this driver does not support
            return None

    def read(self) -> dict[str, Any] | None:
        with self._lock:
            if not self._open():
                return None
            nvml, handle = self._nvml, self._handle
            rates = self._value(lambda: nvml.nvmlDeviceGetUtilizationRates(handle))
            memory = self._value(lambda: nvml.nvmlDeviceGetMemoryInfo(handle))
            power = self._value(lambda: nvml.nvmlDeviceGetPowerUsage(handle))
            limit = self._value(lambda: nvml.nvmlDeviceGetEnforcedPowerLimit(handle))
            if rates is None and memory is None:
                # The driver went away (a WSL restart); open it again next time.
                self._handle = None
                return None
            return {
                "gpu_util": float(rates.gpu) if rates else None,
                "mem_util": float(rates.memory) if rates else None,
                "vram_used_mb": memory.used / MB if memory else None,
                "vram_total_mb": memory.total / MB if memory else None,
                "temp_c": self._value(lambda: float(nvml.nvmlDeviceGetTemperature(handle, nvml.NVML_TEMPERATURE_GPU))),
                "power_w": power / 1000 if power is not None else None,
                "power_limit_w": limit / 1000 if limit is not None else None,
                "sm_clock_mhz": self._value(lambda: float(nvml.nvmlDeviceGetClockInfo(handle, nvml.NVML_CLOCK_SM))),
                "mem_clock_mhz": self._value(lambda: float(nvml.nvmlDeviceGetClockInfo(handle, nvml.NVML_CLOCK_MEM))),
                "fan_pct": self._value(lambda: float(nvml.nvmlDeviceGetFanSpeed(handle))),
            }

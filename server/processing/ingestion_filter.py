"""Ingestion filter — drops noisy log entries before they reach the ring buffer.

Sits between the deduplicator and ring buffer in the processing pipeline.
Configs are immutable (frozen dataclass) so they can be swapped atomically
under the GIL without locks.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from server.models import LogEntry, LogLevel, LogSource

logger = logging.getLogger(__name__)

# Ordered LogLevel values for comparison
_LEVEL_ORDER = {level: idx for idx, level in enumerate(LogLevel)}


@dataclass(frozen=True)
class FilterConfig:
    """Immutable filter configuration. Atomic swap under GIL, no lock needed."""

    process: str | None = None
    processes: frozenset[str] = field(default_factory=frozenset)
    subsystems: frozenset[str] = field(default_factory=frozenset)
    exclude_processes: frozenset[str] = field(default_factory=frozenset)
    exclude_subsystems: frozenset[str] = field(default_factory=frozenset)
    exclude_messages: tuple[str, ...] = ()
    min_level: LogLevel | None = None

    def __post_init__(self) -> None:
        # Convert mutable inputs to frozen types
        if isinstance(self.processes, (list, set)):
            object.__setattr__(self, "processes", frozenset(self.processes))
        if isinstance(self.subsystems, (list, set)):
            object.__setattr__(self, "subsystems", frozenset(self.subsystems))
        if isinstance(self.exclude_processes, (list, set)):
            object.__setattr__(self, "exclude_processes", frozenset(self.exclude_processes))
        if isinstance(self.exclude_subsystems, (list, set)):
            object.__setattr__(self, "exclude_subsystems", frozenset(self.exclude_subsystems))
        if isinstance(self.exclude_messages, list):
            object.__setattr__(self, "exclude_messages", tuple(self.exclude_messages))

    def to_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {}
        if self.process is not None:
            result["process"] = self.process
        if self.processes:
            result["processes"] = sorted(self.processes)
        if self.subsystems:
            result["subsystems"] = sorted(self.subsystems)
        if self.exclude_processes:
            result["exclude_processes"] = sorted(self.exclude_processes)
        if self.exclude_subsystems:
            result["exclude_subsystems"] = sorted(self.exclude_subsystems)
        if self.exclude_messages:
            result["exclude_messages"] = list(self.exclude_messages)
        if self.min_level is not None:
            result["min_level"] = self.min_level.value
        return result


# ---------------------------------------------------------------------------
# Presets
# ---------------------------------------------------------------------------

PRESETS: dict[str, FilterConfig] = {
    "device-quiet": FilterConfig(
        exclude_subsystems=frozenset([
            "CoreBrightness",
            "ColourSensorFilterPlugin",
            "com.apple.CFNetwork",
            "com.apple.network",
        ]),
        exclude_processes=frozenset([
            "remotepairingdeviced",
            "symptomsd",
            "SymptomEvaluator",
            "bluetoothd",
            "wifid",
            "signpost_reporter",
            "kernel",
        ]),
    ),
    "simulator-quiet": FilterConfig(
        exclude_messages=("HangTracer",),
        exclude_subsystems=frozenset(["com.apple.CoreFoundation"]),
    ),
}


def build_config(preset: str | None = None, **overrides: Any) -> FilterConfig:
    """Build a FilterConfig from an optional preset with field overrides."""
    base_kwargs: dict[str, Any] = {}

    if preset:
        base = PRESETS.get(preset)
        if base is None:
            raise ValueError(f"Unknown preset: {preset!r}. Available: {sorted(PRESETS)}")
        # Start from preset defaults
        base_kwargs = {
            "process": base.process,
            "processes": base.processes,
            "subsystems": base.subsystems,
            "exclude_processes": base.exclude_processes,
            "exclude_subsystems": base.exclude_subsystems,
            "exclude_messages": base.exclude_messages,
            "min_level": base.min_level,
        }

    # Overlay explicit overrides (skip None values — they mean "not specified")
    for key, value in overrides.items():
        if value is not None:
            base_kwargs[key] = value

    return FilterConfig(**base_kwargs)


# ---------------------------------------------------------------------------
# IngestionFilter
# ---------------------------------------------------------------------------


class IngestionFilter:
    """Configurable filter between deduplicator and ring buffer.

    Supports three scopes: global, per-source, and per-device (most specific wins).
    """

    def __init__(self) -> None:
        self._global_config: FilterConfig = FilterConfig()
        self._source_configs: dict[LogSource, FilterConfig] = {}
        self._device_configs: dict[str, FilterConfig] = {}

    def _resolve_config(self, entry: LogEntry) -> FilterConfig:
        """Return the most specific config: device > source > global."""
        if entry.device_id and entry.device_id in self._device_configs:
            return self._device_configs[entry.device_id]
        if entry.source in self._source_configs:
            return self._source_configs[entry.source]
        return self._global_config

    def should_admit(self, entry: LogEntry) -> bool:
        """Return True if the entry should be stored in the ring buffer."""
        config = self._resolve_config(entry)

        # Empty config admits everything
        if config == FilterConfig():
            return True

        # 1. Check min_level
        if config.min_level is not None:
            if _LEVEL_ORDER[entry.level] < _LEVEL_ORDER[config.min_level]:
                return False

        # 2. Check excludes (OR — any match drops)
        if config.exclude_processes and entry.process in config.exclude_processes:
            return False
        if config.exclude_subsystems and entry.subsystem in config.exclude_subsystems:
            return False
        if config.exclude_messages:
            msg_lower = entry.message.lower()
            for pattern in config.exclude_messages:
                if pattern.lower() in msg_lower:
                    return False

        # 3. Check includes (AND — must match all specified includes)
        if config.process is not None and entry.process != config.process:
            return False
        if config.processes and entry.process not in config.processes:
            return False
        if config.subsystems and entry.subsystem not in config.subsystems:
            return False

        return True

    def update_filter(
        self,
        config: FilterConfig,
        source: LogSource | None = None,
        device_id: str | None = None,
    ) -> None:
        """Swap config at the appropriate scope (device > source > global)."""
        if device_id:
            self._device_configs[device_id] = config
        elif source:
            self._source_configs[source] = config
        else:
            self._global_config = config

    def get_config(
        self,
        source: LogSource | None = None,
        device_id: str | None = None,
    ) -> FilterConfig:
        """Return the config at the requested scope."""
        if device_id:
            return self._device_configs.get(device_id, FilterConfig())
        if source:
            return self._source_configs.get(source, FilterConfig())
        return self._global_config

    def get_all_configs(self) -> dict[str, Any]:
        """Serialized state for GET endpoint."""
        result: dict[str, Any] = {
            "global": self._global_config.to_dict(),
        }
        if self._source_configs:
            result["sources"] = {
                src.value: cfg.to_dict() for src, cfg in self._source_configs.items()
            }
        if self._device_configs:
            result["devices"] = {
                dev_id: cfg.to_dict() for dev_id, cfg in self._device_configs.items()
            }
        return result

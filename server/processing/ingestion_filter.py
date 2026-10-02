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
    #: Subsystem prefixes whose chatter is dropped and whose problems are
    #: kept: an entry from one of these below `quiet_below` is dropped. Not
    #: `exclude_subsystems`, which drops at every level -- Apple's network
    #: subsystems are most of a simulator's log volume, and also where a TLS
    #: trust failure is reported, which is what a proxy user most needs.
    quiet_subsystems: tuple[str, ...] = ()
    #: The level a quiet subsystem's entry must reach to be kept; `error`
    #: when unset.
    quiet_below: LogLevel | None = None

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
        # One canonical form -- a sorted tuple -- so two configs naming the
        # same prefixes compare equal however they were given. A bare string
        # is one prefix, never its characters.
        quiet = self.quiet_subsystems
        quiet = (quiet,) if isinstance(quiet, str) else tuple(quiet)
        if any(not p.strip() for p in quiet):
            # Every subsystem starts with "", so an empty prefix would quiet
            # the app's own lines too -- the opposite of what an exact-match
            # field like exclude_subsystems does with "" (review).
            raise ValueError("quiet_subsystems cannot contain an empty prefix: it would "
                             "match every subsystem, the app's own included")
        object.__setattr__(self, "quiet_subsystems", tuple(sorted(quiet)))
        if self.quiet_below is not None and not self.quiet_subsystems:
            # Accepted and doing nothing is the reading a caller cannot tell
            # from success (review).
            raise ValueError("quiet_below needs quiet_subsystems: on its own it quiets nothing")

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
        if self.quiet_subsystems:
            result["quiet_subsystems"] = list(self.quiet_subsystems)
            result["quiet_below"] = self.quiet_level.value
        if self.min_level is not None:
            result["min_level"] = self.min_level.value
        return result

    @property
    def quiet_level(self) -> LogLevel:
        """The level a quiet subsystem's entry must reach to be kept."""
        return self.quiet_below or LogLevel.ERROR


# ---------------------------------------------------------------------------
# Presets
# ---------------------------------------------------------------------------

PRESETS: dict[str, FilterConfig] = {
    "device-quiet": FilterConfig(
        # Sending libraries, matched by `sender` -- CoreBrightness logs some
        # lines with no os_log subsystem at all, so the library is the only
        # name they have.
        exclude_subsystems=frozenset([
            "CoreBrightness",
            "ColourSensorFilterPlugin",
        ]),
        # Apple's frameworks below error, as simulator-quiet. This replaces
        # excludes of com.apple.network and com.apple.CFNetwork, which never
        # matched a device line while device subsystems held library names
        # (Network, CFNetwork) -- and which, matching now, would drop every
        # level, hiding CFNetwork's TLS trust failures.
        quiet_subsystems=("com.apple.",),
        quiet_below=LogLevel.ERROR,
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
        # Apple's frameworks inside the app's own process, below error.
        # Measured on a 2.5-minute deep-link run of a real app: 85,570 lines,
        # 99.5% from com.apple.* -- network, defaults, CFBundle, CFNetwork --
        # at about 800 a second, peaking past 7,000, enough to overflow a
        # recording. Below error that is all chatter; at error it is the
        # TLS trust failure and the connection reset worth seeing, so those
        # stay. The app's own lines and third-party SDKs' are untouched.
        quiet_subsystems=("com.apple.",),
        quiet_below=LogLevel.ERROR,
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
            "quiet_subsystems": base.quiet_subsystems,
            "quiet_below": base.quiet_below,
        }

    # Overlay explicit overrides (skip None values — they mean "not specified")
    for key, value in overrides.items():
        if value is not None:
            base_kwargs[key] = value

    # Clearing a preset's rule (`quiet_subsystems=[]`) clears its level with
    # it, unless the caller named one: a level with nothing to apply to is
    # refused, and the preset's own should not make a clear fail.
    if not base_kwargs.get("quiet_subsystems") and overrides.get("quiet_below") is None:
        base_kwargs["quiet_below"] = None

    return FilterConfig(**base_kwargs)


# ---------------------------------------------------------------------------
# IngestionFilter
# ---------------------------------------------------------------------------


def _named(entry: LogEntry, names: frozenset[str]) -> bool:
    """Whether an entry's subsystem -- or its sending library, where the
    source reports one -- is among `names`. Both, because filters and presets
    written before device logs carried a real subsystem name the library
    ("CoreBrightness"), and some device lines have no subsystem at all: the
    library is all there is to match. A reverse-DNS subsystem and a library
    name do not collide."""
    return entry.subsystem in names or (bool(entry.sender) and entry.sender in names)


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
        if config.exclude_subsystems and _named(entry, config.exclude_subsystems):
            return False
        if config.exclude_messages:
            msg_lower = entry.message.lower()
            for pattern in config.exclude_messages:
                if pattern.lower() in msg_lower:
                    return False
        if (config.quiet_subsystems
                and entry.subsystem.startswith(config.quiet_subsystems)
                and _LEVEL_ORDER[entry.level] < _LEVEL_ORDER[config.quiet_level]):
            return False

        # 3. Check includes (AND — must match all specified includes)
        if config.process is not None and entry.process != config.process:
            return False
        if config.processes and entry.process not in config.processes:
            return False
        if config.subsystems and not _named(entry, config.subsystems):
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

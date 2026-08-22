from __future__ import annotations

import contextlib
import json
import os
import tempfile
from dataclasses import asdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any

DEFAULT_CONFIG_PATH = (
    Path(os.environ.get("XDG_CONFIG_HOME", str(Path.home() / ".config")))
    / "switchbot-cli"
    / "config.json"
)


class ConfigError(RuntimeError):
    """Raised when the config file exists but can't be read as valid JSON."""


@dataclass(frozen=True)
class DeviceConfig:
    address: str
    alias: str | None = None
    # Raw library set_position() values (0-100) that this CLI's position=100 (open)
    # and position=0 (closed) map to for this specific device. Defaults assume an
    # unflipped mount with full hardware travel; the setup wizard calibrates these.
    open_target: int = 100
    closed_target: int = 0
    # True if this device is chained/grouped to another and already moves when that
    # other device receives an open/close/position command. Bulk actions (no --address)
    # skip secondaries to avoid sending a redundant command; --address still targets
    # them directly.
    secondary: bool = False
    # Optional named group (e.g. "bedroom") used to scope --group/default-group actions.
    group: str | None = None


def _load_raw(config_path: Path) -> dict[str, Any]:
    if not config_path.exists():
        return {}
    try:
        data: dict[str, Any] = json.loads(config_path.read_text())
    except json.JSONDecodeError as exc:
        msg = f"{config_path} is not valid JSON ({exc}). Fix or delete it, then retry."
        raise ConfigError(msg) from exc
    return data


def _write_raw(data: dict[str, Any], config_path: Path) -> None:
    """Write `data` atomically: a crash mid-write can't leave a truncated config file."""
    config_path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=config_path.parent, suffix=".tmp")
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(data, f, indent=2)
        tmp_path.replace(config_path)
    except BaseException:
        with contextlib.suppress(FileNotFoundError):
            tmp_path.unlink()
        raise


def load_devices(config_path: Path = DEFAULT_CONFIG_PATH) -> list[DeviceConfig]:
    """Return all saved devices, de-duplicated by address (last entry wins)."""
    raw = _load_raw(config_path)
    by_address: dict[str, DeviceConfig] = {}
    for entry in raw.get("devices", []):
        dev = DeviceConfig(
            address=entry["address"],
            alias=entry.get("alias"),
            open_target=int(entry.get("open_target", 100)),
            closed_target=int(entry.get("closed_target", 0)),
            secondary=bool(entry.get("secondary", False)),
            group=entry.get("group"),
        )
        by_address[dev.address] = dev
    return list(by_address.values())


def save_device(  # noqa: PLR0913 -- one field per DeviceConfig attribute, all genuinely independent
    address: str,
    *,
    alias: str | None = None,
    open_target: int = 100,
    closed_target: int = 0,
    secondary: bool = False,
    group: str | None = None,
    config_path: Path = DEFAULT_CONFIG_PATH,
) -> None:
    """Add a device, or update its settings if already saved (upsert by address)."""
    raw = _load_raw(config_path)
    devices = {d.address: d for d in load_devices(config_path)}
    devices[address] = DeviceConfig(
        address=address,
        alias=alias,
        open_target=open_target,
        closed_target=closed_target,
        secondary=secondary,
        group=group,
    )
    raw["devices"] = [asdict(d) for d in devices.values()]
    _write_raw(raw, config_path)


def find_device(identifier: str, devices: list[DeviceConfig]) -> DeviceConfig | None:
    """Find a saved device by exact address or case-insensitive alias."""
    for dev in devices:
        if dev.address == identifier:
            return dev
        if dev.alias is not None and dev.alias.casefold() == identifier.casefold():
            return dev
    return None


def load_default_group(config_path: Path = DEFAULT_CONFIG_PATH) -> str | None:
    """Return the configured default group, if any."""
    value = _load_raw(config_path).get("default_group")
    return value if isinstance(value, str) else None


def save_default_group(group: str | None, config_path: Path = DEFAULT_CONFIG_PATH) -> None:
    """Set the default group, or clear it if `group` is None."""
    raw = _load_raw(config_path)
    if group is None:
        raw.pop("default_group", None)
    else:
        raw["default_group"] = group
    _write_raw(raw, config_path)

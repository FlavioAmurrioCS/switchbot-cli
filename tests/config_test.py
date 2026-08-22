from __future__ import annotations

from typing import TYPE_CHECKING

from switchbot_cli import config

if TYPE_CHECKING:
    from pathlib import Path


def test_load_devices_missing_file_returns_empty(tmp_path: Path) -> None:
    assert config.load_devices(tmp_path / "missing.json") == []


def test_save_then_load_device_round_trips(tmp_path: Path) -> None:
    config_path = tmp_path / "config.json"
    config.save_device(
        "AA:BB:CC:DD:EE:FF",
        alias="left",
        open_target=0,
        closed_target=88,
        config_path=config_path,
    )
    devices = config.load_devices(config_path)
    assert devices == [
        config.DeviceConfig(
            address="AA:BB:CC:DD:EE:FF", alias="left", open_target=0, closed_target=88
        )
    ]


def test_save_device_creates_parent_directories(tmp_path: Path) -> None:
    config_path = tmp_path / "nested" / "dir" / "config.json"
    config.save_device("11:22:33:44:55:66", config_path=config_path)
    assert config.load_devices(config_path) == [config.DeviceConfig(address="11:22:33:44:55:66")]


def test_save_device_upserts_by_address(tmp_path: Path) -> None:
    config_path = tmp_path / "config.json"
    config.save_device("AA:BB:CC:DD:EE:FF", config_path=config_path)
    config.save_device("11:22:33:44:55:66", config_path=config_path)
    config.save_device("AA:BB:CC:DD:EE:FF", alias="left", config_path=config_path)

    devices = {d.address: d for d in config.load_devices(config_path)}
    assert devices["AA:BB:CC:DD:EE:FF"].alias == "left"
    assert devices["11:22:33:44:55:66"].alias is None
    assert set(devices) == {"AA:BB:CC:DD:EE:FF", "11:22:33:44:55:66"}


def test_find_device_by_address() -> None:
    devices = [config.DeviceConfig(address="AA:BB:CC:DD:EE:FF", alias="left")]
    assert config.find_device("AA:BB:CC:DD:EE:FF", devices) is devices[0]


def test_find_device_by_alias_case_insensitive() -> None:
    devices = [config.DeviceConfig(address="AA:BB:CC:DD:EE:FF", alias="Left")]
    assert config.find_device("left", devices) is devices[0]


def test_find_device_no_match_returns_none() -> None:
    devices = [config.DeviceConfig(address="AA:BB:CC:DD:EE:FF", alias="left")]
    assert config.find_device("right", devices) is None

from __future__ import annotations

import asyncio
import contextlib
from dataclasses import dataclass
from typing import TYPE_CHECKING
from typing import cast

import bleak
import bleak.exc
from switchbot import GetSwitchbotDevices  # pyright: ignore[reportMissingTypeStubs]
from switchbot import SwitchbotCurtain  # pyright: ignore[reportMissingTypeStubs]
from switchbot.adv_parser import parse_advertisement_data  # pyright: ignore[reportMissingTypeStubs]
from switchbot.devices.device import (  # pyright: ignore[reportMissingTypeStubs]
    CharacteristicMissingError,
)
from switchbot.devices.device import (  # pyright: ignore[reportMissingTypeStubs]
    SwitchbotOperationError,
)
from switchbot.discovery import CONNECT_LOCK  # pyright: ignore[reportMissingTypeStubs]

if TYPE_CHECKING:
    from collections.abc import Iterable

    from bleak.backends.device import BLEDevice
    from bleak.backends.scanner import AdvertisementData
    from switchbot.models import SwitchBotAdvertisement  # pyright: ignore[reportMissingTypeStubs]


class CurtainError(RuntimeError):
    """Base class for errors this CLI expects during normal BLE operation."""


class CurtainNotFoundError(CurtainError):
    """Raised when no SwitchBot Curtain can be found nearby over BLE."""


class CurtainOperationError(CurtainError):
    """Raised when a BLE command to an already-found Curtain fails.

    Covers connection drops, GATT protocol errors, and unexpected device responses --
    distinct from CurtainNotFoundError, which means the device was never even seen.
    """


# Exceptions pySwitchbot/bleak raise for a live BLE operation gone wrong (connection
# dropped, GATT error, device rejected the command, etc.), as opposed to a bug in our
# own code. Deliberately narrower than bleak_retry_connector.BLEAK_RETRY_EXCEPTIONS,
# which also includes bare AttributeError -- too broad to swallow safely here.
_OPERATION_EXCEPTIONS = (
    bleak.exc.BleakError,
    SwitchbotOperationError,
    CharacteristicMissingError,
    TimeoutError,
    EOFError,
    BrokenPipeError,
)


async def _set_position(curtain: SwitchbotCurtain, raw_value: int) -> None:
    """Command `curtain` to a raw position, wrapping BLE failures as CurtainOperationError."""
    try:
        await curtain.set_position(raw_value)
    except _OPERATION_EXCEPTIONS as exc:
        msg = f"{curtain.name}: BLE operation failed ({exc})"
        raise CurtainOperationError(msg) from exc


@dataclass(frozen=True)
class CurtainStatus:
    position: int
    battery: int
    is_opening: bool
    is_closing: bool
    is_calibrated: bool


# WoCurtain/Curtain model codes used in BLE advertisement data; see
# switchbot.discovery.GetSwitchbotDevices.get_curtains().
_CURTAIN_MODEL_CODES = frozenset({"c", "C", "{", "["})

# pySwitchbot's own retry logic in GetSwitchbotDevices.discover() never actually
# retries (it only retries when the scanner itself is None, which can't happen),
# so a single scan is all we get. Use a longer window than the library's 5s
# default to make catching the curtain's advertisement more reliable.
_DISCOVERY_SCAN_TIMEOUT = 10


def _to_raw(position: int, open_target: int, closed_target: int) -> int:
    """Map a CLI position (0=closed..100=open) to the raw library position to send."""
    raw = closed_target + (open_target - closed_target) * position / 100
    return round(max(0, min(100, raw)))


def _from_raw(raw: int, open_target: int, closed_target: int) -> int:
    """Map a raw library position back to a CLI position (0=closed..100=open)."""
    span = open_target - closed_target
    if span == 0:
        return 0
    cli = (raw - closed_target) / span * 100
    return round(max(0, min(100, cli)))


async def _scan(scan_timeout: int = _DISCOVERY_SCAN_TIMEOUT) -> dict[str, SwitchBotAdvertisement]:
    return cast(
        "dict[str, SwitchBotAdvertisement]",
        await GetSwitchbotDevices().discover(  # pyright: ignore[reportUnknownMemberType]
            scan_timeout=scan_timeout
        ),
    )


async def discover_curtains(
    scan_timeout: int = _DISCOVERY_SCAN_TIMEOUT,
) -> dict[str, SwitchBotAdvertisement]:
    """Scan nearby BLE advertisements and return discovered Curtains keyed by address."""
    devices = await _scan(scan_timeout)
    return {
        address: adv
        for address, adv in devices.items()
        if adv.data.get("model") in _CURTAIN_MODEL_CODES
    }


async def scan_for_addresses(
    addresses: Iterable[str], scan_timeout: int = _DISCOVERY_SCAN_TIMEOUT
) -> dict[str, SwitchBotAdvertisement]:
    """Scan for specific BLE addresses, stopping as soon as all of them have been seen.

    Faster than discover_curtains() when the target addresses are already known (e.g.
    from saved config): GetSwitchbotDevices.discover() always sleeps the full
    scan_timeout regardless of what it's found, but a Curtain typically advertises
    every second or so, so waiting for exactly the addresses we want usually finishes
    well before the timeout instead of always waiting the full window.
    """
    wanted = set(addresses)
    found: dict[str, SwitchBotAdvertisement] = {}
    all_seen = asyncio.Event()

    def _on_detect(ble_device: BLEDevice, advertisement_data: AdvertisementData) -> None:
        discovery = parse_advertisement_data(ble_device, advertisement_data)
        if discovery is not None:
            found[discovery.address] = discovery
            if wanted <= found.keys():
                all_seen.set()

    scanner = bleak.BleakScanner(detection_callback=_on_detect)
    async with CONNECT_LOCK:
        await scanner.start()
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(all_seen.wait(), timeout=scan_timeout)
        await scanner.stop()
    return found


async def _find_curtain_advertisement(
    address: str, scan_timeout: int = _DISCOVERY_SCAN_TIMEOUT
) -> SwitchBotAdvertisement:
    devices = await _scan(scan_timeout)
    advertisement = devices.get(address)
    if advertisement is None:
        msg = f"No SwitchBot Curtain found nearby with address {address!r}"
        raise CurtainNotFoundError(msg)
    return advertisement


async def get_curtain(
    address: str, advertisements: dict[str, SwitchBotAdvertisement] | None = None
) -> SwitchbotCurtain:
    """Resolve a BLE advertisement for `address` and construct a SwitchbotCurtain for it.

    Seeds the curtain's cached state from the advertisement itself: SwitchBot Curtains
    broadcast position/battery/motion/calibration passively, so this data is available
    immediately without an active GATT connection (which set_position() still needs in
    order to actually move the curtain, and which is less reliable).

    Pass `advertisements` (e.g. from a shared discover_curtains() call) to resolve against
    an already-completed scan instead of triggering a fresh one -- pySwitchbot serializes
    scans via a module-level lock, so scanning once for several devices avoids each one
    blocking behind the others' scan window.
    """
    if advertisements is not None:
        advertisement = advertisements.get(address)
        if advertisement is None:
            msg = f"No SwitchBot Curtain found nearby with address {address!r}"
            raise CurtainNotFoundError(msg)
    else:
        advertisement = await _find_curtain_advertisement(address)
    curtain = SwitchbotCurtain(device=advertisement.device)
    curtain.update_from_advertisement(advertisement)
    return curtain


async def _move_to_raw(
    address: str,
    raw_value: int,
    advertisements: dict[str, SwitchBotAdvertisement] | None = None,
) -> None:
    curtain = await get_curtain(address, advertisements)
    await _set_position(curtain, raw_value)


async def open_curtain(
    address: str,
    *,
    open_target: int = 100,
    advertisements: dict[str, SwitchBotAdvertisement] | None = None,
) -> None:
    await _move_to_raw(address, open_target, advertisements)


async def close_curtain(
    address: str,
    *,
    closed_target: int = 0,
    advertisements: dict[str, SwitchBotAdvertisement] | None = None,
) -> None:
    await _move_to_raw(address, closed_target, advertisements)


async def set_curtain_position(
    address: str,
    position: int,
    *,
    open_target: int = 100,
    closed_target: int = 0,
    advertisements: dict[str, SwitchBotAdvertisement] | None = None,
) -> None:
    """Move to `position` (0=closed, 100=open), scaled to this device's calibrated endpoints."""
    await _move_to_raw(address, _to_raw(position, open_target, closed_target), advertisements)


async def get_curtain_status(
    address: str,
    *,
    open_target: int = 100,
    closed_target: int = 0,
    advertisements: dict[str, SwitchBotAdvertisement] | None = None,
) -> CurtainStatus:
    """Return the curtain's last-broadcast status from its BLE advertisement.

    This reads passively-broadcast data (see get_curtain()) rather than connecting,
    so it reflects the state as of the most recent advertisement, not a live poll.
    """
    curtain = await get_curtain(address, advertisements)
    raw_position = cast("int", curtain.get_position())
    reversed_direction = open_target < closed_target
    return CurtainStatus(
        position=_from_raw(raw_position, open_target, closed_target),
        battery=cast("int", curtain.get_battery_percent()),
        is_opening=curtain.is_closing() if reversed_direction else curtain.is_opening(),
        is_closing=curtain.is_opening() if reversed_direction else curtain.is_closing(),
        is_calibrated=bool(curtain.is_calibrated()),
    )


async def get_raw_position(address: str) -> int:
    """Return the curtain's raw library-reported position (0-100), ignoring calibration.

    Used by the setup wizard, which works directly in raw units while it determines
    this device's open_target/closed_target.
    """
    curtain = await get_curtain(address)
    return cast("int", curtain.get_position())


async def set_raw_position(address: str, value: int) -> None:
    """Command the curtain to a raw library position (0-100), bypassing calibration.

    Used by the setup wizard.
    """
    await _move_to_raw(address, max(0, min(100, value)))

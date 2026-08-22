from __future__ import annotations

import asyncio
import functools
from typing import TYPE_CHECKING
from typing import Annotated
from typing import ParamSpec
from typing import TypeVar

import typer
from rich.console import Console
from rich.progress import Progress
from rich.progress import SpinnerColumn
from rich.progress import TextColumn

from switchbot_cli import config
from switchbot_cli import device

if TYPE_CHECKING:
    from collections.abc import Callable
    from collections.abc import Coroutine

P = ParamSpec("P")
T = TypeVar("T")

_DIRECTION_NUDGE = 20
_FINE_TUNE_STEP = 4
_POSITION_MIDPOINT = 50
_MIN_ENDPOINT_GAP = 5
_SCAN_MESSAGE = "Scanning for nearby SwitchBot Curtains…"

app = typer.Typer(no_args_is_help=True, add_completion=False)
config_app = typer.Typer(no_args_is_help=True, help="Manage saved devices.")
app.add_typer(config_app, name="config")

console = Console()
err_console = Console(stderr=True)

AddressOption = Annotated[
    str | None,
    typer.Option(
        "--address", help="Target only this device (address or alias). Overrides --group/--all."
    ),
]
GroupOption = Annotated[
    str | None,
    typer.Option("--group", help="Target only devices in this group."),
]
AllOption = Annotated[
    bool,
    typer.Option("--all", help="Target every saved device, ignoring any default group."),
]


def run_async(func: Callable[P, Coroutine[object, object, T]]) -> Callable[P, T]:
    """Adapt an async command function to Typer/Click's synchronous calling convention.

    `functools.wraps` sets `__wrapped__`, which `inspect.signature` follows by default,
    so Typer still introspects `func`'s real parameters/annotations for building the CLI.
    """

    @functools.wraps(func)
    def wrapper(*args: P.args, **kwargs: P.kwargs) -> T:
        return asyncio.run(func(*args, **kwargs))

    return wrapper


def _label(dev: config.DeviceConfig) -> str:
    return f"{dev.alias} ({dev.address})" if dev.alias else dev.address


def _role(dev: config.DeviceConfig) -> str:
    return "secondary" if dev.secondary else "primary"


def _resolve_devices(
    address: str | None, group: str | None = None, *, all_: bool = False
) -> list[config.DeviceConfig]:
    """Resolve the devices selected by --address / --group / --all / the default group.

    Precedence: --address (exact device) > --all (everything) > --group (that group) >
    the configured default group, if any > everything (when no default group is set).
    """
    devices = config.load_devices()
    if address is not None:
        found = config.find_device(address, devices)
        if found is not None:
            return [found]
        if devices:
            console.print(
                f"[yellow]{address!r} doesn't match a saved device or alias -- treating "
                "it as a raw BLE address.[/yellow]"
            )
        return [config.DeviceConfig(address=address)]

    if all_ and group is not None:
        err_console.print("--all and --group are mutually exclusive.")
        raise typer.Exit(code=1)

    target_group = None if all_ else (group if group is not None else config.load_default_group())

    if target_group is not None:
        selected = [d for d in devices if d.group == target_group]
        if not selected:
            err_console.print(f"No saved devices in group {target_group!r}.")
            raise typer.Exit(code=1)
        return selected

    if not devices:
        err_console.print(
            "No saved devices and no --address given. Run 'switchbot-cli discover' first."
        )
        raise typer.Exit(code=1)
    return devices


def _select_action_targets(
    devices: list[config.DeviceConfig], *, address: str | None
) -> tuple[list[config.DeviceConfig], list[config.DeviceConfig]]:
    """Split resolved devices into (targets, skipped-secondaries) for open/close/position.

    Skips devices marked `secondary` (already driven by another device's chain command)
    unless an explicit --address was given, which always targets that device directly.
    """
    if address is not None:
        return devices, []
    primaries = [d for d in devices if not d.secondary]
    if primaries:
        return primaries, [d for d in devices if d.secondary]
    return devices, []


def _note_skipped_secondaries(skipped: list[config.DeviceConfig]) -> None:
    """Print a note about secondaries a bulk action isn't messaging directly.

    Without this, seeing only the primary's result (e.g. just "right: opened") could
    read as "left didn't move" when it actually does, driven by the primary's command.
    """
    if skipped:
        names = ", ".join(_label(d) for d in skipped)
        console.print(
            f"[dim]Not messaging {names} directly -- it moves automatically as a "
            f"chained secondary.[/dim]"
        )


async def _try(coro: Coroutine[object, object, T]) -> T | device.CurtainError:
    try:
        return await coro
    except device.CurtainError as exc:
        return exc


def _failure_description(dev: config.DeviceConfig, result: device.CurtainError) -> str:
    if isinstance(result, device.CurtainNotFoundError):
        return f"[red]{_label(dev)}: not found nearby[/red]"
    return f"[red]{_label(dev)}: BLE operation failed[/red]"


async def _dispatch(
    devices: list[config.DeviceConfig],
    verb: str,
    done: str,
    make_coro: Callable[[config.DeviceConfig], Coroutine[object, object, T]],
    *,
    transient: bool = False,
) -> list[tuple[config.DeviceConfig, T | device.CurtainError]]:
    """Run `make_coro` for every device concurrently, with a live spinner line per device.

    `verb`/`done` are kept short (e.g. "opening"/"opened") since Progress truncates rather
    than wraps a task's line -- print any fuller per-device detail separately afterward.
    Pass `transient=True` when the caller prints its own more detailed result lines
    afterward, so the "done" row disappears instead of duplicating that detail.
    """
    with Progress(
        SpinnerColumn(finished_text=" "),
        TextColumn("[progress.description]{task.description}"),
        console=console,
        transient=transient,
    ) as progress:
        task_ids = {
            dev.address: progress.add_task(f"{_label(dev)}: {verb}…", total=1) for dev in devices
        }

        async def _one(dev: config.DeviceConfig) -> T | device.CurtainError:
            result = await _try(make_coro(dev))
            task_id = task_ids[dev.address]
            if isinstance(result, device.CurtainError):
                description = _failure_description(dev, result)
            else:
                description = f"[green]{_label(dev)}: {done}[/green]"
            progress.update(task_id, description=description, completed=1)
            return result

        results = await asyncio.gather(*(_one(dev) for dev in devices))
    return list(zip(devices, results, strict=True))


@app.command()
@run_async
async def discover() -> None:
    """Scan nearby BLE devices for SwitchBot Curtains and save them.

    With no --address/--group, other commands target every saved device together --
    handy if you have more than one, e.g. a pair covering one window.
    """
    with console.status(_SCAN_MESSAGE):
        curtains = await device.discover_curtains()
    if not curtains:
        console.print("No SwitchBot Curtains found nearby.")
        raise typer.Exit(code=1)

    addresses = list(curtains)
    for index, address in enumerate(addresses):
        console.print(f"[{index}] {address}  rssi={curtains[address].rssi}")

    if not typer.confirm(f"Save all {len(addresses)} device(s)?", default=True):
        return

    existing = {d.address: d for d in config.load_devices()}
    for address in addresses:
        if address in existing:
            config.save_device(
                address,
                alias=existing[address].alias,
                open_target=existing[address].open_target,
                closed_target=existing[address].closed_target,
                secondary=existing[address].secondary,
                group=existing[address].group,
            )
            continue
        alias = typer.prompt(
            f"Alias for {address} (e.g. left/right, optional)", default="", show_default=False
        )
        group = typer.prompt(
            f"Group for {address} (e.g. bedroom, optional)", default="", show_default=False
        )
        config.save_device(address, alias=alias or None, group=group or None)
    console.print(
        f"Saved {len(addresses)} device(s). Run 'switchbot-cli wizard' to calibrate direction "
        "and stopping points."
    )


@app.command()
@run_async
async def status(
    address: AddressOption = None, group: GroupOption = None, *, all_: AllOption = False
) -> None:
    """Show position, battery, and motion state for one or all saved devices.

    Devices are scanned once and then queried concurrently, rather than one at a time.
    """
    devices = _resolve_devices(address, group, all_=all_)
    with console.status(_SCAN_MESSAGE):
        advertisements = await device.scan_for_addresses(d.address for d in devices)
    pairs = await _dispatch(
        devices,
        "checking status",
        "done",
        lambda dev: device.get_curtain_status(
            dev.address,
            open_target=dev.open_target,
            closed_target=dev.closed_target,
            advertisements=advertisements,
        ),
        transient=True,
    )
    had_error = False
    for dev, result in pairs:
        if isinstance(result, device.CurtainError):
            had_error = True
            err_console.print(f"[red]  {result}[/red]")
            continue
        console.print(
            f"  {_label(dev)}: position={result.position}% battery={result.battery}% "
            f"opening={result.is_opening} closing={result.is_closing} "
            f"calibrated={result.is_calibrated}"
        )
    if had_error:
        raise typer.Exit(code=1)


@app.command(name="open")
@run_async
async def open_command(
    address: AddressOption = None, group: GroupOption = None, *, all_: AllOption = False
) -> None:
    """Open one or all saved devices (skips chained secondaries when targeting all).

    Devices are scanned once and then commanded concurrently, rather than one at a time.
    """
    resolved = _resolve_devices(address, group, all_=all_)
    devices, skipped = _select_action_targets(resolved, address=address)
    _note_skipped_secondaries(skipped)
    with console.status(_SCAN_MESSAGE):
        advertisements = await device.scan_for_addresses(d.address for d in devices)
    pairs = await _dispatch(
        devices,
        "opening",
        "opened",
        lambda dev: device.open_curtain(
            dev.address, open_target=dev.open_target, advertisements=advertisements
        ),
    )
    if any(isinstance(result, device.CurtainError) for _, result in pairs):
        raise typer.Exit(code=1)


@app.command(name="close")
@run_async
async def close_command(
    address: AddressOption = None, group: GroupOption = None, *, all_: AllOption = False
) -> None:
    """Close one or all saved devices (skips chained secondaries when targeting all).

    Devices are scanned once and then commanded concurrently, rather than one at a time.
    """
    resolved = _resolve_devices(address, group, all_=all_)
    devices, skipped = _select_action_targets(resolved, address=address)
    _note_skipped_secondaries(skipped)
    with console.status(_SCAN_MESSAGE):
        advertisements = await device.scan_for_addresses(d.address for d in devices)
    pairs = await _dispatch(
        devices,
        "closing",
        "closed",
        lambda dev: device.close_curtain(
            dev.address, closed_target=dev.closed_target, advertisements=advertisements
        ),
    )
    if any(isinstance(result, device.CurtainError) for _, result in pairs):
        raise typer.Exit(code=1)


@app.command()
@run_async
async def position(
    value: Annotated[int, typer.Argument(min=0, max=100, help="Target position, 0-100.")],
    address: AddressOption = None,
    group: GroupOption = None,
    *,
    all_: AllOption = False,
) -> None:
    """Move one or all saved devices to a position (0=closed, 100=open).

    Skips chained secondaries when targeting all saved devices. Devices are scanned
    once and then commanded concurrently, rather than one at a time.
    """
    resolved = _resolve_devices(address, group, all_=all_)
    devices, skipped = _select_action_targets(resolved, address=address)
    _note_skipped_secondaries(skipped)
    with console.status(_SCAN_MESSAGE):
        advertisements = await device.scan_for_addresses(d.address for d in devices)
    pairs = await _dispatch(
        devices,
        f"moving to {value}%",
        f"position={value}%",
        lambda dev: device.set_curtain_position(
            dev.address,
            value,
            open_target=dev.open_target,
            closed_target=dev.closed_target,
            advertisements=advertisements,
        ),
    )
    if any(isinstance(result, device.CurtainError) for _, result in pairs):
        raise typer.Exit(code=1)


async def _calibrate_direction(dev: config.DeviceConfig) -> tuple[int, int] | None:
    """Nudge the curtain and ask which way it visually moved.

    Returns (open_target, closed_target) in raw library units, or None on failure.
    """
    try:
        with console.status(f"{_label(dev)}: reading position…"):
            raw_before = await device.get_raw_position(dev.address)
    except device.CurtainError as exc:
        err_console.print(f"[red]{exc}[/red]")
        return None
    # Nudge away from whichever hard limit we're closer to, so there's room to move.
    if raw_before <= _POSITION_MIDPOINT:
        raw_target = min(raw_before + _DIRECTION_NUDGE, 100)
    else:
        raw_target = max(raw_before - _DIRECTION_NUDGE, 0)
    moved_toward_higher_raw = raw_target > raw_before
    try:
        with console.status(f"{_label(dev)}: nudging (raw {raw_before} -> {raw_target})…"):
            await device.set_raw_position(dev.address, raw_target)
    except device.CurtainError as exc:
        err_console.print(f"[red]{exc}[/red]")
        return None
    opened = typer.confirm("Did it move toward OPEN (as opposed to closed)?")
    reversed_direction = opened != moved_toward_higher_raw
    return (0, 100) if reversed_direction else (100, 0)


async def _calibrate_closed_point(
    dev: config.DeviceConfig, open_target: int, closed_target: int
) -> int:
    """Interactively nudge the closed stopping point until the user confirms it's right.

    Useful for a pair of curtains that should meet in the middle rather than each
    travelling to its own hardware limit.

    Keeps `current` at least `_MIN_ENDPOINT_GAP` away from `open_target`: letting it
    reach open_target would make closed_target == open_target, which degenerates
    device.py's position mapping to always send the same raw value regardless of the
    requested position.
    """
    step = -_FINE_TUNE_STEP if open_target > closed_target else _FINE_TUNE_STEP
    bounds = (
        (0, open_target - _MIN_ENDPOINT_GAP)
        if open_target > closed_target
        else (open_target + _MIN_ENDPOINT_GAP, 100)
    )
    current = max(bounds[0], min(bounds[1], closed_target))
    try:
        with console.status(f"{_label(dev)}: closing…"):
            await device.set_raw_position(dev.address, current)
    except device.CurtainError as exc:
        err_console.print(f"[red]{exc}[/red]")
        return current
    console.print(f"{_label(dev)}: moved to raw position {current} (fully closed).")
    while True:
        choice = typer.prompt(
            "'+' = close a bit more, '-' = back off (open slightly), 'done' when it's right",
            default="done",
        )
        if choice == "done":
            return current
        if choice == "+":
            new_current = max(bounds[0], min(bounds[1], current + step))
        elif choice == "-":
            new_current = max(bounds[0], min(bounds[1], current - step))
        else:
            console.print("Please enter '+', '-', or 'done'.")
            continue
        if new_current == current:
            console.print(f"{_label(dev)}: already at the limit in that direction.")
            continue
        current = new_current
        try:
            with console.status(f"{_label(dev)}: moving…"):
                await device.set_raw_position(dev.address, current)
        except device.CurtainError as exc:
            err_console.print(f"[red]{exc}[/red]")
            return current
        console.print(f"{_label(dev)}: moved to raw position {current}.")


@app.command()
@run_async
async def wizard(
    address: AddressOption = None, group: GroupOption = None, *, all_: AllOption = False
) -> None:
    """Interactively calibrate direction and stopping points for one or all saved devices.

    Nudges the curtain and asks which way it moved to detect its mounted direction, then
    optionally lets you fine-tune where "closed" stops -- handy for a pair that should
    meet in the middle instead of each fully closing on its own.
    """
    for dev in _resolve_devices(address, group, all_=all_):
        console.print(f"\n[bold]=== {_label(dev)} ===[/bold]")
        targets = await _calibrate_direction(dev)
        if targets is None:
            err_console.print(f"[yellow]{_label(dev)}: skipped (device not found nearby).[/yellow]")
            continue
        open_target, closed_target = targets

        if typer.confirm(
            "Fine-tune the closed stopping point (e.g. so a pair meets in the middle)?",
            default=False,
        ):
            closed_target = await _calibrate_closed_point(dev, open_target, closed_target)

        config.save_device(
            dev.address,
            alias=dev.alias,
            open_target=open_target,
            closed_target=closed_target,
            secondary=dev.secondary,
            group=dev.group,
        )
        console.print(
            f"[green]{_label(dev)}: saved open_target={open_target} "
            f"closed_target={closed_target}[/green]"
        )


@config_app.command("add")
def config_add(  # noqa: PLR0913 -- one option per DeviceConfig attribute, all genuinely independent
    address: str,
    alias: Annotated[str | None, typer.Option(help="e.g. left/right.")] = None,
    open_target: Annotated[int, typer.Option(min=0, max=100)] = 100,
    closed_target: Annotated[int, typer.Option(min=0, max=100)] = 0,
    group: Annotated[str | None, typer.Option(help="e.g. bedroom.")] = None,
    *,
    secondary: Annotated[
        bool,
        typer.Option(
            "--secondary/--primary",
            help="Already driven by another chained device; skipped in bulk actions.",
        ),
    ] = False,
) -> None:
    """Manually save a device."""
    config.save_device(
        address,
        alias=alias,
        open_target=open_target,
        closed_target=closed_target,
        secondary=secondary,
        group=group,
    )
    role = "secondary" if secondary else "primary"
    console.print(
        f"Saved {address} (alias={alias!r}, open={open_target}, closed={closed_target}, "
        f"role={role}, group={group!r})."
    )


def _get_saved_device_or_exit(identifier: str) -> config.DeviceConfig:
    dev = config.find_device(identifier, config.load_devices())
    if dev is None:
        err_console.print(
            f"No saved device matching {identifier!r}. Run 'discover' or 'config add' first."
        )
        raise typer.Exit(code=1)
    return dev


@config_app.command("set-alias")
def config_set_alias(address: str, alias: str) -> None:
    """Set or change a saved device's alias."""
    dev = _get_saved_device_or_exit(address)
    config.save_device(
        dev.address,
        alias=alias,
        open_target=dev.open_target,
        closed_target=dev.closed_target,
        secondary=dev.secondary,
        group=dev.group,
    )
    console.print(f"Set alias={alias!r} for {dev.address}.")


@config_app.command("set-group")
def config_set_group(address: str, group: str) -> None:
    """Set or change a saved device's group."""
    dev = _get_saved_device_or_exit(address)
    config.save_device(
        dev.address,
        alias=dev.alias,
        open_target=dev.open_target,
        closed_target=dev.closed_target,
        secondary=dev.secondary,
        group=group,
    )
    console.print(f"Set group={group!r} for {_label(dev)}.")


@config_app.command("set-secondary")
def config_set_secondary(
    address: str,
    *,
    secondary: Annotated[bool, typer.Option("--secondary/--primary")] = True,
) -> None:
    """Mark a device as chained/grouped (driven by another device's command).

    Secondary devices are skipped by bulk open/close/position (no --address) to avoid
    sending a redundant command, since the primary's command already moves them too.
    """
    dev = _get_saved_device_or_exit(address)
    config.save_device(
        dev.address,
        alias=dev.alias,
        open_target=dev.open_target,
        closed_target=dev.closed_target,
        secondary=secondary,
        group=dev.group,
    )
    role = "secondary" if secondary else "primary"
    console.print(f"Set role={role} for {_label(dev)}.")


@config_app.command("set-default-group")
def config_set_default_group(
    group: Annotated[str | None, typer.Argument(help="Group name, or omit with --clear.")] = None,
    *,
    clear: Annotated[bool, typer.Option("--clear", help="Remove the default group.")] = False,
) -> None:
    """Set (or clear) the group used when a command gets no --address/--group/--all."""
    if clear:
        config.save_default_group(None)
        console.print("Cleared default group.")
        return
    if group is None:
        err_console.print("Provide a group name, or pass --clear to remove the default group.")
        raise typer.Exit(code=1)
    config.save_default_group(group)
    console.print(f"Default group set to {group!r}.")


@config_app.command("list")
def config_list() -> None:
    """List saved devices."""
    default_group = config.load_default_group()
    console.print(f"Default group: {default_group!r}" if default_group else "Default group: none")
    devices = config.load_devices()
    if not devices:
        console.print("No saved devices.")
        return
    for dev in devices:
        console.print(
            f"{_label(dev)}  open_target={dev.open_target} closed_target={dev.closed_target} "
            f"role={_role(dev)} group={dev.group!r}"
        )


def main(argv: list[str] | None = None) -> int:
    try:
        app(args=argv)
    except SystemExit as exc:
        return exc.code if isinstance(exc.code, int) else 0
    except config.ConfigError as exc:
        err_console.print(f"[red]{exc}[/red]")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

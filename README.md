# switchbot-cli

[![pre-commit.ci status](https://results.pre-commit.ci/badge/github/FlavioAmurrioCS/switchbot-cli/main.svg)](https://results.pre-commit.ci/latest/github/FlavioAmurrioCS/switchbot-cli/main)

-----

Control SwitchBot Curtains directly over local Bluetooth LE — no SwitchBot
Hub, cloud account, or phone app required. Supports multiple curtains at
once (e.g. a pair covering one window).

This is a personal tool, not published to PyPI — install it straight from
GitHub instead.

## Table of Contents

- [Installation](#installation)
- [Usage](#usage)
- [License](#license)

## Installation

Install with [uv](https://docs.astral.sh/uv/) directly from this repo:

```console
uv tool install git+https://github.com/FlavioAmurrioCS/switchbot-cli
```

This puts a `switchbot-cli` command on your PATH. To upgrade later:

```console
uv tool upgrade switchbot-cli
```

Or skip installing it and just run it once:

```console
uvx --from git+https://github.com/FlavioAmurrioCS/switchbot-cli switchbot-cli --help
```

## Usage

```console
# Scan nearby BLE devices and save all Curtains found
switchbot-cli discover

# With no --address/--group, commands target every saved device together
switchbot-cli status
switchbot-cli open
switchbot-cli close
switchbot-cli position 50   # 0=closed, 100=open

# --address targets just one device; --group targets a named subset;
# --all ignores any default group and targets everything
switchbot-cli status --address AA:BB:CC:DD:EE:FF
switchbot-cli open --group bedroom
switchbot-cli close --all

# Interactively calibrate direction and stopping point (e.g. so a pair meets
# in the middle instead of each fully closing on its own)
switchbot-cli wizard

# Manage saved devices
switchbot-cli config list
switchbot-cli config set-alias AA:BB:CC:DD:EE:FF right
switchbot-cli config set-group AA:BB:CC:DD:EE:FF bedroom
switchbot-cli config set-secondary AA:BB:CC:DD:EE:FF
switchbot-cli config set-default-group bedroom
```

## License

`switchbot-cli` is distributed under the terms of the [MIT](https://spdx.org/licenses/MIT.html) license.

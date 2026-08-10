# ChamberKeep

Systray controller for [OpenChamber](https://opencode.ai). ChamberKeep does not
manage the OpenChamber server process itself - it delegates to OpenChamber's
native daemon management (`serve`, `stop`, `restart`, `status`, `update`) and
interprets the JSON output.

## Features

- Systray icon reflecting the real server state (green = running, red =
  stopped, yellow = ambiguous), polled from `openchamber status --json`.
  The icon is the ChamberKeep logo in the state color (pre-rendered from
  `logo-light.svg`).
- Start / Stop / Restart / Update from the tray menu.
- Exiting the tray never stops the server; OpenChamber keeps running as a
  daemon.
- Settings UI with the environment variables OpenChamber reads. Each field
  shows a lamp indicating its source:
  - green = value comes from an environment variable
  - orange = value is locally overridden in the config
  - no lamp = built-in default
  - Clearing a text field (or setting a boolean/radio to the environment
    value) reverts to the environment value.
- Optional: start the server automatically when ChamberKeep starts (if none is
  running), and start ChamberKeep itself with Windows.

## Requirements

- Windows (tray + registry autostart). 
- Python 3.9+
- Install dependencies: `pip install -r requirements.txt`

## Usage

```
python ChamberKeep.py
```

Optional flags:

```
python ChamberKeep.py --port 3001   # control the instance on this port
                                    # (overrides config; locked in settings)
```

## Configuration

Local, portable `chamberkeep.json` next to the script (created with defaults
on first run):

```json
{
  "port": 3000,
  "auto_start_server": false,
  "start_with_windows": false,
  "poll_seconds": 5,
  "env_overrides": {
    "OPENCHAMBER_HOST": null,
    "OPENCHAMBER_UI_PASSWORD": null,
    "OPENCHAMBER_API_ONLY": null,
    "OPENCHAMBER_DATA_DIR": null,
    "OPENCODE_HOST": null,
    "OPENCODE_PORT": null,
    "OPENCODE_SKIP_START": null,
    "OPENCHAMBER_OPENCODE_HOSTNAME": null
  }
}
```

`env_overrides` only contains values you changed in the settings dialog. The
effective value is resolved as:

```
local override (config)  >  environment variable  >  built-in default
```

The environment variables are passed to `openchamber serve` when starting or
restarting, so the daemon picks them up.

### Security note

A password you enter in the settings UI is stored in plaintext in
`chamberkeep.json`. The recommended way to supply the UI password is the
environment variable `OPENCHAMBER_UI_PASSWORD`, which keeps the value out of
any file on disk.

## Development

- All code comments and docstrings are English.
- Tests: `python <temp>/test_ck_logic.py` (pure logic, no server required).
- Icons: the `logo-*.png` tray icons are generated from `logo-light.svg` via
  `python generate_icons.py` (dev-time only, needs `resvg-py`). Re-run it
  whenever the SVG changes and commit the updated PNGs.

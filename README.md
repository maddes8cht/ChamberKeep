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
- During `update` the tray shows a blinking inverted logo with an
  "Updating..." tooltip; transient status errors are suppressed. When the
  update finishes, the display returns to the real state, and if the server
  was running before, ChamberKeep starts it again automatically. A warning
  is shown if the update takes longer than two minutes.
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
- **Remote control**: control an OpenChamber server on another PC in the local
  network. A small agent (`chamberkeep-agent.py`) runs on the target PC in the
  interactive session and executes all commands locally (correct context,
  environment and UAC). The tray connects to it through an SSH tunnel (or
  directly over the LAN) - see the "Remote" tab in the settings.

## Requirements

- Windows (tray + registry autostart). 
- Python 3.9+
- Install dependencies: `pip install -r requirements.txt`

## Remote control setup

**Target PC (runs the OpenChamber server):**
1. Start the agent: `python chamberkeep-agent.py` (or use the "Start agent
   now" button / "Start the local agent when ChamberKeep starts" option under
   "ChamberKeep" in the Server tab of the settings). The agent prints its
   port and access token on first start.
2. The agent binds to `127.0.0.1` by default. How it is reached depends on
   the transport:
   - **SSH tunnel (recommended)**: the tunnel connects to the agent via
     `127.0.0.1` on the target, so the default local-only bind suffices.
     Install Windows OpenSSH Server, configure key-based login, and open only
     port 22 in the firewall. The SSH session is used purely as an encrypted
     tunnel - commands still run inside the interactive agent, so the server
     keeps the same context as a locally started one.
   - **Direct LAN**: the client reaches the agent directly over the network,
     so it must be reachable on a network interface: start the agent with
     `python chamberkeep-agent.py --host 0.0.0.0` and open the agent port in
     the firewall. Simpler, but exposes the agent (token-protected) directly
     on the network.

**Client PC (runs the tray):**
1. In settings -> "Remote": enable remote control, choose the transport,
   enter the target host, the user, the user's login password on the target
   PC (or leave empty to use an SSH key), the ports and the agent token from
   the target.
2. Use "Test connection..." to verify, then Save. The tray now controls the
   remote server with the same status/start/stop/restart/update behavior.


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

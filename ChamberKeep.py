"""ChamberKeep - systray controller for OpenChamber.

ChamberKeep does NOT manage the OpenChamber server process itself. It
delegates to OpenChamber's native daemon management (serve/stop/restart/
status/update) and interprets the JSON output. The server keeps running
independently of the tray; exiting the tray never stops the server.

Configuration is stored in a local, portable chamberkeep.json next to
this script. Environment variables read by OpenChamber are exposed in a
settings dialog with a lamp indicator showing the source of each value
(environment, local override, or default).
"""

import argparse
import json
import os
import queue
import shutil
import subprocess
import sys
import threading
import time
import tkinter as tk
from tkinter import messagebox

try:
    import pystray
    from PIL import Image, ImageDraw
except ImportError:  # pragma: no cover - reported via CLI
    pystray = None
    Image = None
    ImageDraw = None

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(SCRIPT_DIR, "chamberkeep.json")
SERVE_LOG_PATH = os.path.join(SCRIPT_DIR, "chamberkeep-serve.log")

CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

APP_NAME = "ChamberKeep"
VERSION = "0.1.0"
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"

# Defaults for the environment variables OpenChamber reads.
ENV_DEFAULTS = {
    "OPENCHAMBER_HOST": "127.0.0.1",
    "OPENCHAMBER_UI_PASSWORD": "",
    "OPENCHAMBER_API_ONLY": False,
    "OPENCHAMBER_DATA_DIR": "",
    "OPENCODE_HOST": "",
    "OPENCODE_PORT": "",
    "OPENCODE_SKIP_START": False,
    "OPENCHAMBER_OPENCODE_HOSTNAME": "127.0.0.1",
}

ENV_BOOL_KEYS = {"OPENCHAMBER_API_ONLY", "OPENCODE_SKIP_START"}

ICON_COLORS = {
    "running": "#00cc00",
    "stopped": "#cc0000",
    "ambiguous": "#cccc00",
    "error": "#cc0000",
}

STATE_LABELS = {
    "running": "Running",
    "stopped": "Stopped",
    "ambiguous": "Ambiguous",
    "error": "Error",
}

# Per-state logo PNGs (generated from logo-light.svg via generate_icons.py).
# Falls back to the colored circle when a file is missing.
LOGO_PATHS = {
    "running": os.path.join(SCRIPT_DIR, "logo-green.png"),
    "stopped": os.path.join(SCRIPT_DIR, "logo-red.png"),
    "ambiguous": os.path.join(SCRIPT_DIR, "logo-yellow.png"),
    "error": os.path.join(SCRIPT_DIR, "logo-red.png"),
    "updating": os.path.join(SCRIPT_DIR, "logo-inverted.png"),
}

# Update progress handling: while `update` runs, error notifications are
# suppressed and the tray shows a blinking inverted logo with an
# "Updating..." tooltip. The mode ends once two consecutive polls report a
# valid status (or after UPDATE_TIMEOUT seconds, which prompts the user to
# check the server). If the server was running before the update, it is
# started again two poll cycles after the status stabilized.
UPDATE_STABLE_POLLS = 2
UPDATE_RESTART_POLLS = 2
UPDATE_TIMEOUT = 120.0  # seconds

_MUTEX_HANDLE = None


def _parse_bool(value):
    """Parse a boolean-ish environment value into a real boolean."""
    return str(value).strip().lower() in ("1", "true", "yes", "on")


class Config:
    """Load/save chamberkeep.json and resolve effective setting values.

    Precedence: local override (JSON) > environment variable > default.
    The port is passed to OpenChamber as a flag and stored at the top
    level; all environment-backed fields live in env_overrides.
    """

    def __init__(self, data=None, path=CONFIG_PATH):
        data = data or {}
        self.path = path
        self.port = int(data.get("port", 3000))
        self.auto_start_server = bool(data.get("auto_start_server", False))
        self.start_with_windows = bool(data.get("start_with_windows", False))
        self.poll_seconds = int(data.get("poll_seconds", 5))
        self.env_overrides = dict(data.get("env_overrides") or {})
        self.cli_port = None
        self._lock = threading.RLock()

    @classmethod
    def load(cls, path=CONFIG_PATH):
        """Load the config file, falling back to defaults on any error."""
        if os.path.exists(path):
            try:
                with open(path, encoding="utf-8") as f:
                    return cls(json.load(f), path=path)
            except (json.JSONDecodeError, ValueError, OSError):
                pass
        return cls({}, path=path)

    def save(self, path=None):
        """Persist the config to disk, dropping empty/None overrides."""
        path = path or self.path
        data = {
            "port": self.port,
            "auto_start_server": self.auto_start_server,
            "start_with_windows": self.start_with_windows,
            "poll_seconds": self.poll_seconds,
            "env_overrides": {
                key: value
                for key, value in self.env_overrides.items()
                if value not in (None, "")
            },
        }
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)

    def effective(self, key):
        """Return (value, source) for an env-backed key.

        source is one of "override", "env", "default". Values for boolean
        keys are returned as strings until coerced via effective_value.
        """
        with self._lock:
            stored = self.env_overrides.get(key)
            if stored not in (None, ""):
                return stored, "override"
        env_value = os.environ.get(key)
        if env_value not in (None, ""):
            return env_value, "env"
        return ENV_DEFAULTS.get(key), "default"

    def effective_value(self, key):
        """Return the coerced effective value for an env-backed key."""
        value, _ = self.effective(key)
        if key in ENV_BOOL_KEYS:
            return _parse_bool(value)
        return value

    def env_source(self, key):
        """Return the source indicator for a key: override/env/default."""
        return self.effective(key)[1]

    def set_override(self, key, value):
        with self._lock:
            self.env_overrides[key] = value

    def remove_override(self, key):
        with self._lock:
            self.env_overrides.pop(key, None)

    def build_serve_env(self):
        """Build the environment for launching 'openchamber serve'.

        The daemon inherits the environment from the launching process,
        so the resolved settings are written back as environment
        variables. Empty strings are omitted so no stray values reach
        the daemon.
        """
        env = dict(os.environ)
        for key in ENV_DEFAULTS:
            value, _ = self.effective(key)
            if key in ENV_BOOL_KEYS:
                env[key] = "true" if _parse_bool(value) else "false"
            elif value:
                env[key] = str(value)
        return env


def _find_openchamber():
    """Return the resolved path to the openchamber CLI.

    On Windows, npm installs a .cmd shim that CreateProcess cannot run
    by bare name (only .exe files are found). Returning the full path
    lets subprocess route .cmd/.bat shims through cmd.exe.
    """
    found = shutil.which("openchamber")
    return found if found else "openchamber"


class OpenChamber:
    """Thin wrapper around the openchamber CLI commands."""

    def __init__(self, config):
        self.config = config
        self._exe = _find_openchamber()

    def _run(self, args, env=None, timeout=30):
        """Run a CLI command and return (CompletedProcess|None, error)."""
        try:
            proc = subprocess.run(
                [self._exe, *args],
                env=env,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout,
                creationflags=CREATE_NO_WINDOW,
            )
        except FileNotFoundError:
            return None, "openchamber not found on PATH"
        except subprocess.TimeoutExpired:
            return None, "command timed out"
        return proc, None

    def status(self):
        """Return (status_json, error) for the configured port."""
        proc, err = self._run(
            ["status", "--port", str(self.config.port), "--json", "--quiet"],
            timeout=60,
        )
        if err:
            return None, err
        try:
            return json.loads(proc.stdout), None
        except json.JSONDecodeError:
            return None, "unparseable status output: %s" % proc.stdout.strip()

    def start(self):
        """Launch the server as an OpenChamber daemon (non-blocking)."""
        args = ["serve", "--port", str(self.config.port)]
        env = self.config.build_serve_env()
        with open(SERVE_LOG_PATH, "a", encoding="utf-8") as log:
            subprocess.Popen(
                [self._exe, *args],
                env=env,
                stdout=log,
                stderr=log,
                creationflags=CREATE_NO_WINDOW,
            )

    def stop(self, port=None):
        """Stop the daemon on the given port (default: configured port)."""
        port = port or self.config.port
        return self._run(["stop", "--port", str(port)])

    def restart(self, port=None):
        """Restart the daemon, re-applying the configured environment."""
        port = port or self.config.port
        return self._run(
            ["restart", "--port", str(port)],
            env=self.config.build_serve_env(),
            timeout=60,
        )

    def update(self):
        """Check for and install OpenChamber updates."""
        return self._run(["update", "--json"], timeout=120)


def resolve_state(data, port):
    """Map a status JSON document to (state, info_text).

    state is one of running/stopped/ambiguous/error.
    """
    if not isinstance(data, dict):
        return "error", "no status data"
    if data.get("state") != "running":
        return "stopped", "no server running on port %s" % port
    instances = data.get("instances") or []
    on_port = [i for i in instances if i.get("port") == port]
    if len(on_port) == 1:
        inst = on_port[0]
        parts = ["port %s" % inst.get("port")]
        if inst.get("pid"):
            parts.append("pid %s" % inst["pid"])
        if inst.get("launchMode"):
            parts.append(str(inst["launchMode"]))
        if inst.get("passwordProtected"):
            parts.append("password protected")
        return "running", ", ".join(parts)
    if len(on_port) > 1:
        return "ambiguous", "%d instances on port %s" % (len(on_port), port)
    other = sorted(str(i.get("port")) for i in instances if i.get("port"))
    running = ", ".join(other) if other else "none"
    return "ambiguous", "no instance on port %s (running: %s)" % (port, running)


def _registry_command():
    """Return the command line stored in the Run key for autostart."""
    if getattr(sys, "frozen", False):
        return '"%s"' % sys.executable
    exe = sys.executable
    if exe.lower().endswith("python.exe"):
        exe = exe[:-len("python.exe")] + "pythonw.exe"
    return '"%s" "%s"' % (exe, os.path.abspath(__file__))


def set_autostart(enabled):
    """Install/remove the HKCU Run entry that starts ChamberKeep at login."""
    if os.name != "nt":
        return False
    try:
        import winreg
    except ImportError:
        return False
    try:
        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE
        ) as key:
            if enabled:
                winreg.SetValueEx(key, APP_NAME, 0, winreg.REG_SZ, _registry_command())
            else:
                try:
                    winreg.DeleteValue(key, APP_NAME)
                except FileNotFoundError:
                    pass
        return True
    except OSError:
        return False


def autostart_enabled():
    """Return whether the Run key entry is currently installed."""
    if os.name != "nt":
        return False
    try:
        import winreg
        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_READ
        ) as key:
            winreg.QueryValueEx(key, APP_NAME)
        return True
    except (OSError, ImportError, FileNotFoundError):
        return False


def _acquire_singleton():
    """Guard against a second ChamberKeep instance (Windows named mutex)."""
    if os.name != "nt":
        return True
    global _MUTEX_HANDLE
    try:
        import ctypes
        _MUTEX_HANDLE = ctypes.windll.kernel32.CreateMutexW(
            None, False, "ChamberKeep_Singleton"
        )
        return ctypes.windll.kernel32.GetLastError() != 183  # ERROR_ALREADY_EXISTS
    except Exception:
        return True


class TrayApp:
    """Systray icon that reflects and controls the OpenChamber server."""

    def __init__(self, config):
        self.config = config
        self.oc = OpenChamber(config)
        self.icon = None
        self.state = "unknown"
        self.status_info = ""
        self._stop_poll = threading.Event()
        self._refresh_lock = threading.RLock()
        self._first_refresh = True
        self._tk_root = None
        self._tk_queue = queue.Queue()
        self._settings = None
        self._update_mode = False
        self._update_command_done = False
        self._update_start = 0.0
        self._update_timeout_warned = False
        self._update_stable = 0
        self._update_extra = 0
        self._update_restart_done = False
        self._update_blink = False
        self._update_was_running = False
        self._update_normal_state = "stopped"

    # --- icon / menu ------------------------------------------------------

    @staticmethod
    def create_image(color):
        """Create a 64x64 colored circle icon (fallback)."""
        img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
        draw = ImageDraw.Draw(img)
        draw.ellipse([6, 6, 58, 58], fill=color, outline="#000000", width=2)
        return img

    def create_icon(self, state):
        """Return the logo icon for `state`, falling back to a circle."""
        path = LOGO_PATHS.get(state)
        if path and os.path.exists(path):
            try:
                return Image.open(path).convert("RGBA")
            except OSError:
                pass
        return self.create_image(ICON_COLORS.get(state, "gray"))

    def build_menu(self):
        """Build the tray menu reflecting the current state."""
        updating = self._update_mode
        if updating:
            status_text = "Status: Updating..."
        else:
            state_label = STATE_LABELS.get(self.state, "Unknown")
            status_text = "Status: %s" % state_label
            if self.status_info:
                status_text += " - %s" % self.status_info
        return pystray.Menu(
            pystray.MenuItem(status_text, None, enabled=False),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Start Server", self.on_start, enabled=not updating),
            pystray.MenuItem("Stop Server", self.on_stop, enabled=not updating),
            pystray.MenuItem("Restart Server", self.on_restart, enabled=not updating),
            pystray.MenuItem("Update", self.on_update, enabled=not updating),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Settings...", self.on_settings),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Exit", self.on_exit),
        )

    def refresh(self):
        """Poll openchamber status and update icon, tooltip and menu."""
        with self._refresh_lock:
            if self.icon is None:
                return
            data, err = self.oc.status()
            if self._update_mode:
                self._refresh_during_update(data, err)
                return
            if err:
                state, info = "error", err
            else:
                state, info = resolve_state(data, self.config.port)
            self._apply_normal_display(state, info)

    def _refresh_during_update(self, data, err):
        """Handle a poll while an update is in progress.

        Polling continues but error notifications are suppressed. Until the
        update command has finished, the icon shows a blinking inverted logo
        with an "Updating..." tooltip. Afterwards the display returns to the
        real status once two consecutive polls report a valid status, and -
        if the server was running before the update - it is started again
        two poll cycles later. After UPDATE_TIMEOUT seconds the user is
        warned to check the status, since the update may have failed.
        """
        if not self._update_command_done:
            if time.monotonic() - self._update_start >= UPDATE_TIMEOUT:
                self._warn_update_timeout()
            self._apply_updating_display()
            return
        state, info = ("error", err) if err else resolve_state(data, self.config.port)
        stable = not err and state != "error"
        self._update_stable = self._update_stable + 1 if stable else 0
        if not stable:
            self._update_extra = 0
        if self._update_stable >= UPDATE_STABLE_POLLS:
            if self._update_was_running and not self._update_restart_done:
                if self._update_extra == 0:
                    self._update_extra = UPDATE_RESTART_POLLS
                else:
                    self._update_extra -= 1
                    if self._update_extra == 0:
                        self._update_restart_done = True
                        self._schedule_start_after_update()
                self._apply_normal_display(state, info)
                return
            self._finish_update(state, info)
            return
        if time.monotonic() - self._update_start >= UPDATE_TIMEOUT:
            self._warn_update_timeout()
            self._finish_update(state, info)
            return
        self._apply_updating_display()

    def _warn_update_timeout(self):
        """Warn once when the update exceeds the expected duration."""
        if not self._update_timeout_warned:
            self._update_timeout_warned = True
            self.notify(
                "Update is taking longer than %d seconds. Please check the "
                "OpenChamber status - the update may have failed."
                % int(UPDATE_TIMEOUT),
                "ChamberKeep - Update warning",
            )

    def _finish_update(self, state, info):
        """Leave update mode and show the real status."""
        self._update_mode = False
        self._update_command_done = False
        self._update_stable = 0
        self._update_extra = 0
        self._update_restart_done = False
        self._apply_normal_display(state, info)

    def _apply_normal_display(self, state, info):
        """Apply the regular icon/tooltip/menu for a real status."""
        changed = state != self.state or info != self.status_info
        self.state = state
        self.status_info = info
        self.icon.icon = self.create_icon(state)
        self.icon.title = "%s - OpenChamber (port %s)" % (
            STATE_LABELS.get(state, "Unknown"),
            self.config.port,
        )
        self.icon.menu = self.build_menu()
        self.icon.update_menu()
        if changed and not self._first_refresh and not self._update_mode:
            self.notify(self.status_info, STATE_LABELS.get(state, state))
        self._first_refresh = False

    def _apply_updating_display(self):
        """Blink the inverted logo and report "Updating..." in update mode."""
        self._update_blink = not self._update_blink
        self.icon.icon = self._updating_icon()
        self.icon.title = "ChamberKeep - Updating... (port %s)" % self.config.port
        self.icon.menu = self.build_menu()
        self.icon.update_menu()

    def _updating_icon(self):
        """Alternate between the inverted logo and the pre-update state icon."""
        if self._update_blink:
            return self.create_icon("updating")
        return self.create_icon(self._update_normal_state)

    def _enter_update_mode(self):
        """Enter update mode: suppress errors and show the updating icon."""
        self._update_mode = True
        self._update_command_done = False
        self._update_start = time.monotonic()
        self._update_timeout_warned = False
        self._update_stable = 0
        self._update_extra = 0
        self._update_restart_done = False
        self._update_blink = False
        self._update_was_running = self.state == "running"
        self._update_normal_state = self.state if self.state != "unknown" else "stopped"

    def _schedule_start_after_update(self):
        """Start the server after an update, but only if it is not running."""

        def run():
            try:
                data, err = self.oc.status()
                if not err:
                    state, _ = resolve_state(data, self.config.port)
                    if state == "running":
                        return
                self.oc.start()
                self.notify(
                    "Server restarted after update (port %s)." % self.config.port,
                    "ChamberKeep",
                )
            except Exception:
                pass

        threading.Thread(target=run, daemon=True).start()

    def notify(self, message, title):
        """Show a native tray notification (best effort)."""
        try:
            self.icon.notify(message, title)
        except Exception:
            pass

    # --- actions ----------------------------------------------------------

    def _spawn(self, fn):
        """Run an action on a worker thread so the tray stays responsive."""
        threading.Thread(target=self._task, args=(fn,), daemon=True).start()

    def _task(self, fn):
        try:
            ok, message = fn()
            self.refresh()
            self.notify(message, "ChamberKeep - %s" % ("Success" if ok else "Error"))
        except Exception as exc:
            self.refresh()
            self.notify(str(exc), "ChamberKeep - Error")

    def do_start(self):
        data, err = self.oc.status()
        if not err:
            state, _ = resolve_state(data, self.config.port)
            if state != "stopped":
                return False, "Server already running on port %s" % self.config.port
        self.oc.start()
        time.sleep(1.5)
        return True, "Server start requested on port %s" % self.config.port

    def do_stop(self):
        proc, err = self.oc.stop()
        if err:
            return False, err
        if proc.returncode != 0:
            detail = (proc.stderr or proc.stdout or "").strip()
            return False, "Failed to stop server%s" % (
                ": " + detail if detail else ""
            )
        return True, "Server stopped on port %s" % self.config.port

    def do_restart(self):
        proc, err = self.oc.restart()
        if err:
            return False, err
        if proc.returncode != 0:
            detail = (proc.stderr or proc.stdout or "").strip()
            return False, "Failed to restart server%s" % (
                ": " + detail if detail else ""
            )
        return True, "Server restarted on port %s" % self.config.port

    def do_update(self):
        proc, err = self.oc.update()
        if err:
            self._update_mode = False
            return False, err
        try:
            data = json.loads(proc.stdout)
            current = data.get("currentVersion", "?")
            latest = data.get("latestVersion", "?")
            if data.get("updated"):
                self._update_command_done = True
                return True, "OpenChamber updated %s -> %s" % (current, latest)
            self._update_mode = False
            return True, "OpenChamber is up to date (%s)" % current
        except json.JSONDecodeError:
            detail = proc.stdout.strip() or "unknown"
            self._update_mode = False
            return proc.returncode == 0, detail

    def on_start(self, icon, item):
        self._spawn(self.do_start)

    def on_stop(self, icon, item):
        self._spawn(self.do_stop)

    def on_restart(self, icon, item):
        self._spawn(self.do_restart)

    def on_update(self, icon, item):
        self._enter_update_mode()
        self._apply_updating_display()
        self._spawn(self.do_update)

    def on_settings(self, icon, item):
        self._tk_queue.put(self._show_settings)

    def on_exit(self, icon, item):
        self._stop_poll.set()
        self._tk_queue.put(self._quit_tk)
        self.icon.stop()

    def _show_settings(self):
        """Open the settings dialog on the Tk main thread."""
        if self._tk_root is None:
            return
        if self._settings is not None:
            for child in self._tk_root.winfo_children():
                child.destroy()
            self._settings = None
        self._settings = SettingsDialog(self._tk_root, self.config, self)

    def _quit_tk(self):
        if self._tk_root is not None:
            if self._settings is not None:
                self._settings.on_cancel()
            self._tk_root.quit()

    def _tk_poll(self):
        """Marshal queued callbacks from worker threads onto the Tk loop."""
        while True:
            try:
                fn = self._tk_queue.get_nowait()
            except queue.Empty:
                break
            try:
                fn()
            except Exception as exc:
                self.notify(str(exc), "ChamberKeep - Error")
        if self._tk_root is not None:
            self._tk_root.after(100, self._tk_poll)

    # --- lifecycle --------------------------------------------------------

    def start(self):
        """Start the Tk main loop (main thread) and the tray icon (detached)."""
        if self.config.start_with_windows and not autostart_enabled():
            set_autostart(True)
        threading.Thread(target=self._poll_loop, daemon=True).start()
        if self.config.auto_start_server:
            data, err = self.oc.status()
            if not err:
                state, _ = resolve_state(data, self.config.port)
                if state == "stopped":
                    self.oc.start()
        self._tk_root = tk.Tk()
        self._tk_root.withdraw()
        self._tk_root.after(100, self._tk_poll)
        self.icon = pystray.Icon(
            APP_NAME,
            self.create_icon("stopped"),
            "%s - OpenChamber (port %s)" % (APP_NAME, self.config.port),
            self.build_menu(),
        )
        self.refresh()
        self.icon.run_detached()
        self._tk_root.mainloop()

    def _poll_loop(self):
        while not self._stop_poll.is_set():
            self.refresh()
            self._stop_poll.wait(self.config.poll_seconds)


class ToolTip:
    """A minimal hover tooltip for a tkinter widget."""

    def __init__(self, widget, text):
        self.widget = widget
        self.text = text
        self.tip = None
        widget.bind("<Enter>", self.show)
        widget.bind("<Leave>", self.hide)

    def show(self, event=None):
        if self.tip is not None or not self.text:
            return
        x = self.widget.winfo_rootx() + 20
        y = self.widget.winfo_rooty() + 20
        self.tip = tk.Toplevel(self.widget)
        self.tip.wm_overrideredirect(True)
        self.tip.wm_geometry("+%d+%d" % (x, y))
        label = tk.Label(
            self.tip,
            text=self.text,
            background="#ffffe0",
            relief="solid",
            borderwidth=1,
            justify="left",
            padx=4,
            pady=2,
        )
        label.pack()
        self.tip.bind("<Leave>", self.hide)

    def hide(self, event=None):
        if self.tip is not None:
            self.tip.destroy()
            self.tip = None


def apply_env_override(config, key, value, bool_value=None):
    """Apply the revert/override rules for a field on save.

    Boolean/radio fields revert to the environment when they match the
    environment (or the default when no env var is set); text fields
    revert when empty or when they match the environment or the default.
    Anything else becomes a local override.
    """
    if key in ENV_BOOL_KEYS or bool_value is not None:
        selected = _parse_bool(value if bool_value is None else bool_value)
        env_raw = os.environ.get(key)
        if env_raw is not None:
            if selected == _parse_bool(env_raw):
                config.remove_override(key)
                return
        if selected == ENV_DEFAULTS[key]:
            config.remove_override(key)
        else:
            config.set_override(key, selected)
        return
    env_raw = os.environ.get(key)
    if not value:
        config.remove_override(key)
    elif env_raw is not None and value == env_raw:
        config.remove_override(key)
    elif env_raw is None and value == ENV_DEFAULTS.get(key, ""):
        config.remove_override(key)
    else:
        config.set_override(key, value)


class SettingsDialog:
    """Settings dialog for ChamberKeep.

    Every environment-backed field shows a lamp indicating its source:
    green = environment variable, orange = local override, none = default.
    Clearing a text field (or making a boolean/radio match the
    environment) removes the local override and reverts to the
    environment value.
    """

    LAMP_COLORS = {"env": "#00cc00", "override": "#ff9900", "default": None}

    def __init__(self, root, config, app):
        self.root = root
        self.config = config
        self.app = app
        self.dialog = root
        self.dialog.title("ChamberKeep Settings")
        self.dialog.resizable(False, False)
        self.dialog.protocol("WM_DELETE_WINDOW", self.on_cancel)
        self.dialog.deiconify()
        self.dialog.after_idle(self.dialog.grab_set)

        self.port_var = tk.StringVar()
        self.poll_var = tk.StringVar()
        self.auto_start_var = tk.BooleanVar()
        self.start_with_windows_var = tk.BooleanVar()
        self.host_var = tk.StringVar()
        self.api_only_var = tk.BooleanVar()
        self.skip_start_var = tk.BooleanVar()
        self.password_vars = {"value": tk.StringVar(), "confirm": tk.StringVar()}
        self.text_vars = {
            "OPENCHAMBER_DATA_DIR": tk.StringVar(),
            "OPENCODE_HOST": tk.StringVar(),
            "OPENCODE_PORT": tk.StringVar(),
            "OPENCHAMBER_OPENCODE_HOSTNAME": tk.StringVar(),
        }

        self._build()
        self._populate()

    # --- construction -----------------------------------------------------

    def _section(self, parent, text):
        label = tk.Label(parent, text=text, font=("Segoe UI", 9, "bold"))
        label.grid(sticky="w", pady=(10, 4))
        return label

    def _lamp(self, key):
        """Create a lamp label (source indicator) for an env-backed field."""
        label = tk.Label(self.body, text="", width=2)
        tip = ToolTip(label, "")
        self._lamp_tips[key] = tip
        return label

    def _build(self):
        self.body = tk.Frame(self.dialog)
        self.body.pack(fill="both", expand=True, padx=12, pady=8)
        self.body.columnconfigure(1, weight=1)
        self._lamp_tips = {}

        row = 0
        self._section(self.body, "Server").grid(row=row, column=0, columnspan=3)
        row += 1
        self._add_label(row, 0, "Port")
        port_entry = tk.Entry(
            self.body, textvariable=self.port_var, width=30, state="readonly" if self.config.cli_port else "normal"
        )
        port_entry.grid(row=row, column=1, sticky="we")
        if self.config.cli_port:
            ToolTip(port_entry, "Set via command line (--port); locked")
        row += 1

        self._add_label(row, 0, "Host")
        host_frame = tk.Frame(self.body)
        host_frame.grid(row=row, column=1, sticky="w")
        for value, text in (
            ("127.0.0.1", "Localhost only (127.0.0.1)"),
            ("0.0.0.0", "Local network (0.0.0.0)"),
        ):
            tk.Radiobutton(
                host_frame, text=text, value=value, variable=self.host_var
            ).pack(anchor="w")
        self.host_lamp = self._lamp("OPENCHAMBER_HOST")
        self.host_lamp.grid(row=row, column=2, sticky="n")
        row += 1

        self._add_label(row, 0, "API only")
        tk.Checkbutton(
            self.body, text="Serve API routes only (no browser UI)", variable=self.api_only_var
        ).grid(row=row, column=1, sticky="w")
        self.api_lamp = self._lamp("OPENCHAMBER_API_ONLY")
        self.api_lamp.grid(row=row, column=2)
        row += 1

        self._add_label(row, 0, "UI password")
        pw_frame = tk.Frame(self.body)
        pw_frame.grid(row=row, column=1, sticky="we")
        tk.Entry(
            pw_frame, textvariable=self.password_vars["value"], show="*", width=30
        ).pack(fill="x")
        tk.Label(pw_frame, text="Confirm:").pack(anchor="w", pady=(6, 0))
        tk.Entry(
            pw_frame, textvariable=self.password_vars["confirm"], show="*", width=30
        ).pack(fill="x")
        self.pw_lamp = self._lamp("OPENCHAMBER_UI_PASSWORD")
        self.pw_lamp.grid(row=row, column=2, sticky="n")
        row += 1

        self._section(self.body, "OpenCode integration").grid(
            row=row, column=0, columnspan=3
        )
        row += 1

        self._add_label(row, 0, "Data directory")
        self._add_text_row(row, "OPENCHAMBER_DATA_DIR")
        row += 1
        self._add_label(row, 0, "OpenCode host")
        self._add_text_row(row, "OPENCODE_HOST")
        row += 1
        self._add_label(row, 0, "OpenCode port")
        self._add_text_row(row, "OPENCODE_PORT")
        row += 1
        self._add_label(row, 0, "OpenCode hostname")
        self._add_text_row(row, "OPENCHAMBER_OPENCODE_HOSTNAME")
        row += 1

        self._add_label(row, 0, "Skip starting OpenCode")
        tk.Checkbutton(
            self.body, text="Use an external OpenCode server", variable=self.skip_start_var
        ).grid(row=row, column=1, sticky="w")
        self.skip_lamp = self._lamp("OPENCODE_SKIP_START")
        self.skip_lamp.grid(row=row, column=2)
        row += 1

        self._section(self.body, "ChamberKeep").grid(
            row=row, column=0, columnspan=3
        )
        row += 1
        self._add_label(row, 0, "Poll interval (s)")
        tk.Entry(self.body, textvariable=self.poll_var, width=30).grid(
            row=row, column=1, sticky="we"
        )
        row += 1
        tk.Checkbutton(
            self.body,
            text="Start the server when ChamberKeep starts (if not running)",
            variable=self.auto_start_var,
        ).grid(row=row, column=1, sticky="w")
        row += 1
        tk.Checkbutton(
            self.body,
            text="Start ChamberKeep with Windows",
            variable=self.start_with_windows_var,
        ).grid(row=row, column=1, sticky="w")
        row += 1

        buttons = tk.Frame(self.dialog)
        buttons.pack(fill="x", padx=12, pady=8)
        tk.Button(buttons, text="Save", width=12, command=self.on_save).pack(
            side="right", padx=(6, 0)
        )
        tk.Button(buttons, text="Cancel", width=12, command=self.on_cancel).pack(
            side="right"
        )

    def _add_label(self, row, column, text):
        tk.Label(self.body, text=text, anchor="w").grid(
            row=row, column=column, sticky="w", pady=2
        )

    def _add_text_row(self, row, key):
        var = self.text_vars[key]
        entry = tk.Entry(self.body, textvariable=var, width=30)
        entry.grid(row=row, column=1, sticky="we")
        lamp = self._lamp(key)
        lamp.grid(row=row, column=2)
        setattr(self, "lamp_%s" % key, lamp)

    # --- population / validation ------------------------------------------

    def _populate(self):
        self.port_var.set(str(self.config.cli_port or self.config.port))
        self.poll_var.set(str(self.config.poll_seconds))
        self.auto_start_var.set(self.config.auto_start_server)
        self.start_with_windows_var.set(autostart_enabled())

        self._set_lamp("OPENCHAMBER_HOST", self.host_lamp)
        self.host_var.set(self.config.effective_value("OPENCHAMBER_HOST"))
        self._set_lamp("OPENCHAMBER_API_ONLY", self.api_lamp)
        self.api_only_var.set(self.config.effective_value("OPENCHAMBER_API_ONLY"))
        self._set_lamp("OPENCHAMBER_UI_PASSWORD", self.pw_lamp)
        self.password_vars["value"].set(
            self.config.effective_value("OPENCHAMBER_UI_PASSWORD")
        )
        self.password_vars["confirm"].set(
            self.config.effective_value("OPENCHAMBER_UI_PASSWORD")
        )
        self._set_lamp("OPENCODE_SKIP_START", self.skip_lamp)
        self.skip_start_var.set(self.config.effective_value("OPENCODE_SKIP_START"))

        for key in self.text_vars:
            self._set_lamp(key, getattr(self, "lamp_%s" % key))
            self.text_vars[key].set(self.config.effective_value(key))

    def _set_lamp(self, key, widget):
        """Refresh a lamp widget to match the current source for a key."""
        source = self.config.env_source(key)
        color = self.LAMP_COLORS.get(source)
        widget.config(text="\u25cf" if color else "", fg=color)
        env_value = os.environ.get(key)
        tip = self._lamp_tips.get(key)
        if tip is None:
            return
        if source == "env":
            tip.text = "Value comes from environment variable %s=%s" % (key, env_value)
        elif source == "override":
            tip.text = "Local override stored in config"
            if env_value:
                tip.text += " (overrides environment variable %s)" % key
            else:
                tip.text += " (no environment variable %s set)" % key
        else:
            tip.text = "No environment variable %s set; using default" % key

    def _validate(self):
        try:
            port = int(self.port_var.get())
            if not 1 <= port <= 65535:
                raise ValueError
        except ValueError:
            messagebox.showerror("Invalid setting", "Port must be a number 1-65535.")
            return None
        try:
            poll = int(self.poll_var.get())
            if poll < 1:
                raise ValueError
        except ValueError:
            messagebox.showerror(
                "Invalid setting", "Poll interval must be a positive number."
            )
            return None
        op_port = self.text_vars["OPENCODE_PORT"].get().strip()
        if op_port:
            try:
                op_port_int = int(op_port)
                if not 1 <= op_port_int <= 65535:
                    raise ValueError
            except ValueError:
                messagebox.showerror(
                    "Invalid setting", "OpenCode port must be a number 1-65535."
                )
                return None
        password = self.password_vars["value"].get()
        confirm = self.password_vars["confirm"].get()
        if password != confirm:
            messagebox.showerror(
                "Invalid setting", "The password entries do not match."
            )
            return None
        return port, poll

    # --- save / cancel ----------------------------------------------------

    def _apply_env_override(self, key, value, bool_value=None):
        """Delegate the revert/override rules to the shared helper."""
        apply_env_override(self.config, key, value, bool_value)

    def on_save(self):
        port, poll = self._validate()
        if port is None:
            return
        if self.config.cli_port is None:
            self.config.port = port
        self.config.poll_seconds = poll
        self.config.auto_start_server = self.auto_start_var.get()

        self._apply_env_override("OPENCHAMBER_HOST", self.host_var.get())
        self._apply_env_override("OPENCHAMBER_API_ONLY", self.api_only_var.get())
        self._apply_env_override("OPENCHAMBER_UI_PASSWORD", self.password_vars["value"].get())
        self._apply_env_override("OPENCODE_SKIP_START", self.skip_start_var.get())
        for key, var in self.text_vars.items():
            self._apply_env_override(key, var.get().strip())

        want_autostart = self.start_with_windows_var.get()
        if want_autostart != autostart_enabled():
            ok = set_autostart(want_autostart)
            if not ok:
                messagebox.showerror(
                    "ChamberKeep", "Could not update the Windows autostart entry."
                )
        self.config.start_with_windows = want_autostart

        self.config.save()
        if self.app is not None:
            self.app.refresh()
        self.on_cancel()

    def on_cancel(self):
        """Close the dialog by hiding the root again."""
        try:
            self.dialog.grab_release()
        except tk.TclError:
            pass
        self.dialog.withdraw()


def main():
    parser = argparse.ArgumentParser(
        description="ChamberKeep - systray controller for OpenChamber."
    )
    parser.add_argument(
        "--port",
        type=int,
        help="OpenChamber port to control (overrides config; mandatory value).",
    )
    parser.add_argument(
        "--version", action="version", version="ChamberKeep %s" % VERSION
    )
    args = parser.parse_args()

    if pystray is None or Image is None:
        print("Missing dependency: install pystray and Pillow (pip install -r requirements.txt)")
        return 1

    if not _acquire_singleton():
        print("Another ChamberKeep instance is already running.")
        return 1

    config = Config.load()
    if args.port is not None:
        config.port = args.port
        config.cli_port = args.port

    TrayApp(config).start()
    return 0


if __name__ == "__main__":
    sys.exit(main())

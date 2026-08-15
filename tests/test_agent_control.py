import sys

import pytest

import ChamberKeep as ck
from fakes import FakeHTTPConnection, FakeResponse, fake_http


class TestAgentRunning:
    @pytest.mark.parametrize(
        "status, request_error, expected",
        [
            (200, None, True),
            (500, None, False),
            (200, OSError("connection refused"), False),
        ],
    )
    def test_cases(self, monkeypatch, status, request_error, expected):
        conn = FakeHTTPConnection(None, None)
        conn.response = FakeResponse(status, b"")
        conn.raise_on_request = request_error
        fake_http(monkeypatch, conn)
        assert ck.agent_running(8040) is expected

    def test_targets_localhost_ping(self, monkeypatch):
        conn = FakeHTTPConnection(None, None)
        conn.response = FakeResponse(200, b"")
        fake_http(monkeypatch, conn)
        assert ck.agent_running(8040) is True
        assert conn.host == "127.0.0.1"
        assert conn.port == 8040
        assert conn.timeout == 1
        verb, path, _, _ = conn.request_calls[0]
        assert (verb, path) == ("GET", "/api/ping")


class TestStopAgent:
    @pytest.mark.parametrize(
        "status, body, request_error, expected",
        [
            (200, b'{"ok": true}', None, (True, "Agent stopped")),
            (200, b'{"ok": false, "error": "nope"}', None, (False, "nope")),
            (500, b'{"ok": true}', None, (False, "shutdown failed (500)")),
            (200, b"<html>oops</html>", None, (False, "shutdown failed (200)")),
            (200, b"{}", OSError("refused"), (False, "Agent unreachable: refused")),
        ],
    )
    def test_cases(self, monkeypatch, status, body, request_error, expected):
        conn = FakeHTTPConnection(None, None)
        conn.response = FakeResponse(status, body)
        conn.raise_on_request = request_error
        fake_http(monkeypatch, conn)
        assert ck.stop_agent(8040, "tok") == expected

    def test_request_shape(self, monkeypatch):
        conn = FakeHTTPConnection(None, None)
        conn.response = FakeResponse(200, b'{"ok": true}')
        fake_http(monkeypatch, conn)
        assert ck.stop_agent(8040, "tok") == (True, "Agent stopped")
        assert conn.host == "127.0.0.1"
        assert conn.port == 8040
        assert conn.timeout == 5
        verb, path, _, headers = conn.request_calls[0]
        assert (verb, path) == ("POST", "/api/shutdown")
        assert headers["X-ChamberKeep-Token"] == "tok"


class TestLaunchAgent:
    @pytest.mark.parametrize(
        "agent_up, popen_error, expected",
        [
            (True, None, "already running on port 8040"),
            (False, None, "started on port 8040"),
            (False, OSError("cannot spawn"), "failed to start: cannot spawn"),
        ],
    )
    def test_cases(self, monkeypatch, agent_up, popen_error, expected):
        spawned = []
        monkeypatch.setattr(ck, "agent_running", lambda port: agent_up)
        if popen_error is not None:
            def boom(*a, **kw):
                raise popen_error

            monkeypatch.setattr(ck.subprocess, "Popen", boom)
        else:
            monkeypatch.setattr(
                ck.subprocess, "Popen",
                lambda cmd, **kw: spawned.append(cmd) or object(),
            )
        assert ck.launch_agent(8040) == expected
        if agent_up:
            assert spawned == []
        elif popen_error is None:
            assert "--port" in spawned[0]
            assert "8040" in spawned[0]


class FakeWinreg:
    def __init__(self):
        self.set_calls = []
        self.delete_calls = []
        self.query_calls = []
        self.open_error = None
        self.delete_error = None
        self.HKEY_CURRENT_USER = "HKCU"
        self.KEY_SET_VALUE = 2
        self.KEY_READ = 1
        self.REG_SZ = 1

    def OpenKey(self, root, path, reserved, access):
        if self.open_error is not None:
            raise self.open_error
        return self

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def SetValueEx(self, key, name, reserved, typ, value):
        self.set_calls.append((name, typ, value))

    def DeleteValue(self, key, name):
        self.delete_calls.append(name)
        if self.delete_error is not None:
            raise self.delete_error

    def QueryValueEx(self, key, name):
        self.query_calls.append(name)
        return ("cmd", 1)


def install_winreg(monkeypatch, fake):
    monkeypatch.setattr(ck.os, "name", "nt")
    monkeypatch.setitem(sys.modules, "winreg", fake)


class TestRegistryCommand:
    @pytest.mark.parametrize(
        "frozen, exe, prefix",
        [
            (True, r"C:\Python39\python.exe", '"C:\\Python39\\python.exe"'),
            (False, r"C:\Python39\python.exe", '"C:\\Python39\\pythonw.exe"'),
            (False, r"C:\Tools\py.exe", '"C:\\Tools\\py.exe"'),
        ],
    )
    def test_cases(self, monkeypatch, frozen, exe, prefix):
        monkeypatch.setattr(sys, "frozen", frozen, raising=False)
        monkeypatch.setattr(sys, "executable", exe)
        assert ck._registry_command().startswith(prefix)
        assert ck._registry_command().endswith('"')


class TestSetAutostart:
    @pytest.mark.parametrize(
        "enabled, os_name, open_error, delete_error, winreg_available, expected",
        [
            (True, "nt", None, None, True, True),
            (False, "nt", None, None, True, True),
            (False, "nt", None, FileNotFoundError("no key"), True, True),
            (True, "nt", OSError("denied"), None, True, False),
            (True, "nt", None, None, False, False),
            (True, "posix", None, None, True, False),
        ],
    )
    def test_cases(
        self,
        monkeypatch,
        enabled,
        os_name,
        open_error,
        delete_error,
        winreg_available,
        expected,
    ):
        monkeypatch.setattr(ck.os, "name", os_name)
        fake = None
        if os_name == "nt":
            if winreg_available:
                fake = FakeWinreg()
                fake.open_error = open_error
                fake.delete_error = delete_error
                install_winreg(monkeypatch, fake)
            else:
                monkeypatch.setitem(sys.modules, "winreg", None)
        assert ck.set_autostart(enabled) is expected
        if fake is not None:
            if enabled and open_error is None:
                assert fake.set_calls == [(ck.APP_NAME, 1, ck._registry_command())]
                assert fake.delete_calls == []
            elif not enabled and open_error is None:
                assert fake.set_calls == []
                assert fake.delete_calls == [ck.APP_NAME]


class TestAutostartEnabled:
    @pytest.mark.parametrize(
        "open_error, expected",
        [
            (None, True),
            (FileNotFoundError("no key"), False),
            (OSError("denied"), False),
        ],
    )
    def test_cases(self, monkeypatch, open_error, expected):
        fake = FakeWinreg()
        fake.open_error = open_error
        install_winreg(monkeypatch, fake)
        assert ck.autostart_enabled() is expected
        if expected:
            assert fake.query_calls == [ck.APP_NAME]
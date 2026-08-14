import json

import pytest

import ChamberKeep as ck


class FakeProc:
    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


@pytest.fixture
def loc():
    return ck.LocalOpenChamber(ck.Config({}))


class TestRun:
    def test_file_not_found_and_timeout(self, loc, monkeypatch):
        def raise_fnf(*a, **kw):
            raise FileNotFoundError

        monkeypatch.setattr(ck.subprocess, "run", raise_fnf)
        assert loc._run(["status"]) == (None, "openchamber not found on PATH")

        def raise_timeout(*a, **kw):
            raise ck.subprocess.TimeoutExpired("openchamber", 30)

        monkeypatch.setattr(ck.subprocess, "run", raise_timeout)
        assert loc._run(["status"]) == (None, "command timed out")


class TestStatus:
    def test_parses_json(self, loc, monkeypatch):
        monkeypatch.setattr(
            ck.subprocess, "run",
            lambda *a, **kw: FakeProc(stdout='{"state": "running"}'),
        )
        data, err = loc.status()
        assert data == {"state": "running"}
        assert err is None

    def test_unparseable_stdout(self, loc, monkeypatch):
        monkeypatch.setattr(
            ck.subprocess, "run",
            lambda *a, **kw: FakeProc(stdout="not json at all\n"),
        )
        data, err = loc.status()
        assert data is None
        assert err == "unparseable status output: not json at all"


class TestStart:
    def test_ok(self, loc, monkeypatch, tmp_path):
        monkeypatch.setattr(ck, "SERVE_LOG_PATH", str(tmp_path / "serve.log"))
        spawned = []
        monkeypatch.setattr(
            ck.subprocess, "Popen",
            lambda cmd, **kw: spawned.append(cmd) or object(),
        )
        ok, message = loc.start()
        assert ok is True
        assert message == "Server start requested on port 3000"
        assert "serve" in spawned[0]
        assert "--port" in spawned[0]

    def test_oserror(self, loc, monkeypatch, tmp_path):
        monkeypatch.setattr(ck, "SERVE_LOG_PATH", str(tmp_path / "serve.log"))

        def boom(*a, **kw):
            raise OSError("cannot spawn")

        monkeypatch.setattr(ck.subprocess, "Popen", boom)
        ok, message = loc.start()
        assert ok is False
        assert message == "Failed to launch server: cannot spawn"


class TestStopRestart:
    def test_stop_paths(self, loc, monkeypatch):
        monkeypatch.setattr(ck.subprocess, "run", lambda *a, **kw: FakeProc())
        assert loc.stop() == (True, "Server stopped on port 3000")

        monkeypatch.setattr(
            ck.subprocess, "run",
            lambda *a, **kw: FakeProc(returncode=2, stderr="no daemon on port"),
        )
        assert loc.stop() == (False, "Failed to stop server: no daemon on port")

        monkeypatch.setattr(ck.subprocess, "run", lambda *a, **kw: FakeProc(returncode=1))
        assert loc.stop() == (False, "Failed to stop server")

        calls = []

        def rec(args, **kw):
            calls.append(args)
            return FakeProc()

        monkeypatch.setattr(ck.subprocess, "run", rec)
        loc.stop(port=8080)
        assert "8080" in calls[-1]

    def test_restart_paths(self, loc, monkeypatch):
        monkeypatch.setattr(ck.subprocess, "run", lambda *a, **kw: FakeProc())
        assert loc.restart() == (True, "Server restarted on port 3000")

        monkeypatch.setattr(
            ck.subprocess, "run",
            lambda *a, **kw: FakeProc(returncode=1, stderr="nope"),
        )
        assert loc.restart() == (False, "Failed to restart server: nope")

        def raise_fnf(*a, **kw):
            raise FileNotFoundError

        monkeypatch.setattr(ck.subprocess, "run", raise_fnf)
        assert loc.restart() == (False, "openchamber not found on PATH")


class TestUpdate:
    def test_paths(self, loc, monkeypatch):
        monkeypatch.setattr(
            ck.subprocess, "run",
            lambda *a, **kw: FakeProc(stdout=json.dumps(
                {"updated": True, "currentVersion": "0.2.0", "latestVersion": "0.3.0"}
            )),
        )
        ok, message, updated = loc.update()
        assert ok is True
        assert updated is True
        assert "0.2.0 -> 0.3.0" in message

        monkeypatch.setattr(
            ck.subprocess, "run",
            lambda *a, **kw: FakeProc(stdout=json.dumps(
                {"updated": False, "currentVersion": "0.3.0"}
            )),
        )
        ok, message, updated = loc.update()
        assert ok is True
        assert updated is False
        assert "up to date" in message

        monkeypatch.setattr(ck.subprocess, "run", lambda *a, **kw: FakeProc(stdout="oops"))
        assert loc.update() == (True, "oops", None)

        monkeypatch.setattr(
            ck.subprocess, "run", lambda *a, **kw: FakeProc(returncode=1),
        )
        assert loc.update() == (False, "unknown", None)
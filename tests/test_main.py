import sys

import pytest

import ChamberKeep as ck


class TestAcquireSingleton:
    def test_non_windows_returns_true(self, monkeypatch):
        monkeypatch.setattr(ck.os, "name", "posix")
        assert ck._acquire_singleton() is True

    def test_error_already_exists_returns_false(self, monkeypatch):
        monkeypatch.setattr(ck.os, "name", "nt")
        monkeypatch.setitem(sys.modules, "ctypes", FakeCtypes(183))
        assert ck._acquire_singleton() is False

    def test_other_last_error_returns_true(self, monkeypatch):
        monkeypatch.setattr(ck.os, "name", "nt")
        monkeypatch.setitem(sys.modules, "ctypes", FakeCtypes(0))
        assert ck._acquire_singleton() is True

    def test_ctypes_exception_returns_true(self, monkeypatch):
        monkeypatch.setattr(ck.os, "name", "nt")

        class BoomCtypes:
            @property
            def windll(self):
                raise OSError("ctypes unavailable")

        monkeypatch.setitem(sys.modules, "ctypes", BoomCtypes())
        assert ck._acquire_singleton() is True


class FakeKernel32:
    def __init__(self, last_error):
        self.last_error = last_error
        self.mutex_calls = []

    def CreateMutexW(self, *args):
        self.mutex_calls.append(args)
        return 123

    def GetLastError(self):
        return self.last_error


class FakeCtypes:
    def __init__(self, last_error):
        self.windll = type(
            "FakeWindll", (), {"kernel32": FakeKernel32(last_error)}
        )()


@pytest.fixture
def main_env(monkeypatch):
    monkeypatch.setattr(ck.Config, "load", classmethod(lambda cls, *a, **kw: ck.Config({})))
    return monkeypatch


class TestMain:
    def test_missing_pystray_returns_1(self, main_env):
        main_env.setattr(ck, "pystray", None)
        main_env.setattr(ck, "Image", None)
        main_env.setattr(sys, "argv", ["ChamberKeep.py"])
        assert ck.main() == 1

    def test_singleton_busy_returns_1(self, main_env):
        main_env.setattr(ck, "_acquire_singleton", lambda: False)
        main_env.setattr(sys, "argv", ["ChamberKeep.py"])
        assert ck.main() == 1

    def test_port_flag_applies_to_config(self, main_env):
        started = []

        class RecordingTrayApp:
            def __init__(self, config):
                self.config = config

            def start(self):
                started.append(self.config)
                return None

        main_env.setattr(ck, "_acquire_singleton", lambda: True)
        main_env.setattr(ck, "TrayApp", RecordingTrayApp)
        main_env.setattr(sys, "argv", ["ChamberKeep.py", "--port", "4321"])
        assert ck.main() == 0
        config = started[0]
        assert config.port == 4321
        assert config.cli_port == 4321

    def test_no_port_keeps_config_default(self, main_env):
        started = []

        class RecordingTrayApp:
            def __init__(self, config):
                self.config = config

            def start(self):
                started.append(self.config)
                return None

        main_env.setattr(ck, "_acquire_singleton", lambda: True)
        main_env.setattr(ck, "TrayApp", RecordingTrayApp)
        main_env.setattr(sys, "argv", ["ChamberKeep.py"])
        assert ck.main() == 0
        config = started[0]
        assert config.port == 3000
        assert config.cli_port is None
import sys
import time
import types

import pytest

import ChamberKeep as ck


class FakeTransport:
    def __init__(self, active=True):
        self.active = active
        self.opened = []

    def is_active(self):
        return self.active

    def open_channel(self, kind, target, source):
        self.opened.append((kind, target, source))
        return FakeChannel()


class FakeChannel:
    def __init__(self):
        self.timeout = None
        self.sent = b""
        self.closed = False

    def settimeout(self, t):
        self.timeout = t

    def sendall(self, data):
        self.sent += data

    def recv(self, bufsize):
        return b""

    def makefile(self, mode="r", buffering=None):
        return []

    def close(self):
        self.closed = True


class FakeClient:
    def __init__(self):
        self.transport = FakeTransport(active=True)
        self.connect_calls = 0
        self.close_calls = 0
        self.connect_error = None
        self.last_kwargs = None

    def set_missing_host_key_policy(self, policy):
        pass

    def load_system_host_keys(self):
        pass

    def connect(self, **kwargs):
        self.connect_calls += 1
        self.last_kwargs = kwargs
        if self.connect_error is not None:
            raise self.connect_error

    def get_transport(self):
        return self.transport

    def close(self):
        self.close_calls += 1


class FakeParamiko:
    """Callable helper building an isolated fake paramiko module."""

    class AuthenticationException(Exception):
        pass

    def __init__(self, connect_error=None):
        error = connect_error

        class ParamikoClient(FakeClient):
            def connect(self, **kwargs):
                self.connect_calls += 1
                self.last_kwargs = kwargs
                if error is not None:
                    raise error

        self._module = types.SimpleNamespace(
            SSHClient=ParamikoClient,
            AutoAddPolicy=object,
            AuthenticationException=self.AuthenticationException,
        )

    def install(self, monkeypatch):
        monkeypatch.setitem(sys.modules, "paramiko", self._module)


@pytest.fixture
def ssh_config():
    return ck.Config({
        "remote_enabled": True,
        "remote_mode": "ssh",
        "remote_host": "192.168.1.50",
        "remote_user": "alice",
        "remote_password": "pw",
        "remote_ssh_port": 2222,
        "remote_agent_port": 8040,
    })


class TestConnect:
    def test_retry_delay_guard(self, monkeypatch, ssh_config):
        monkeypatch.setitem(sys.modules, "paramiko", None)
        tunnel = ck.SSHTunnel(ssh_config)
        tunnel._last_failure = time.monotonic()
        assert tunnel.connect() == "SSH connection not established yet (will retry)"

    def test_paramiko_missing_and_generic_failure(self, monkeypatch, ssh_config):
        monkeypatch.setitem(sys.modules, "paramiko", None)
        tunnel = ck.SSHTunnel(ssh_config)
        tunnel._last_failure = 0.0
        err = tunnel.connect()
        assert "paramiko is not installed" in err
        assert "pip install paramiko" in err

        FakeParamiko(connect_error=OSError("timed out")).install(monkeypatch)
        tunnel = ck.SSHTunnel(ssh_config)
        tunnel._last_failure = 0.0
        assert tunnel.connect() == "SSH connection to 192.168.1.50 failed: timed out"

    def test_auth_failure_message(self, monkeypatch, ssh_config):
        fake = FakeParamiko(
            connect_error=FakeParamiko.AuthenticationException("nope")
        )
        fake.install(monkeypatch)
        tunnel = ck.SSHTunnel(ssh_config)
        tunnel._last_failure = 0.0
        assert tunnel.connect() == (
            "SSH authentication failed for alice@192.168.1.50: wrong password/key"
        )

    def test_reuse_and_reconnect(self, monkeypatch, ssh_config):
        FakeParamiko().install(monkeypatch)
        tunnel = ck.SSHTunnel(ssh_config)
        old = FakeClient()
        tunnel._client = old
        assert tunnel.connect() is None
        assert old.connect_calls == 0

        old.transport = FakeTransport(active=False)
        assert tunnel.connect() is None
        assert old.close_calls == 1
        assert tunnel._client is not old
        kw = tunnel._client.last_kwargs
        assert kw["hostname"] == "192.168.1.50"
        assert kw["port"] == 2222
        assert kw["username"] == "alice"
        assert kw["password"] == "pw"
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


class FakeTunnel:
    def __init__(self, channel):
        self._channel = channel

    def open_channel(self):
        return self._channel


class TestOpenChannel:
    def test_direct_tcpip_target_and_default_timeout(self, ssh_config):
        tunnel = ck.SSHTunnel(ssh_config)
        transport = FakeTransport()
        client = FakeClient()
        client.transport = transport
        tunnel._client = client
        channel = tunnel.open_channel()
        assert transport.opened == [
            ("direct-tcpip", ("127.0.0.1", 8040), ("127.0.0.1", 0))
        ]
        assert channel.timeout == 30


class TestClose:
    def test_idempotent(self, ssh_config):
        tunnel = ck.SSHTunnel(ssh_config)
        client = FakeClient()
        tunnel._client = client
        tunnel.close()
        tunnel.close()
        assert client.close_calls == 1
        assert tunnel._client is None


class TestChannelSocket:
    def test_forwards_to_channel(self):
        channel = FakeChannel()
        sock = ck._ChannelSocket(channel)
        sock.sendall(b"abc")
        sock.settimeout(12)
        assert channel.sent == b"abc"
        assert channel.timeout == 12
        assert sock.recv(10) == b""
        assert sock.makefile("rb") == []
        assert sock.connect(("host", 1)) is None
        assert sock.shutdown(0) is None
        sock.close()
        assert channel.closed is True


class TestTunnelHTTPConnection:
    @pytest.mark.parametrize(
        "timeout, expected", [(240, 240), (30, 30), (None, None)]
    )
    def test_connect_channel_timeout(self, timeout, expected):
        channel = FakeChannel()
        tunnel = FakeTunnel(channel)
        conn = ck._TunnelHTTPConnection(tunnel, "127.0.0.1", 8040, timeout=timeout)
        conn.connect()
        assert isinstance(conn.sock, ck._ChannelSocket)
        assert channel.timeout == expected
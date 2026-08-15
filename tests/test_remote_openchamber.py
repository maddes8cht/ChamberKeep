import json

import pytest

import ChamberKeep as ck
from fakes import FakeHTTPConnection, FakeResponse, fake_http

RUNNING = {"status": "ok", "state": "running", "instances": [{"port": 3000}]}


@pytest.fixture
def lan_config():
    return ck.Config({
        "remote_enabled": True,
        "remote_mode": "lan",
        "remote_host": "10.0.0.9",
        "agent_token": "tok",
    })


@pytest.fixture
def ssh_config():
    return ck.Config({
        "remote_enabled": True,
        "remote_mode": "ssh",
        "remote_host": "10.0.0.9",
        "remote_user": "user",
        "remote_agent_port": 8040,
        "agent_token": "tok",
    })


class TestRequestLan:
    def test_ok_uses_get_and_token_header(self, monkeypatch, lan_config):
        conn = FakeHTTPConnection(None, None)
        conn.response = FakeResponse(200, b'{"ok": true}')
        fake_http(monkeypatch, conn)
        remote = ck.RemoteOpenChamber(lan_config)
        payload, err = remote._request("status")
        assert payload == {"ok": True}
        assert err is None
        verb, path, body, headers = conn.request_calls[0]
        assert verb == "GET"
        assert path == "/api/status"
        assert headers["X-ChamberKeep-Token"] == "tok"

    def test_post_verb_and_body(self, monkeypatch, lan_config):
        conn = FakeHTTPConnection(None, None)
        conn.response = FakeResponse(200, b'{"ok": true}')
        fake_http(monkeypatch, conn)
        remote = ck.RemoteOpenChamber(lan_config)
        remote._request("start", data={"force": True})
        verb, path, body, headers = conn.request_calls[0]
        assert verb == "POST"
        assert json.loads(body) == {"force": True}

    def test_401_rejected_token(self, monkeypatch, lan_config):
        conn = FakeHTTPConnection(None, None)
        conn.response = FakeResponse(401, b'{"ok": false, "error": "unauthorized"}')
        fake_http(monkeypatch, conn)
        remote = ck.RemoteOpenChamber(lan_config)
        payload, err = remote._request("status")
        assert payload is None
        assert err == "agent rejected the token (check settings)"

    def test_unparseable_body(self, monkeypatch, lan_config):
        conn = FakeHTTPConnection(None, None)
        conn.response = FakeResponse(200, b"<html>gateway</html>")
        fake_http(monkeypatch, conn)
        remote = ck.RemoteOpenChamber(lan_config)
        payload, err = remote._request("status")
        assert payload is None
        assert err == "unparseable agent response: <html>gateway</html>"

    def test_oserror_unreachable(self, monkeypatch, lan_config):
        conn = FakeHTTPConnection(None, None)
        conn.raise_on_request = OSError("connection refused")
        fake_http(monkeypatch, conn)
        remote = ck.RemoteOpenChamber(lan_config)
        payload, err = remote._request("status")
        assert payload is None
        assert err == "agent unreachable over lan: connection refused"


class TestRequestSsh:
    def test_uses_tunnel_and_short_circuits_on_error(self, monkeypatch, ssh_config):
        class FakeTunnel:
            def __init__(self):
                self.connected = 0
                self.error = None

            def connect(self):
                self.connected += 1
                return self.error

        tunnel = FakeTunnel()
        conn = FakeHTTPConnection(None, None)
        conn.response = FakeResponse(200, b'{"ok": true}')
        monkeypatch.setattr(ck, "_TunnelHTTPConnection", lambda *a, **kw: conn)
        remote = ck.RemoteOpenChamber(ssh_config)
        remote._tunnel = tunnel
        payload, err = remote._request("status")
        assert payload == {"ok": True}
        assert err is None
        assert tunnel.connected == 1

        tunnel.error = "tunnel down"
        payload, err = remote._request("status")
        assert payload is None
        assert err == "tunnel down"
        assert tunnel.connected == 2


class TestStatus:
    def test_caches_port_and_not_ok(self, monkeypatch, lan_config):
        remote = ck.RemoteOpenChamber(lan_config)
        conn = FakeHTTPConnection(None, None)
        conn.response = FakeResponse(
            200, b'{"ok": true, "port": 4321, "data": {"state": "running"}}'
        )
        fake_http(monkeypatch, conn)
        data, err = remote.status()
        assert data == {"state": "running"}
        assert err is None
        assert remote.resolve_port(3000) == 4321
        assert remote.api_only is False

        conn.response = FakeResponse(500, b'{"ok": false, "error": "nope"}')
        data, err = remote.status()
        assert data is None
        assert err == "nope"

        conn.response = FakeResponse(200, b'{"ok": true, "data": {}}')
        remote._port = None
        remote.status()
        assert remote.resolve_port(3000) == 3000

    def test_surfaces_api_only_flag(self, monkeypatch, lan_config):
        remote = ck.RemoteOpenChamber(lan_config)
        conn = FakeHTTPConnection(None, None)
        conn.response = FakeResponse(
            200,
            b'{"ok": true, "port": 3000, "api_only": true, '
            b'"data": {"state": "running"}}',
        )
        fake_http(monkeypatch, conn)
        data, err = remote.status()
        assert data == {"state": "running"}
        assert err is None
        assert remote.api_only is True


class TestActions:
    def test_ok_and_error(self, monkeypatch, lan_config):
        remote = ck.RemoteOpenChamber(lan_config)
        cases = [
            (200, b'{"ok": true, "message": "started"}', "start", (True, "started")),
            (500, b'{"ok": false, "error": "boom"}', "start", (False, "boom")),
            (500, b'{"ok": false}', "stop", (False, "stop failed")),
            (
                200,
                b'{"ok": true, "message": "done", "updated": true}',
                "update",
                (True, "done", True),
            ),
        ]
        for status, body, method, expected in cases:
            conn = FakeHTTPConnection(None, None)
            conn.response = FakeResponse(status, body)
            fake_http(monkeypatch, conn)
            result = getattr(remote, method)()
            assert result == expected, (method, result)

        conn = FakeHTTPConnection(None, None)
        conn.raise_on_request = OSError("reset")
        fake_http(monkeypatch, conn)
        ok, message, updated = remote.update()
        assert ok is False
        assert updated is None
        assert "agent unreachable" in message


class TestTestRemote:
    def test_ok(self, monkeypatch, lan_config):
        remote = FakeRemote((RUNNING, None))
        monkeypatch.setattr(ck, "RemoteOpenChamber", lambda cfg: remote)
        ok, message = ck.test_remote(lan_config)
        assert ok is True
        assert "Running" in message
        assert "port 3000" in message

    def test_error(self, monkeypatch, lan_config):
        remote = FakeRemote((None, "agent rejected the token (check settings)"))
        monkeypatch.setattr(ck, "RemoteOpenChamber", lambda cfg: remote)
        ok, message = ck.test_remote(lan_config)
        assert ok is False
        assert message == "agent rejected the token (check settings)"

    def test_exception(self, monkeypatch, lan_config):
        class Boom:
            def status(self):
                raise RuntimeError("kaboom")

        monkeypatch.setattr(ck, "RemoteOpenChamber", lambda cfg: Boom())
        ok, message = ck.test_remote(lan_config)
        assert ok is False
        assert message == "kaboom"


class FakeRemote:
    def __init__(self, status_result):
        self.status_result = status_result

    def status(self):
        return self.status_result

    def resolve_port(self, fallback):
        return fallback


class TestTimeout:
    def test_per_method_values(self):
        remote = ck.RemoteOpenChamber(ck.Config({}))
        assert remote._timeout("update") == 240
        assert remote._timeout("restart") == 120
        assert remote._timeout("status") == 30
        assert remote._timeout("start") == 30
        assert remote._timeout("stop") == 30
        assert remote._timeout("bogus") == 30
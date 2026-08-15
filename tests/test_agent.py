import http.client
import importlib.util
import io
import json
import os
import sys
import threading

import pytest

import ChamberKeep as ck

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
AGENT_PATH = os.path.join(REPO_ROOT, "chamberkeep-agent.py")


@pytest.fixture(scope="module")
def agent():
    spec = importlib.util.spec_from_file_location("chamberkeep_agent", AGENT_PATH)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    module.log.disabled = True
    return module


class StubOC:
    def __init__(self):
        self.config = ck.Config({})
        self.calls = []
        self.status_result = ({"state": "stopped", "instances": []}, None)
        self.update_result = (True, "up to date (0.3.0)", False)
        self.raise_on_update = None

    def status(self):
        self.calls.append("status")
        return self.status_result

    def start(self):
        self.calls.append("start")
        return (True, "started")

    def stop(self):
        self.calls.append("stop")
        return (True, "stopped")

    def restart(self):
        self.calls.append("restart")
        return (True, "restarted")

    def update(self):
        self.calls.append("update")
        if self.raise_on_update is not None:
            raise self.raise_on_update
        return self.update_result


@pytest.fixture
def httpd(agent):
    oc = StubOC()
    server = agent.AgentHTTPServer(("127.0.0.1", 0), agent.AgentHandler, oc, "tok123")
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    port = server.server_address[1]
    yield server, oc, port, thread
    server.shutdown()
    server.server_close()
    thread.join(5)


def http_request(port, verb, path, token=None):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    headers = {}
    if token is not None:
        headers["X-ChamberKeep-Token"] = token
    conn.request(verb, path, headers=headers)
    resp = conn.getresponse()
    raw = resp.read()
    conn.close()
    return resp.status, json.loads(raw.decode("utf-8"))


class TestAgentHTTPServer:
    def test_carries_oc_and_token(self, agent):
        oc = object()
        server = agent.AgentHTTPServer(("127.0.0.1", 0), agent.AgentHandler, oc, "t")
        try:
            assert server.oc is oc
            assert server.token == "t"
            assert server.daemon_threads is True
            assert server.allow_reuse_address is True
        finally:
            server.server_close()


class TestAgentEndpoints:
    def test_ping_ok_without_token(self, httpd):
        server, oc, port, thread = httpd
        status, payload = http_request(port, "GET", "/api/ping")
        assert status == 200
        assert payload == {"ok": True}

    def test_deny_without_token(self, httpd):
        server, oc, port, thread = httpd
        for verb, path in (("GET", "/api/status"), ("POST", "/api/start")):
            status, payload = http_request(port, verb, path)
            assert status == 401
            assert payload == {"ok": False, "error": "unauthorized"}

    def test_deny_wrong_token(self, httpd):
        server, oc, port, thread = httpd
        for verb, path in (("GET", "/api/status"), ("POST", "/api/start")):
            status, payload = http_request(port, verb, path, token="wrong")
            assert status == 401
            assert payload == {"ok": False, "error": "unauthorized"}

    def test_status_ok(self, httpd):
        server, oc, port, thread = httpd
        oc.status_result = ({"state": "running", "instances": []}, None)
        status, payload = http_request(port, "GET", "/api/status", token="tok123")
        assert status == 200
        assert payload == {
            "ok": True,
            "data": {"state": "running", "instances": []},
            "port": 3000,
            "api_only": False,
        }
        assert oc.calls == ["status"]

    def test_status_reports_effective_api_only(self, httpd):
        server, oc, port, thread = httpd
        oc.config.set_override("OPENCHAMBER_API_ONLY", True)
        oc.status_result = ({"state": "running", "instances": []}, None)
        status, payload = http_request(port, "GET", "/api/status", token="tok123")
        assert status == 200
        assert payload["api_only"] is True

    def test_status_error_returns_500(self, httpd):
        server, oc, port, thread = httpd
        oc.status_result = (None, "openchamber not found on PATH")
        status, payload = http_request(port, "GET", "/api/status", token="tok123")
        assert status == 500
        assert payload == {"ok": False, "error": "openchamber not found on PATH"}

    def test_unknown_path_returns_404(self, httpd):
        server, oc, port, thread = httpd
        for verb in ("GET", "POST"):
            status, payload = http_request(port, verb, "/api/bogus", token="tok123")
            assert status == 404
            assert payload == {"ok": False, "error": "not found"}

    def test_action_endpoints_ok(self, httpd):
        server, oc, port, thread = httpd
        for path, call, message in (
            ("start", "start", "started"),
            ("stop", "stop", "stopped"),
            ("restart", "restart", "restarted"),
        ):
            status, payload = http_request(
                port, "POST", "/api/" + path, token="tok123"
            )
            assert status == 200
            assert payload == {"ok": True, "message": message}
            assert oc.calls[-1] == call

    def test_update_three_tuple_mapping(self, httpd):
        server, oc, port, thread = httpd
        cases = [
            ((True, "OpenChamber updated 0.2.0 -> 0.3.0", True), 200, {"ok": True, "updated": True, "message": "OpenChamber updated 0.2.0 -> 0.3.0"}),
            ((True, "OpenChamber is up to date (0.3.0)", False), 200, {"ok": True, "updated": False, "message": "OpenChamber is up to date (0.3.0)"}),
            ((False, "update failed", None), 500, {"ok": False, "updated": None, "error": "update failed"}),
        ]
        for result, expected_status, expected_payload in cases:
            oc.update_result = result
            status, payload = http_request(port, "POST", "/api/update", token="tok123")
            assert status == expected_status
            assert payload == expected_payload

    def test_command_exception_returns_500_with_message(self, httpd, agent, monkeypatch):
        server, oc, port, thread = httpd
        monkeypatch.setattr(agent.log, "exception", lambda *a, **kw: None)
        oc.raise_on_update = RuntimeError("registry unreachable")
        status, payload = http_request(port, "POST", "/api/update", token="tok123")
        assert status == 500
        assert payload == {"ok": False, "error": "registry unreachable"}

    def test_shutdown_returns_200_and_stops_server(self, httpd):
        server, oc, port, thread = httpd
        status, payload = http_request(port, "POST", "/api/shutdown", token="tok123")
        assert status == 200
        assert payload == {"ok": True, "message": "agent shutting down"}
        thread.join(5)
        assert not thread.is_alive()


class StubHTTPServer:
    def __init__(self, address, handler, oc, token):
        self.address = address
        self.handler = handler
        self.oc = oc
        self.token = token
        self.closed = False

    def serve_forever(self):
        return None

    def server_close(self):
        self.closed = True


class BoomHTTPServer:
    def __init__(self, address, handler, oc, token):
        raise OSError("address already in use")


class KIStubHTTPServer(StubHTTPServer):
    def serve_forever(self):
        raise KeyboardInterrupt


@pytest.fixture
def agent_main_env(agent, monkeypatch):
    monkeypatch.setattr(agent.logging, "basicConfig", lambda **kw: None)
    monkeypatch.setattr(
        ck.Config,
        "load",
        classmethod(lambda cls, *a, **kw: ck.Config({"agent_port": 8040, "agent_token": "tok"})),
    )
    return agent


class TestAgentMain:
    @pytest.mark.parametrize(
        "argv, expected_address",
        [
            (["chamberkeep-agent.py"], ("127.0.0.1", 8040)),
            (["chamberkeep-agent.py", "--port", "8123"], ("127.0.0.1", 8123)),
            (["chamberkeep-agent.py", "--host", "0.0.0.0"], ("0.0.0.0", 8040)),
            (
                ["chamberkeep-agent.py", "--port", "8123", "--host", "0.0.0.0"],
                ("0.0.0.0", 8123),
            ),
        ],
    )
    def test_binds_expected_address(self, agent_main_env, monkeypatch, argv, expected_address):
        monkeypatch.setattr(sys, "argv", argv)
        monkeypatch.setattr(agent_main_env, "AgentHTTPServer", StubHTTPServer)
        assert agent_main_env.main() == 0

    def test_listen_failure_returns_1(self, agent_main_env, monkeypatch):
        monkeypatch.setattr(sys, "argv", ["chamberkeep-agent.py", "--port", "9999"])
        monkeypatch.setattr(agent_main_env, "AgentHTTPServer", BoomHTTPServer)
        assert agent_main_env.main() == 1

    def test_server_receives_oc_and_token(self, agent_main_env, monkeypatch):
        started = []
        monkeypatch.setattr(sys, "argv", ["chamberkeep-agent.py"])

        def factory(address, handler, oc, token):
            server = StubHTTPServer(address, handler, oc, token)
            started.append(server)
            return server

        monkeypatch.setattr(agent_main_env, "AgentHTTPServer", factory)
        assert agent_main_env.main() == 0
        server = started[0]
        assert server.address == ("127.0.0.1", 8040)
        assert server.handler is agent_main_env.AgentHandler
        assert isinstance(server.oc, ck.LocalOpenChamber)
        assert server.token == "tok"
        assert server.closed is True

    def test_keyboard_interrupt_returns_0_and_closes(self, agent_main_env, monkeypatch):
        started = []
        monkeypatch.setattr(sys, "argv", ["chamberkeep-agent.py"])

        def factory(address, handler, oc, token):
            server = KIStubHTTPServer(address, handler, oc, token)
            started.append(server)
            return server

        monkeypatch.setattr(agent_main_env, "AgentHTTPServer", factory)
        assert agent_main_env.main() == 0
        assert started[0].closed is True

    def test_version_flag_exits_zero(self, agent_main_env, monkeypatch, capsys):
        monkeypatch.setattr(
            sys, "argv", ["chamberkeep-agent.py", "--version"]
        )
        with pytest.raises(SystemExit) as exc_info:
            agent_main_env.main()
        assert exc_info.value.code == 0
        assert "chamberkeep-agent" in capsys.readouterr().out

"""ChamberKeep agent - local controller for remote tray access.

Runs on the same machine as the OpenChamber server and executes the
openchamber CLI *in the interactive user session*. A ChamberKeep tray on
another machine connects to this agent over an SSH tunnel (recommended) or
directly over the LAN and sends JSON commands. Because the agent is a normal
interactive process, the server always runs in the same context and
environment as a locally started one - unlike commands executed through an
SSH session.

Usage:
    python chamberkeep-agent.py                 # 127.0.0.1:<config agent_port>
    python chamberkeep-agent.py --port 8040
    python chamberkeep-agent.py --host 0.0.0.0  # accept direct-LAN connections

The agent binds to 127.0.0.1 by default. Reach it from another PC over an
SSH tunnel (forward to 127.0.0.1:<port>), or use --host 0.0.0.0 to allow
direct LAN access (token-protected).

The access token is generated on first start, printed to the console and
stored in chamberkeep.json. Configure the matching token in the remote
settings of the controlling ChamberKeep.

Endpoints (JSON):
    GET  /api/ping                       -> {"ok": true}
    GET  /api/status                     -> {"ok": true, "data": <status json>, "port": N}
    POST /api/start|stop|restart|update  -> {"ok": true, "message": "..."}
All endpoints except /api/ping require the header "X-ChamberKeep-Token".
"""

import argparse
import hmac
import json
import logging
import os
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SCRIPT_DIR)
from ChamberKeep import Config, LocalOpenChamber  # noqa: E402

AGENT_LOG_PATH = os.path.join(SCRIPT_DIR, "chamberkeep-agent.log")

log = logging.getLogger("chamberkeep-agent")

VERSION = "0.2.0"


class AgentHTTPServer(ThreadingHTTPServer):
    """Threaded HTTP server carrying the OpenChamber backend and token."""

    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address, handler, oc, token):
        self.oc = oc
        self.token = token
        super().__init__(address, handler)


class AgentHandler(BaseHTTPRequestHandler):
    server_version = "ChamberKeepAgent/%s" % VERSION
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        log.info("%s - %s", self.address_string(), fmt % args)

    def _send(self, code, obj):
        body = json.dumps(obj).encode("utf-8")
        try:
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionAbortedError, ConnectionResetError, OSError):
            log.info("client disconnected while sending response")

    def _authorized(self):
        provided = self.headers.get("X-ChamberKeep-Token", "")
        expected = self.server.token
        return bool(provided) and hmac.compare_digest(provided, expected)

    def _deny(self):
        self._send(401, {"ok": False, "error": "unauthorized"})

    def do_GET(self):
        if self.path == "/api/ping":
            self._send(200, {"ok": True})
            return
        if not self._authorized():
            self._deny()
            return
        if self.path == "/api/status":
            data, err = self.server.oc.status()
            if err:
                self._send(500, {"ok": False, "error": err})
                return
            self._send(
                200,
                {
                    "ok": True,
                    "data": data,
                    "port": self.server.oc.config.port,
                },
            )
            return
        self._send(404, {"ok": False, "error": "not found"})

    def do_POST(self):
        if not self._authorized():
            self._deny()
            return
        command = self.server.oc
        handlers = {
            "/api/start": command.start,
            "/api/stop": command.stop,
            "/api/restart": command.restart,
            "/api/update": command.update,
        }
        handler = handlers.get(self.path)
        if handler is None:
            self._send(404, {"ok": False, "error": "not found"})
            return
        try:
            result = handler()
        except Exception as exc:
            log.exception("command %s failed", self.path)
            self._send(500, {"ok": False, "error": str(exc)})
            return
        if self.path == "/api/update":
            ok, message, updated = result
            payload = {"ok": ok, "updated": updated}
        else:
            ok, message = result
            payload = {"ok": ok}
        payload["message" if ok else "error"] = message
        self._send(200 if ok else 500, payload)


def main():
    parser = argparse.ArgumentParser(
        description="ChamberKeep agent - local controller for remote tray access."
    )
    parser.add_argument(
        "--port", type=int, help="listening port (default: config agent_port)"
    )
    parser.add_argument(
        "--host",
        default=None,
        help="bind address (default 127.0.0.1; use 0.0.0.0 only for direct-LAN access)",
    )
    parser.add_argument(
        "--version", action="version", version="chamberkeep-agent %s" % VERSION
    )
    args = parser.parse_args()

    config = Config.load()
    token = config.ensure_agent_token()
    port = args.port or config.agent_port
    host = args.host or "127.0.0.1"

    logging.basicConfig(
        filename=AGENT_LOG_PATH,
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )
    log.info("starting chamberkeep-agent %s on %s:%d", VERSION, host, port)
    print("chamberkeep-agent on %s:%d" % (host, port))
    print("token: %s" % token)

    oc = LocalOpenChamber(config)
    try:
        server = AgentHTTPServer((host, port), AgentHandler, oc, token)
    except OSError as exc:
        log.error("cannot listen on %s:%d: %s", host, port, exc)
        print("cannot listen on %s:%d: %s" % (host, port, exc))
        return 1
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())

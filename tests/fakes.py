import ChamberKeep as ck


class FakeResponse:
    def __init__(self, status=200, body=b"{}"):
        self.status = status
        self._body = body

    def read(self):
        return self._body


class FakeHTTPConnection:
    def __init__(self, host, port, timeout=30):
        self.host = host
        self.port = port
        self.timeout = timeout
        self.request_calls = []
        self.response = FakeResponse()
        self.raise_on_request = None

    def request(self, verb, path, body=None, headers=None):
        if self.raise_on_request is not None:
            raise self.raise_on_request
        self.request_calls.append((verb, path, body, headers))

    def getresponse(self):
        return self.response

    def close(self):
        pass


def fake_http(monkeypatch, conn):
    """Patch ck.http.client.HTTPConnection to return a pre-built fake conn."""

    def factory(host, port, timeout=None):
        conn.host = host
        conn.port = port
        conn.timeout = timeout
        return conn

    monkeypatch.setattr(ck.http.client, "HTTPConnection", factory)
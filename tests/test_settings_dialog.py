import pytest

import ChamberKeep as ck


class FakeVar:
    def __init__(self, value=""):
        self.value = value

    def get(self):
        return self.value

    def set(self, value):
        self.value = value


def make_dialog(**kw):
    defaults = dict(
        port="3000",
        poll="5",
        opcode_port="",
        password="",
        confirm="",
        ssh_port="22",
        remote_agent_port="8040",
        local_agent_port="8040",
        remote_enabled=False,
        remote_mode="ssh",
        remote_host="",
        remote_user="",
        remote_password="",
        remote_token="",
    )
    defaults.update(kw)
    dlg = object.__new__(ck.SettingsDialog)
    dlg.port_var = FakeVar(defaults["port"])
    dlg.poll_var = FakeVar(defaults["poll"])
    dlg.text_vars = {"OPENCODE_PORT": FakeVar(defaults["opcode_port"])}
    dlg.password_vars = {
        "value": FakeVar(defaults["password"]),
        "confirm": FakeVar(defaults["confirm"]),
    }
    dlg.remote_ssh_port_var = FakeVar(defaults["ssh_port"])
    dlg.remote_agent_port_var = FakeVar(defaults["remote_agent_port"])
    dlg.agent_port_var = FakeVar(defaults["local_agent_port"])
    dlg.remote_enabled_var = FakeVar(defaults["remote_enabled"])
    dlg.remote_mode_var = FakeVar(defaults["remote_mode"])
    dlg.remote_host_var = FakeVar(defaults["remote_host"])
    dlg.remote_user_var = FakeVar(defaults["remote_user"])
    dlg.remote_password_var = FakeVar(defaults["remote_password"])
    dlg.remote_token_var = FakeVar(defaults["remote_token"])
    return dlg


@pytest.fixture
def error_box(monkeypatch):
    calls = []
    monkeypatch.setattr(
        ck.messagebox, "showerror", lambda title, msg: calls.append((title, msg))
    )
    return calls


class TestValidate:
    def test_valid_returns_port_poll_remote(self):
        result = make_dialog(
            port="8080",
            poll="7",
            opcode_port="11434",
            remote_enabled=True,
            remote_mode="lan",
            remote_host="10.0.0.5",
            remote_user="bob",
            ssh_port="2222",
            remote_agent_port="9000",
            local_agent_port="9001",
            remote_password="pw",
            remote_token="tok",
        )._validate()
        port, poll, remote = result
        assert (port, poll) == (8080, 7)
        assert remote == {
            "enabled": True,
            "mode": "lan",
            "host": "10.0.0.5",
            "user": "bob",
            "ssh_port": 2222,
            "agent_port": 9000,
            "local_agent_port": 9001,
            "password": "pw",
            "token": "tok",
        }

    @pytest.mark.parametrize("bad", ["0", "65536", "-1", "abc", ""])
    def test_port_out_of_range_rejected(self, error_box, bad):
        assert make_dialog(port=bad)._validate() is None
        assert error_box == [
            ("Invalid setting", "Port must be a number 1-65535.")
        ]

    @pytest.mark.parametrize("bad", ["0", "-3", "abc"])
    def test_poll_must_be_positive(self, error_box, bad):
        assert make_dialog(poll=bad)._validate() is None
        assert error_box == [
            ("Invalid setting", "Poll interval must be a positive number.")
        ]

    @pytest.mark.parametrize("bad", ["0", "65536", "abc"])
    def test_opencode_port_range(self, error_box, bad):
        assert make_dialog(opcode_port=bad)._validate() is None
        assert error_box == [
            ("Invalid setting", "OpenCode port must be a number 1-65535.")
        ]

    def test_opencode_port_optional_when_empty(self, error_box):
        port, poll, remote = make_dialog(opcode_port="")._validate()
        assert (port, poll) == (3000, 5)
        assert error_box == []

    def test_password_mismatch_rejected(self, error_box):
        assert make_dialog(password="secret", confirm="other")._validate() is None
        assert error_box == [
            ("Invalid setting", "The password entries do not match.")
        ]

    @pytest.mark.parametrize("bad", ["0", "65536", "abc"])
    def test_ssh_port_range(self, error_box, bad):
        assert make_dialog(ssh_port=bad)._validate() is None
        assert error_box == [
            ("Invalid setting", "SSH port must be a number 1-65535.")
        ]

    @pytest.mark.parametrize("bad", ["0", "65536"])
    def test_remote_agent_port_range(self, error_box, bad):
        assert make_dialog(remote_agent_port=bad)._validate() is None
        assert error_box == [
            ("Invalid setting", "Agent port must be a number 1-65535.")
        ]

    @pytest.mark.parametrize("bad", ["0", "65536"])
    def test_local_agent_port_range(self, error_box, bad):
        assert make_dialog(local_agent_port=bad)._validate() is None
        assert error_box == [
            ("Invalid setting", "Local agent port must be a number 1-65535.")
        ]

    def test_remote_host_required_when_enabled(self, error_box):
        assert make_dialog(remote_enabled=True, remote_host="")._validate() is None
        assert error_box == [
            (
                "Invalid setting",
                "Remote host is required when remote control is enabled.",
            )
        ]

    def test_ssh_user_required_in_ssh_mode(self, error_box):
        dlg = make_dialog(
            remote_enabled=True, remote_mode="ssh", remote_host="10.0.0.1"
        )
        assert dlg._validate() is None
        assert error_box == [
            ("Invalid setting", "SSH user is required for SSH tunnel mode.")
        ]

    def test_lan_mode_without_user_is_valid(self, error_box):
        dlg = make_dialog(
            remote_enabled=True,
            remote_mode="lan",
            remote_host="10.0.0.1",
            remote_user="",
        )
        port, poll, remote = dlg._validate()
        assert (port, poll) == (3000, 5)
        assert remote["mode"] == "lan"
        assert remote["user"] == ""
        assert error_box == []
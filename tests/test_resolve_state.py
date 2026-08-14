import pytest

import ChamberKeep as ck

RUNNING = {
    "status": "ok",
    "state": "running",
    "runningCount": 1,
    "instances": [
        {
            "runtime": "cli",
            "port": 3000,
            "pid": 25136,
            "launchMode": "daemon",
            "passwordProtected": True,
        }
    ],
}


class TestResolveState:
    def test_running_on_port(self):
        state, info = ck.resolve_state(RUNNING, 3000)
        assert state == "running"
        assert "port 3000" in info
        assert "pid 25136" in info
        assert "daemon" in info
        assert "password protected" in info

    def test_stopped_and_other_states(self):
        assert ck.resolve_state({"state": "stopped", "instances": []}, 3000) == (
            "stopped",
            "no server running on port 3000",
        )
        assert ck.resolve_state({"state": "installing"}, 3000)[0] == "stopped"

    def test_error_for_missing_data(self):
        assert ck.resolve_state(None, 3000)[0] == "error"
        assert ck.resolve_state(["not", "a", "dict"], 3000) == ("error", "no status data")

    def test_running_without_instances(self):
        state, info = ck.resolve_state({"state": "running"}, 3000)
        assert state == "ambiguous"
        assert "running: none" in info

    def test_ambiguous_on_other_port(self):
        state, info = ck.resolve_state(RUNNING, 3001)
        assert state == "ambiguous"
        assert "running: 3000" in info

    def test_multiple_instances_on_port(self):
        data = {"state": "running", "instances": [{"port": 3000}, {"port": 3000}]}
        state, info = ck.resolve_state(data, 3000)
        assert state == "ambiguous"
        assert "2 instances on port 3000" in info

    def test_port_type_mismatch(self):
        assert ck.resolve_state(RUNNING, "3000")[0] == "ambiguous"
        data = {"state": "running", "instances": [{"port": "3000"}]}
        assert ck.resolve_state(data, 3000)[0] == "ambiguous"

    @pytest.mark.xfail(
        strict=True,
        reason="BUG: instances containing None crash with AttributeError "
        "('NoneType' object has no attribute 'get')",
    )
    def test_none_instance_entry(self):
        data = {"state": "running", "instances": [None, {"port": 3000}]}
        assert ck.resolve_state(data, 3000)[0] == "running"

    @pytest.mark.xfail(
        strict=True,
        reason="BUG: non-dict instance entries crash with AttributeError",
    )
    def test_non_dict_instance_entry(self):
        data = {"state": "running", "instances": ["oops", {"port": 3000}]}
        assert ck.resolve_state(data, 3000)[0] == "running"
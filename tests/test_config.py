import json
import os

import pytest

import ChamberKeep as ck


class TestParseBool:
    def test_truthy_and_falsy(self):
        for value in ("1", "true", "True", " TRUE ", "yes", "on"):
            assert ck._parse_bool(value) is True
        for value in ("0", "false", "no", "off", "", None, "banana"):
            assert ck._parse_bool(value) is False


class TestConfigDefaults:
    def test_defaults(self, clean_env):
        with clean_env():
            cfg = ck.Config({})
            assert cfg.port == 3000
            assert cfg.agent_port == 8040
            assert cfg.remote_ssh_port == 22
            assert cfg.remote_agent_port == 8040
            assert cfg.poll_seconds == 5
            assert cfg.auto_start_server is False
            assert cfg.start_with_windows is False
            assert cfg.start_agent is False
            assert cfg.remote_enabled is False
            assert cfg.remote_mode == "ssh"
            assert cfg.agent_token == ""
            assert cfg.cli_port is None

    def test_precedence(self, clean_env):
        with clean_env():
            cfg = ck.Config({})
            assert cfg.effective_value("OPENCHAMBER_HOST") == "127.0.0.1"
            assert cfg.effective_value("OPENCHAMBER_API_ONLY") is False
            assert cfg.effective_value("OPENCHAMBER_OPENCODE_HOSTNAME") == "127.0.0.1"
            assert cfg.env_source("OPENCHAMBER_HOST") == "default"

        with clean_env({"OPENCHAMBER_HOST": "0.0.0.0"}):
            cfg = ck.Config({})
            assert cfg.effective_value("OPENCHAMBER_HOST") == "0.0.0.0"
            assert cfg.env_source("OPENCHAMBER_HOST") == "env"
            cfg.set_override("OPENCHAMBER_HOST", "10.0.0.5")
            assert cfg.effective_value("OPENCHAMBER_HOST") == "10.0.0.5"
            assert cfg.env_source("OPENCHAMBER_HOST") == "override"
            cfg.remove_override("OPENCHAMBER_HOST")
            assert cfg.env_source("OPENCHAMBER_HOST") == "env"

        with clean_env({"OPENCHAMBER_HOST": ""}):
            cfg = ck.Config({})
            assert cfg.effective_value("OPENCHAMBER_HOST") == "127.0.0.1"
            assert cfg.env_source("OPENCHAMBER_HOST") == "default"

        with clean_env({"OPENCHAMBER_API_ONLY": "true"}):
            cfg = ck.Config({})
            assert cfg.effective_value("OPENCHAMBER_API_ONLY") is True
        with clean_env({"OPENCHAMBER_API_ONLY": "0"}):
            cfg = ck.Config({})
            assert cfg.effective_value("OPENCHAMBER_API_ONLY") is False


class TestBuildServeEnv:
    def test_normalizes_values(self, clean_env):
        with clean_env({"OPENCHAMBER_UI_PASSWORD": "s3cret", "OPENCODE_SKIP_START": "1"}):
            cfg = ck.Config({})
            env = cfg.build_serve_env()
            assert env.get("OPENCHAMBER_UI_PASSWORD") == "s3cret"
            assert env.get("OPENCHAMBER_HOST") == "127.0.0.1"
            assert env.get("OPENCHAMBER_API_ONLY") == "false"
            assert env.get("OPENCODE_SKIP_START") == "true"
            assert not env.get("OPENCHAMBER_DATA_DIR")
            assert not env.get("OPENCODE_PORT")

        with clean_env():
            cfg = ck.Config({})
            cfg.set_override("OPENCODE_SKIP_START", True)
            assert cfg.build_serve_env().get("OPENCODE_SKIP_START") == "true"


class TestSaveLoad:
    def test_roundtrip(self, tmp_path, clean_env):
        with clean_env():
            path = str(tmp_path / "chamberkeep.json")
            cfg = ck.Config({})
            cfg.port = 3001
            cfg.auto_start_server = True
            cfg.set_override("OPENCHAMBER_HOST", "0.0.0.0")
            cfg.set_override("OPENCODE_HOST", "http://host:4096")
            cfg.set_override("OPENCODE_PORT", "")
            cfg.save(path)
            loaded = ck.Config.load(path)
            assert loaded.port == 3001
            assert loaded.auto_start_server is True
            assert loaded.effective_value("OPENCHAMBER_HOST") == "0.0.0.0"
            assert loaded.effective_value("OPENCODE_HOST") == "http://host:4096"
            assert "OPENCODE_PORT" not in loaded.env_overrides

    def test_saved_schema(self, tmp_path, clean_env):
        with clean_env():
            path = str(tmp_path / "chamberkeep.json")
            ck.Config({}).save(path)
            with open(path, encoding="utf-8") as f:
                raw = json.load(f)
            assert set(raw) == {
                "port", "auto_start_server", "start_with_windows", "poll_seconds",
                "start_agent", "agent_port", "agent_token",
                "remote_enabled", "remote_mode", "remote_host", "remote_user",
                "remote_ssh_port", "remote_agent_port", "remote_password",
                "env_overrides",
            }

    def test_load_missing_and_corrupt(self, tmp_path):
        assert ck.Config.load(str(tmp_path / "nope.json")).port == 3000
        path = tmp_path / "chamberkeep.json"
        path.write_text("{ this is not json", encoding="utf-8")
        cfg = ck.Config.load(str(path))
        assert cfg.port == 3000
        assert cfg.remote_mode == "ssh"

    def test_load_non_int_port_falls_back(self, tmp_path):
        path = tmp_path / "chamberkeep.json"
        path.write_text(
            json.dumps({"port": "not-a-number", "poll_seconds": "nope"}),
            encoding="utf-8",
        )
        cfg = ck.Config.load(str(path))
        assert cfg.port == 3000
        assert cfg.poll_seconds == 5

    @pytest.mark.xfail(
        strict=True,
        reason="BUG: Config.load crashes with AttributeError when the JSON "
        "is valid but not a dict",
    )
    def test_load_top_level_list_falls_back(self, tmp_path):
        path = tmp_path / "chamberkeep.json"
        path.write_text("[1, 2]", encoding="utf-8")
        cfg = ck.Config.load(str(path))
        assert cfg.port == 3000


class TestEnsureAgentToken:
    def test_generates_persists_and_is_stable(self, tmp_path):
        path = str(tmp_path / "chamberkeep.json")
        cfg = ck.Config({}, path=path)
        token = cfg.ensure_agent_token()
        assert len(token) == 32
        assert cfg.agent_token == token
        assert cfg.ensure_agent_token() == token
        with open(path, encoding="utf-8") as f:
            assert json.load(f)["agent_token"] == token

    def test_existing_token_not_overwritten(self, tmp_path):
        path = str(tmp_path / "chamberkeep.json")
        cfg = ck.Config({"agent_token": "pre-set"}, path=path)
        assert cfg.ensure_agent_token() == "pre-set"
        assert not os.path.exists(path)


class TestApplyEnvOverride:
    def test_bool_branch(self, clean_env):
        with clean_env():
            cfg = ck.Config({})
            ck.apply_env_override(cfg, "OPENCODE_SKIP_START", False)
            assert cfg.env_source("OPENCODE_SKIP_START") == "default"
            ck.apply_env_override(cfg, "OPENCODE_SKIP_START", True)
            assert cfg.env_source("OPENCODE_SKIP_START") == "override"
            assert cfg.effective_value("OPENCODE_SKIP_START") is True

        with clean_env({"OPENCHAMBER_API_ONLY": "true"}):
            cfg = ck.Config({})
            ck.apply_env_override(cfg, "OPENCHAMBER_API_ONLY", True)
            assert cfg.env_source("OPENCHAMBER_API_ONLY") == "env"

        with clean_env():
            cfg = ck.Config({})
            ck.apply_env_override(cfg, "OPENCODE_SKIP_START", True, bool_value=False)
            assert cfg.env_source("OPENCODE_SKIP_START") == "default"
            ck.apply_env_override(cfg, "OPENCODE_SKIP_START", True, bool_value=True)
            assert cfg.env_source("OPENCODE_SKIP_START") == "override"
            assert cfg.effective_value("OPENCODE_SKIP_START") is True

    def test_text_branch(self, clean_env):
        with clean_env({"OPENCHAMBER_UI_PASSWORD": "s3cret"}):
            cfg = ck.Config({})
            ck.apply_env_override(cfg, "OPENCHAMBER_UI_PASSWORD", "s3cret")
            assert cfg.env_source("OPENCHAMBER_UI_PASSWORD") == "env"
            ck.apply_env_override(cfg, "OPENCHAMBER_UI_PASSWORD", "newpass")
            assert cfg.env_source("OPENCHAMBER_UI_PASSWORD") == "override"
            ck.apply_env_override(cfg, "OPENCHAMBER_UI_PASSWORD", "")
            assert "OPENCHAMBER_UI_PASSWORD" not in cfg.env_overrides

        with clean_env():
            cfg = ck.Config({})
            ck.apply_env_override(cfg, "OPENCHAMBER_OPENCODE_HOSTNAME", "127.0.0.1")
            assert cfg.env_source("OPENCHAMBER_OPENCODE_HOSTNAME") == "default"
            ck.apply_env_override(cfg, "OPENCHAMBER_OPENCODE_HOSTNAME", "0.0.0.0")
            assert cfg.env_source("OPENCHAMBER_OPENCODE_HOSTNAME") == "override"
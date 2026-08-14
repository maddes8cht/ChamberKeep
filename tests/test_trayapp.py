import time

import pytest

import ChamberKeep as ck


class FakeIcon:
    def __init__(self):
        self.icon = None
        self.title = ""
        self.menu = None
        self.menu_updates = 0
        self.notifications = []

    def update_menu(self):
        self.menu_updates += 1


class FakeOC:
    def __init__(self, status_result=({"state": "stopped", "instances": []}, None)):
        self.status_result = status_result
        self.start_calls = 0

    def status(self):
        return self.status_result

    def resolve_port(self, fallback):
        return fallback

    def start(self):
        self.start_calls += 1
        return True, "Server start requested on port 3000"


@pytest.fixture
def app(monkeypatch):
    monkeypatch.setattr(ck, "agent_running", lambda port: False)
    app = ck.TrayApp(ck.Config({}))
    app.oc = FakeOC()
    app.icon = FakeIcon()
    app.create_icon = lambda state: "icon-%s" % state
    app.build_menu = lambda: None
    app.notify = lambda msg, title: app.icon.notifications.append((msg, title))
    return app


RUNNING_STATUS = {"state": "running", "instances": [{"port": 3000}]}


class TestEnterUpdateMode:
    def test_flags_and_updating_display(self, app):
        app.state = "running"
        app._enter_update_mode()
        assert app._update_mode is True
        assert app._update_was_running is True
        assert app._update_normal_state == "running"
        assert app._update_stable == 0
        assert app._update_timeout_warned is False

        app._refresh_during_update({"state": "stopped", "instances": []}, None)
        assert "Updating" in app.icon.title
        assert app._update_stable == 0

        app.state = "unknown"
        app._enter_update_mode()
        assert app._update_was_running is False
        assert app._update_normal_state == "stopped"


class TestRefreshDuringUpdate:
    def test_stable_polls_finish_update_mode(self, app):
        app._enter_update_mode()
        app._update_command_done = True
        app._update_was_running = False
        app._refresh_during_update(RUNNING_STATUS, None)
        assert app._update_stable == 1
        assert app._update_mode is True

        app._refresh_during_update(RUNNING_STATUS, None)
        assert app._update_mode is False
        assert app.state == "running"
        assert app._update_stable == 0

    def test_error_resets_stability(self, app):
        app._enter_update_mode()
        app._update_command_done = True
        app._refresh_during_update(RUNNING_STATUS, None)
        app._refresh_during_update(None, "boom")
        assert app._update_stable == 0
        assert app._update_mode is True

    def test_restart_schedule_only_when_was_running(self, app, monkeypatch):
        scheduled = []
        monkeypatch.setattr(
            ck.TrayApp, "_schedule_start_after_update",
            lambda self: scheduled.append(True),
        )
        app._enter_update_mode()
        app._update_command_done = True
        app._update_was_running = True
        app._refresh_during_update(RUNNING_STATUS, None)
        app._refresh_during_update(RUNNING_STATUS, None)
        assert app._update_mode is True
        app._refresh_during_update(RUNNING_STATUS, None)
        assert scheduled == []
        app._refresh_during_update(RUNNING_STATUS, None)
        assert scheduled == [True]
        assert app._update_restart_done is True

        app._enter_update_mode()
        app._update_command_done = True
        app._update_was_running = False
        app._refresh_during_update(RUNNING_STATUS, None)
        app._refresh_during_update(RUNNING_STATUS, None)
        assert scheduled == [True]
        assert app._update_mode is False

    def test_timeout_warns_once_and_finish_resets(self, app):
        app._enter_update_mode()
        app._update_command_done = True
        app._update_start = time.monotonic() - ck.UPDATE_TIMEOUT - 10
        app._update_was_running = False
        app._refresh_during_update(RUNNING_STATUS, None)
        assert app._update_timeout_warned is True
        assert len(app.icon.notifications) == 1
        assert "Update is taking longer" in app.icon.notifications[0][0]
        assert app._update_mode is False

        app._update_stable = 5
        app._update_extra = 1
        app._update_restart_done = True
        app._finish_update("running", "port 3000")
        assert app._update_mode is False
        assert app._update_command_done is False
        assert app._update_stable == 0
        assert app._update_extra == 0
        assert app._update_restart_done is False
        assert app.state == "running"
        assert app.status_info == "port 3000"
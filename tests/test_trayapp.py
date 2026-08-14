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


class TestDoStart:
    @pytest.mark.parametrize(
        "status_result, expected_ok, expected_msg, expected_starts",
        [
            (
                (RUNNING_STATUS, None),
                False,
                "Server already running on port 3000",
                0,
            ),
            (
                ({"state": "stopped", "instances": []}, None),
                True,
                "Server start requested on port 3000",
                1,
            ),
            (
                (None, "openchamber not found on PATH"),
                True,
                "Server start requested on port 3000",
                1,
            ),
        ],
    )
    def test_cases(
        self, app, monkeypatch, status_result, expected_ok, expected_msg, expected_starts
    ):
        monkeypatch.setattr(ck.time, "sleep", lambda s: None)
        app.oc.status_result = status_result
        ok, message = app.do_start()
        assert ok is expected_ok
        assert message == expected_msg
        assert app.oc.start_calls == expected_starts


class TestDoUpdate:
    @pytest.mark.parametrize(
        "update_result, expected_ok, expected_msg, cmd_done, update_mode",
        [
            ((True, "updated 0.2.0 -> 0.3.0", True), True, "updated 0.2.0 -> 0.3.0", True, True),
            ((True, "up to date (0.3.0)", False), True, "up to date (0.3.0)", False, False),
            ((False, "boom", None), False, "boom", False, False),
        ],
    )
    def test_cases(
        self, app, update_result, expected_ok, expected_msg, cmd_done, update_mode
    ):
        app.oc.update = lambda: update_result
        app._update_mode = True
        app._update_command_done = False
        ok, message = app.do_update()
        assert ok is expected_ok
        assert message == expected_msg
        assert app._update_command_done is cmd_done
        assert app._update_mode is update_mode


def _boom():
    raise RuntimeError("kaboom")


class TestScheduleStartAfterUpdate:
    class SyncThread:
        def __init__(self, target, daemon=None):
            self.target = target

        def start(self):
            self.target()

    @pytest.mark.parametrize(
        "status_result, status_raises, expected_starts, expected_notify",
        [
            ((RUNNING_STATUS, None), False, 0, 0),
            (({"state": "stopped", "instances": []}, None), False, 1, 1),
            ((None, None), True, 0, 0),
        ],
    )
    def test_cases(
        self, app, monkeypatch, status_result, status_raises, expected_starts, expected_notify
    ):
        monkeypatch.setattr(ck.threading, "Thread", self.SyncThread)
        app.oc.status_result = status_result
        if status_raises:
            app.oc.status = _boom
        app._schedule_start_after_update()
        assert app.oc.start_calls == expected_starts
        assert len(app.icon.notifications) == expected_notify
        if expected_notify:
            assert app.icon.notifications[0][0] == (
                "Server restarted after update (port 3000)."
            )

    @pytest.mark.xfail(
        strict=True,
        reason="BUG: _schedule_start_after_update ignores the oc.start() "
        "result and always shows the 'Server restarted after update' success "
        "notification, even when the start request fails",
    )
    def test_start_failure_shows_error_notification(self, app, monkeypatch):
        monkeypatch.setattr(ck.threading, "Thread", self.SyncThread)
        app.oc.status_result = ({"state": "stopped", "instances": []}, None)
        starts = []
        app.oc.start = lambda: starts.append(1) or (False, "port 3000 in use")
        app._schedule_start_after_update()
        assert starts == [1]
        assert app.icon.notifications == [
            (
                "Server restart after update failed (port 3000): port 3000 in use",
                "ChamberKeep - Error",
            )
        ]

    def test_start_exception_swallowed(self, app, monkeypatch):
        monkeypatch.setattr(ck.threading, "Thread", self.SyncThread)
        app.oc.status_result = ({"state": "stopped", "instances": []}, None)
        app.oc.start = _boom
        app._schedule_start_after_update()
        assert app.icon.notifications == []


class TestRefresh:
    def test_error_state_mapping(self, app):
        app.oc.status_result = (None, "connection refused")
        app.refresh()
        assert app.state == "error"
        assert app.status_info == "connection refused"

    def test_first_refresh_notify_suppressed(self, app):
        app.oc.status_result = (RUNNING_STATUS, None)
        app.refresh()
        assert app.icon.notifications == []
        app.oc.status_result = ({"state": "stopped", "instances": []}, None)
        app.refresh()
        assert app.icon.notifications == [
            ("no server running on port 3000", "Stopped")
        ]

    def test_local_mode_polls_agent(self, app, monkeypatch):
        calls = []
        monkeypatch.setattr(
            ck, "agent_running", lambda port: calls.append(port) or True
        )
        app.refresh()
        assert calls == [8040]
        assert app.agent_running is True

    def test_remote_mode_skips_agent_poll(self, app, monkeypatch):
        calls = []
        monkeypatch.setattr(
            ck, "agent_running", lambda port: calls.append(port) or True
        )
        app.config.remote_enabled = True
        app.config.remote_host = "10.0.0.1"
        app.refresh()
        assert calls == []
        assert app.agent_running is False


class TestTask:
    @pytest.mark.parametrize(
        "fn, expected_notify",
        [
            (lambda: (True, "Server started"), ("Server started", "ChamberKeep - Success")),
            (_boom, ("kaboom", "ChamberKeep - Error")),
        ],
    )
    def test_success_and_error(self, app, monkeypatch, fn, expected_notify):
        refreshes = []
        monkeypatch.setattr(app, "refresh", lambda: refreshes.append(1))
        app._task(fn)
        assert refreshes == [1]
        assert app.icon.notifications == [expected_notify]


class FakeMenuItem:
    def __init__(self, text, action=None, enabled=True):
        self.text = text
        self.action = action
        self.enabled = enabled


class FakeMenu:
    SEPARATOR = object()

    def __init__(self, *items):
        self.items = items


class FakePystray:
    def __init__(self):
        self.MenuItem = FakeMenuItem
        self.Menu = FakeMenu


@pytest.fixture
def menu_app(monkeypatch):
    monkeypatch.setattr(ck, "pystray", FakePystray())
    monkeypatch.setattr(ck, "agent_running", lambda port: False)
    app = ck.TrayApp(ck.Config({}))
    app.oc = None
    return app


def menu_items(menu):
    return [item for item in menu.items if isinstance(item, FakeMenuItem)]


def menu_texts(menu):
    return [item.text for item in menu_items(menu)]


def find_item(menu, text):
    return next(item for item in menu_items(menu) if item.text == text)


class TestBuildMenu:
    def test_normal_status_text(self, menu_app):
        menu_app.state = "running"
        menu_app.status_info = "port 3000, pid 123"
        menu = menu_app.build_menu()
        texts = menu_texts(menu)
        assert "Status: Running - port 3000, pid 123" in texts
        assert "Target: local" in texts

    def test_updating_status_text(self, menu_app):
        menu_app._update_mode = True
        menu = menu_app.build_menu()
        assert "Status: Updating..." in menu_texts(menu)

    def test_action_items_disabled_during_update(self, menu_app):
        menu_app._update_mode = True
        menu = menu_app.build_menu()
        for text in ("Start Server", "Stop Server", "Restart Server", "Update"):
            assert find_item(menu, text).enabled is False

    def test_action_items_enabled_normally(self, menu_app):
        menu = menu_app.build_menu()
        for text in ("Start Server", "Stop Server", "Restart Server", "Update"):
            assert find_item(menu, text).enabled is True
        assert find_item(menu, "Start Server").action.__self__ is menu_app
        assert find_item(menu, "Start Server").action.__func__ is ck.TrayApp.on_start
        assert find_item(menu, "Update").action.__self__ is menu_app

    def test_agent_section_local_states(self, menu_app):
        menu = menu_app.build_menu()
        assert "Agent: Stopped" in menu_texts(menu)
        assert find_item(menu, "Start Agent").enabled is True
        assert find_item(menu, "Stop Agent").enabled is False

        menu_app.agent_running = True
        menu = menu_app.build_menu()
        assert "Agent: Running" in menu_texts(menu)
        assert find_item(menu, "Start Agent").enabled is False
        assert find_item(menu, "Stop Agent").enabled is True

    def test_agent_section_hidden_in_remote_mode(self, menu_app):
        menu_app.config.remote_enabled = True
        menu_app.config.remote_host = "10.0.0.1"
        texts = menu_texts(menu_app.build_menu())
        assert "Target: remote 10.0.0.1" in texts
        assert not any(t.startswith("Agent:") or t.endswith("Agent") for t in texts)
        assert find_item(menu_app.build_menu(), "Settings...") is not None
import contextlib
import os
import sys

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

import pytest


@pytest.fixture
def clean_env(monkeypatch):
    """Context manager factory that clears ChamberKeep env vars for a block."""

    def factory(overrides=None):
        @contextlib.contextmanager
        def _ctx():
            import ChamberKeep as ck

            for key in ck.ENV_DEFAULTS:
                monkeypatch.delenv(key, raising=False)
            for key, value in (overrides or {}).items():
                monkeypatch.setenv(key, value)
            yield

        return _ctx()

    return factory
"""settled_state: Docker's OOMKilled flag can trail the exit (found by scripts/lab_smoke.py)."""

import time

from labflux.runtime import settled_state


class FakeContainer:
    """Exited container whose State changes to `later` after `flag_after` reloads."""

    def __init__(self, exit_code, *, oom=False, flag_after=None):
        self.reloads = 0
        self.flag_after = flag_after
        self.attrs = {"State": {"Running": False, "ExitCode": exit_code, "OOMKilled": oom}}

    def reload(self):
        self.reloads += 1
        if self.flag_after is not None and self.reloads >= self.flag_after:
            self.attrs = {"State": {**self.attrs["State"], "OOMKilled": True}}


def test_a_late_oom_flag_is_picked_up():
    container = FakeContainer(137, flag_after=3)
    assert settled_state(container)["OOMKilled"] is True
    assert container.reloads == 3


def test_a_plain_sigkill_is_reported_after_the_settle_time():
    container = FakeContainer(137)
    started = time.monotonic()
    state = settled_state(container, timeout=0.3)
    assert state["OOMKilled"] is False and state["ExitCode"] == 137
    assert 0.3 <= time.monotonic() - started < 1.0 and container.reloads >= 2


def test_other_exits_and_marked_kills_are_not_re_read():
    for container in (FakeContainer(0), FakeContainer(1), FakeContainer(137, oom=True)):
        settled_state(container)
        assert container.reloads == 0

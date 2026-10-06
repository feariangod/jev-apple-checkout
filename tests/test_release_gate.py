from datetime import datetime, timedelta
from run_at_release import launch_state


def test_release_gate_never_starts_early_or_catches_up_after_window():
    release = datetime.fromisoformat('2026-10-16T20:00:00+08:00')
    assert launch_state(release - timedelta(days=1), release) == 'too_early'
    assert launch_state(release - timedelta(minutes=10), release) == 'wait'
    assert launch_state(release - timedelta(microseconds=1), release) == 'wait'
    assert launch_state(release, release) == 'ready'
    assert launch_state(release + timedelta(minutes=29), release) == 'ready'
    assert launch_state(release + timedelta(minutes=30), release) == 'expired'

from scripts.shadow_poller import Backoff


def test_backoff_starts_at_base():
    b = Backoff(base=60.0, jitter=False)
    assert b.current() == 60.0


def test_backoff_doubles_on_rate_limit():
    b = Backoff(base=60.0, cap=600.0, jitter=False)
    b.on_rate_limit()
    assert b.current() == 120.0
    b.on_rate_limit()
    assert b.current() == 240.0


def test_backoff_caps():
    b = Backoff(base=60.0, cap=300.0, jitter=False)
    for _ in range(10):
        b.on_rate_limit()
    assert b.current() == 300.0


def test_backoff_resets_on_success():
    b = Backoff(base=60.0, jitter=False)
    b.on_rate_limit()
    b.on_rate_limit()
    b.on_success()
    assert b.current() == 60.0


def test_backoff_jitter_within_range():
    b = Backoff(base=60.0, jitter=True)
    # Sample many to confirm jitter is in [0.8, 1.2] of base
    samples = [b.current() for _ in range(50)]
    assert all(60.0 * 0.8 <= s <= 60.0 * 1.2 for s in samples)

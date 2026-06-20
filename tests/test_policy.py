from datetime import datetime, timedelta

from src.core import RecoveryAttempt, may_retrigger

NOW = datetime(2026, 6, 14, 12, 0, 0)


def test_allows_when_no_recent_attempts():
    d = may_retrigger("fivetran", "c1", [], NOW)
    assert d.allowed is True


def test_blocks_within_min_interval():
    attempts = [RecoveryAttempt("fivetran", "c1", NOW - timedelta(minutes=30))]
    d = may_retrigger("fivetran", "c1", attempts, NOW, min_interval_hours=1)
    assert d.allowed is False


def test_blocks_when_daily_cap_reached():
    attempts = [
        RecoveryAttempt("fivetran", "c1", NOW - timedelta(hours=h))
        for h in (2, 4, 6, 8)
    ]
    d = may_retrigger("fivetran", "c1", attempts, NOW, max_attempts_per_day=4)
    assert d.allowed is False


def test_other_loader_attempts_do_not_count():
    attempts = [RecoveryAttempt("airflow", "dag1", NOW - timedelta(minutes=5))]
    d = may_retrigger("fivetran", "c1", attempts, NOW)
    assert d.allowed is True

"""The month of the plans' hours (TISMA_USAGE_UTC_OFFSET): UTC by default, or the calendar month of a fixed offset."""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from tisma_cloud.models import month_of, month_start
from tisma_cloud.settings import Settings, utc_offset


def test_offsets_that_are_read_and_refused():
    assert utc_offset("") == utc_offset("UTC") == utc_offset("+00:00") == timedelta(0)
    assert utc_offset("-03:00") == utc_offset("-3") == utc_offset("UTC-03:00") == timedelta(hours=-3)
    assert utc_offset("+05:30") == timedelta(hours=5, minutes=30)
    for bad in ("America/Montevideo", "-15:00", "+03:60", "3"):
        with pytest.raises(ValueError, match="TISMA_USAGE_UTC_OFFSET"):
            utc_offset(bad)


def test_utc_by_default(monkeypatch):
    monkeypatch.delenv("TISMA_USAGE_UTC_OFFSET", raising=False)
    assert month_of(datetime(2026, 12, 1, 1, 0)) == "2026-12"
    assert month_start(datetime(2026, 12, 15, 9, 0)) == datetime(2026, 12, 1)


def test_uruguay_midnight(monkeypatch):
    """With -03:00 the month changes at 03:00 UTC: 1 a.m. UTC of the 1st is still the last month in Montevideo."""
    monkeypatch.setenv("TISMA_USAGE_UTC_OFFSET", "-03:00")
    assert month_of(datetime(2026, 12, 1, 1, 0)) == "2026-11"
    assert month_of(datetime(2026, 12, 1, 3, 0)) == "2026-12"
    assert month_of(datetime(2027, 1, 1, 2, 59)) == "2026-12"
    assert month_start(datetime(2026, 12, 15, 9, 0)) == datetime(2026, 12, 1, 3, 0)
    assert month_start(datetime(2026, 12, 1, 1, 0)) == datetime(2026, 11, 1, 3, 0)


def test_a_wrong_offset_stops_the_master(monkeypatch):
    monkeypatch.setenv("TISMA_USAGE_UTC_OFFSET", "America/Montevideo")
    with pytest.raises(ValueError, match="TISMA_USAGE_UTC_OFFSET"):
        Settings()


def test_hours_are_charged_in_the_month_of_the_offset(client, f, monkeypatch):
    """A job that ends at 01:00 UTC of the 1st is charged to the month before in Montevideo."""
    from sqlalchemy import select

    from tisma_cloud import scheduler
    from tisma_cloud.models import Job, Usage

    monkeypatch.setenv("TISMA_USAGE_UTC_OFFSET", "-03:00")
    user = f.user()
    f.submit(user)
    ended = datetime(2026, 12, 1, 1, 0)
    with f.db.session() as s:
        job = s.execute(select(Job)).scalar_one()
        job.started_at, job.finished_at = ended - timedelta(minutes=10), ended
        scheduler.charge_time(s, job)
    with f.db.session() as s:
        assert [(u.month, round(u.seconds)) for u in s.execute(select(Usage)).scalars()] == [("2026-11", 600)]

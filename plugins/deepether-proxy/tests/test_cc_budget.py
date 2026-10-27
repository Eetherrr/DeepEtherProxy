"""金额预算：滚动窗口、保留额度与落盘。"""

import json
from pathlib import Path

import pytest

from deepether.commandcode.budget import (
    WINDOW_FIVE_HOUR,
    WINDOW_MONTH,
    WINDOW_WEEK,
    BudgetLimits,
    BudgetTracker,
)
from deepether.errors import DeepEtherError, ErrorCode


class FakeClock:
    def __init__(self, now=1_000_000.0):
        self.now = now

    def __call__(self):
        return self.now


def make_tracker(tmp_path: Path, clock: FakeClock, **limits):
    tracker = BudgetTracker(
        BudgetLimits(**{"five_hour": 3.0, "weekly": 6.0, "monthly": 9.0, "reserve": 0.5, **limits}),
        state_path=tmp_path / "budget.json",
        clock=clock,
    )
    tracker.load()
    return tracker


def test_spending_falls_out_of_five_hour_window(tmp_path):
    clock = FakeClock()
    tracker = make_tracker(tmp_path, clock)
    tracker.record(1.0, "m")
    assert tracker.spent(WINDOW_FIVE_HOUR) == pytest.approx(1.0)
    clock.now += WINDOW_FIVE_HOUR + 1
    assert tracker.spent(WINDOW_FIVE_HOUR) == 0.0
    assert tracker.spent(WINDOW_WEEK) == pytest.approx(1.0)


def test_remaining_uses_tightest_window(tmp_path):
    clock = FakeClock()
    tracker = make_tracker(tmp_path, clock)
    tracker.record(5.0, "m")
    # weekly 6 - 5 = 1，monthly 9 - 5 = 4，5h 3 - 5 → 0（最紧）
    assert tracker.remaining() == 0.0


def test_check_rejects_when_estimate_exceeds_usable(tmp_path):
    clock = FakeClock()
    tracker = make_tracker(tmp_path, clock)
    tracker.record(2.0, "m")
    # 剩余 1.0，保留 0.5 → 可用 0.5
    with pytest.raises(DeepEtherError) as error:
        tracker.check(0.6)
    assert error.value.code == ErrorCode.BUDGET_EXHAUSTED
    tracker.check(0.2)  # 不抛


def test_check_rejects_when_only_reserve_left(tmp_path):
    clock = FakeClock()
    tracker = make_tracker(tmp_path, clock)
    tracker.record(2.6, "m")
    with pytest.raises(DeepEtherError):
        tracker.check(0.0)


def test_persist_and_reload(tmp_path):
    clock = FakeClock()
    tracker = make_tracker(tmp_path, clock)
    tracker.record(0.25, "cc-a")
    tracker.persist()
    raw = json.loads((tmp_path / "budget.json").read_text(encoding="utf-8"))
    assert raw["version"] == 1

    restored = BudgetTracker(
        BudgetLimits(), state_path=tmp_path / "budget.json", clock=clock
    )
    restored.load()
    assert restored.spent(WINDOW_MONTH) == pytest.approx(0.25)
    assert restored.snapshot()["entries"] == 1


def test_entries_older_than_month_are_trimmed(tmp_path):
    clock = FakeClock()
    tracker = make_tracker(tmp_path, clock)
    tracker.record(1.0, "old")
    clock.now += WINDOW_MONTH + 2 * 24 * 3600
    tracker.record(0.5, "new")
    tracker.persist()
    raw = json.loads((tmp_path / "budget.json").read_text(encoding="utf-8"))
    assert [item["model"] for item in raw["entries"]] == ["new"]


def test_snapshot_shape(tmp_path):
    clock = FakeClock()
    tracker = make_tracker(tmp_path, clock)
    snapshot = tracker.snapshot()
    assert snapshot["five_hour_limit"] == 3.0
    assert snapshot["weekly_limit"] == 6.0
    assert snapshot["monthly_limit"] == 9.0
    assert snapshot["remaining"] == 3.0
    assert snapshot["entries"] == 0


def test_corrupt_state_file_is_ignored(tmp_path):
    path = tmp_path / "budget.json"
    path.write_text("not json", encoding="utf-8")
    tracker = BudgetTracker(BudgetLimits(), state_path=path, clock=FakeClock())
    tracker.load()
    assert tracker.spent(WINDOW_MONTH) == 0.0

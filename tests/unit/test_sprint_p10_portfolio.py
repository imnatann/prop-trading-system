import pytest
from datetime import datetime, timezone

from src.broker.base import PositionInfo
from src.portfolio.allocator import PortfolioAllocator
from src.portfolio.exposure import PortfolioExposureManager


def test_portfolio_exposure_decomposition():
    mgr = PortfolioExposureManager(max_exposure_per_currency_usd=150_000.0)

    # 1. BUY 1.0 lot EURUSD + BUY 1.0 lot GBPUSD
    # Menghasilkan eksposur ganda SHORT USD (-$200,000)!
    positions = [
        PositionInfo("1", "QP-1", "EURUSD", "BUY", 1.0, 1.0850, 0, 0, 1.0850, 0),
        PositionInfo("2", "QP-2", "GBPUSD", "BUY", 1.0, 1.2650, 0, 0, 1.2650, 0),
    ]

    report = mgr.decompose_positions(positions)
    assert report.currency_exposures_usd["EUR"] == 100_000.0
    assert report.currency_exposures_usd["GBP"] == 100_000.0
    assert report.currency_exposures_usd["USD"] == -200_000.0
    assert report.dominant_currency == "USD"
    assert report.is_concentrated is True  # $200k > $150k limit

    # 2. BUY 1.0 lot EURUSD + SELL 1.0 lot GBPUSD (Net USD is balanced!)
    hedged_positions = [
        PositionInfo("1", "QP-1", "EURUSD", "BUY", 1.0, 1.0850, 0, 0, 1.0850, 0),
        PositionInfo("2", "QP-2", "GBPUSD", "SELL", 1.0, 1.2650, 0, 0, 1.2650, 0),
    ]
    hedged_report = mgr.decompose_positions(hedged_positions)
    assert hedged_report.currency_exposures_usd["USD"] == 0.0
    assert hedged_report.is_concentrated is False


def test_portfolio_allocator_rules():
    allocator = PortfolioAllocator(
        allowed_symbols={"EURUSD", "USDJPY", "GBPUSD"},
        max_exposure_per_currency_usd=150_000.0,
        max_portfolio_open_positions=2
    )

    # 1. Approved first EURUSD trade
    ok, reason, lot = allocator.evaluate_allocation("EURUSD", "BUY", 1.0, current_positions=[])
    assert ok is True
    assert lot == 1.0

    # 2. Reject unapproved symbol (e.g. AUDNZD)
    ok, reason, _ = allocator.evaluate_allocation("AUDNZD", "BUY", 1.0, current_positions=[])
    assert ok is False
    assert "not in approved portfolio instruments" in reason

    # 3. Reject when max portfolio positions reached
    current_pos = [
        PositionInfo("1", "QP-1", "EURUSD", "BUY", 0.5, 1.0850, 0, 0, 1.0850, 0),
        PositionInfo("2", "QP-2", "USDJPY", "BUY", 0.5, 154.00, 0, 0, 154.00, 0),
    ]
    ok, reason, _ = allocator.evaluate_allocation("GBPUSD", "BUY", 0.5, current_positions=current_pos)
    assert ok is False
    assert "Max portfolio positions reached" in reason

    # 4. Reject when concentration limit breached
    single_pos = [
        PositionInfo("1", "QP-1", "EURUSD", "BUY", 1.0, 1.0850, 0, 0, 1.0850, 0), # -$100k USD
    ]
    # Trying to BUY another 1.0 lot GBPUSD would bring USD net short to -$200k (> $150k limit)
    ok, reason, _ = allocator.evaluate_allocation("GBPUSD", "BUY", 1.0, current_positions=single_pos)
    assert ok is False
    assert "excessive USD concentration" in reason

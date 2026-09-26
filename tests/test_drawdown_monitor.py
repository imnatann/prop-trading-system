"""
Unit Tests for Drawdown Monitor.
Memvalidasi kalkulasi persentase daily loss, total loss, dan pendeteksian breach.
"""

import pytest
from src.risk.drawdown_monitor import DrawdownMonitor, AccountSnapshot


def test_drawdown_monitor_calculations():
    monitor = DrawdownMonitor(
        initial_balance=100_000.0,
        max_daily_loss_pct=4.0,
        max_total_loss_pct=8.0,
        kill_switch_pct=3.8
    )

    # 1. Tidak ada loss (Equity = Balance)
    assert monitor.calculate_daily_loss_pct(100_000.0) == 0.0
    assert not monitor.is_daily_limit_breached(100_000.0)

    # 2. Floating loss kecil (Equity = $98,000 -> Loss = 2.0%)
    assert monitor.calculate_daily_loss_pct(98_000.0) == pytest.approx(2.0, 0.01)
    assert not monitor.is_daily_limit_breached(98_000.0)
    assert not monitor.is_kill_switch_triggered(98_000.0)

    # 3. Floating loss mendekati batas darurat (Equity = $96,200 -> Loss = 3.8%)
    assert monitor.calculate_daily_loss_pct(96_200.0) == pytest.approx(3.8, 0.01)
    assert monitor.is_kill_switch_triggered(96_200.0)
    assert not monitor.is_daily_limit_breached(96_200.0)  # Limit 4.0 belum kena

    # 4. Floating loss melanggar Daily Limit (Equity = $95,500 -> Loss = 4.5%)
    assert monitor.calculate_daily_loss_pct(95_500.0) == pytest.approx(4.5, 0.01)
    assert monitor.is_daily_limit_breached(95_500.0)

    # 5. Total loss limit breach (Equity = $91,000 -> Total Loss = 9.0%)
    assert monitor.is_total_limit_breached(91_000.0)


def test_drawdown_monitor_profit_scenario():
    monitor = DrawdownMonitor(initial_balance=100_000.0)
    # Jika akun sedang profit (Equity = $103,000), daily loss harus 0.0%
    assert monitor.calculate_daily_loss_pct(103_000.0) == 0.0


def test_drawdown_monitor_midnight_floating_profit_baseline():
    """
    Skenario Kritis Prop Firm:
    Pada 00:00 midnight, balance = $100,000 tapi ada trade floating profit sehingga equity = $102,000.
    Baseline harian diatur ke $102,000 (higher of balance & equity).
    Jika market berbalik dan equity turun ke $97,000:
    Loss = ($102,000 - $97,000) / $102,000 = 4.90% (Melanggar Daily DD limit 4.0%).
    """
    monitor = DrawdownMonitor(initial_balance=100_000.0, max_daily_loss_pct=4.0)
    # Reset pada midnight dengan floating profit
    monitor.reset_daily_baseline(new_balance=100_000.0, current_equity=102_000.0, force=True)
    assert monitor.start_of_day_baseline == 102_000.0

    # Market berbalik ke $97,000
    loss_pct = monitor.calculate_daily_loss_pct(97_000.0)
    assert loss_pct == pytest.approx(4.902, 0.01)
    assert monitor.is_daily_limit_breached(97_000.0) is True


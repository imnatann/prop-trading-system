"""
Unit Tests for Sprint P0 — Correctness Hardening.
Menguji BrokerClock, PropFirmPolicy (Funding Pips / The5ers / FundedNext),
Broker-Native Tick Economics, dan Hard Risk Floor (P0-1 s/d P0-5).
"""

from datetime import datetime, timezone, date
import zoneinfo
import pytest

from src.broker.clock import ServerTimeClock, MockBrokerClock
from src.broker.models import SymbolSpec
from src.risk.policies import FundingPipsPolicy, The5ersPolicy, FundedNextPolicy
from src.risk.position_sizer import PositionSizer
from src.risk.drawdown_monitor import DrawdownMonitor, AccountSnapshot


# =====================================================================
# P0-1: BROKER CLOCK & TIMEZONE TESTS
# =====================================================================

def test_mock_clock_day_rollover():
    """Memverifikasi deteksi pergantian hari (00:00) pada jam broker."""
    # Jam 23:55 pada 2026-09-20
    dt_start = datetime(2026, 9, 20, 23, 55, tzinfo=timezone.utc)
    clock = MockBrokerClock(dt_start)

    last_date = clock.today()
    assert last_date == date(2026, 9, 20)
    assert clock.is_new_day(last_date) is False

    # Maju 10 menit ke 00:05 pada 2026-09-21
    clock.advance_minutes(10)
    assert clock.today() == date(2026, 9, 21)
    assert clock.is_new_day(last_date) is True


def test_server_clock_dst_timezone_resolution():
    """Memverifikasi resolusi otomatis waktu musim dingin (UTC+2) dan musim panas (UTC+3) di Cyprus/Nicosia."""
    tz_cyprus = zoneinfo.ZoneInfo("Europe/Nicosia")

    # Musim Dingin (Januari): UTC+2
    winter_dt = datetime(2026, 1, 15, 12, 0, tzinfo=timezone.utc).astimezone(tz_cyprus)
    assert winter_dt.utcoffset().total_seconds() == 7200  # +2 jam

    # Musim Panas (Juli): UTC+3
    summer_dt = datetime(2026, 7, 15, 12, 0, tzinfo=timezone.utc).astimezone(tz_cyprus)
    assert summer_dt.utcoffset().total_seconds() == 10800  # +3 jam


# =====================================================================
# P0-2: PROPFIRM POLICY STRATEGY PATTERN TESTS
# =====================================================================

def test_funding_pips_policy_baseline_and_floor():
    """
    Funding Pips 2-Step Standard:
    Baseline = max(Balance, Equity) pada 00:00 platform time.
    Floor = Baseline - (4% buffer * Initial Balance).
    """
    policy = FundingPipsPolicy(max_daily_loss_pct=4.0, max_total_loss_pct=8.0)
    initial_balance = 100_000.0

    # Kasus 1: Floating Profit pada tengah malam (Balance $100k, Equity $102k)
    baseline = policy.compute_daily_baseline(balance=100_000.0, equity=102_000.0)
    assert baseline == 102_000.0

    floor = policy.compute_daily_floor(baseline=baseline, initial_balance=initial_balance)
    # Official base is the BASELINE: 4% of $102,000 = $4,080 -> floor $97,920.
    # (The old assertion used 4% of the INITIAL balance = $4,000, which is the
    # formula FundingPips does not use.)
    assert floor == pytest.approx(102_000.0 * (1.0 - 4.0 / 100.0))  # $97,920.0

    # Equity turun ke $97,900 -> Breach! (di bawah floor $97,920)
    assert policy.is_daily_breached(equity=97_900.0, baseline=baseline, initial_balance=initial_balance) is True
    # Equity di $98,000 -> Safe (di atas floor)
    assert policy.is_daily_breached(equity=98_000.0, baseline=baseline, initial_balance=initial_balance) is False


def test_the5ers_vs_fundednext_policy_differences():
    """Memverifikasi perbedaan formula Floor antara The5ers dan FundedNext."""
    the5ers = The5ersPolicy(max_daily_loss_pct=4.0)
    fundednext = FundedNextPolicy(max_daily_loss_pct=4.0)
    initial = 100_000.0

    # Midnight: Balance $100k, Equity $105k
    b_the5ers = the5ers.compute_daily_baseline(balance=100_000.0, equity=105_000.0)
    b_fnext = fundednext.compute_daily_baseline(balance=100_000.0, equity=105_000.0)

    assert b_the5ers == 105_000.0  # The5ers uses higher of balance/equity
    assert b_fnext == 100_000.0    # FundedNext uses opening balance

    floor_the5ers = the5ers.compute_daily_floor(b_the5ers, initial)
    floor_fnext = fundednext.compute_daily_floor(b_fnext, initial)

    # The5ers 4% of $105,000 = $4,200 loss -> floor $100,800
    assert floor_the5ers == pytest.approx(100_800.0)
    # FundedNext 4% of initial $100,000 from $100,000 balance -> floor $96,000
    assert floor_fnext == 96_000.0


# =====================================================================
# P0-4: HARD RISK CAP — REJECT TRADE JIKA LOT AMAN < VOLUME_MIN
# =====================================================================

def test_position_sizer_hard_risk_floor_rejects_sub_min():
    """
    ATURAN KESELAMATAN MODAL:
    Jika lot aman yang dihitung < broker volume_min, sistem WAJIB mengembalikan 0.0 (REJECT).
    DILARANG menaikkan lot ke volume_min karena akan melipatgandakan risiko aktual!
    """
    sizer = PositionSizer()
    spec = SymbolSpec(
        symbol="EURUSD",
        tick_size=0.00001,
        tick_value=1.0,
        contract_size=100_000.0,
        volume_min=0.01,
        volume_max=50.0,
        volume_step=0.01,
        digits=5
    )

    # Modal $2,000, risiko 0.25% = $5.00 cash risk
    # Entry: 1.08500, SL: 1.07500 (100 pips / 1000 ticks)
    # 1000 ticks * $1.00 tick_value = $1,000 per lot
    # Lot aman = $5 / $1,000 = 0.005 lot
    # Karena 0.005 < volume_min 0.01:
    lot = sizer.calculate_lot(
        equity=2_000.0,
        risk_pct=0.25,
        entry_price=1.08500,
        stop_loss=1.07500,
        symbol="EURUSD",
        spec=spec
    )
    # WAJIB REJECT (0.0)! Tidak boleh dibulatkan ke 0.01!
    assert lot == 0.0


def test_position_sizer_volume_step_quantization():
    """Memverifikasi kuantisasi presisi ke volume_step broker."""
    sizer = PositionSizer()
    spec = SymbolSpec(
        symbol="EURUSD",
        tick_size=0.00001,
        tick_value=1.0,
        contract_size=100_000.0,
        volume_min=0.01,
        volume_max=50.0,
        volume_step=0.01,
        digits=5
    )

    # Akun $100,000, risiko 0.5% = $500
    # Entry: 1.08500, SL: 1.08323 (17.7 pips / 177 ticks)
    # 177 ticks * $1.00 = $177 per lot
    # Lot mentah = $500 / $177 = 2.8248... lot
    lot = sizer.calculate_lot(
        equity=100_000.0,
        risk_pct=0.5,
        entry_price=1.08500,
        stop_loss=1.08323,
        symbol="EURUSD",
        spec=spec
    )
    # Harus dibulatkan ke bawah / kuantisasi ke step 0.01 -> 2.82 lot
    assert lot == 2.82


# =====================================================================
# P0-3 & P0-5: BROKER-NATIVE TICK ECONOMICS (GOLD & FX)
# =====================================================================

def test_broker_native_gold_tick_economics():
    """Memverifikasi kalkulasi Emas XAUUSD menggunakan spesifikasi resmi broker."""
    sizer = PositionSizer()
    spec_gold = SymbolSpec(
        symbol="XAUUSD",
        tick_size=0.01,           # 2-digit gold
        tick_value=1.0,           # 100 oz * 0.01 = $1.00 per tick
        contract_size=100.0,
        volume_min=0.01,
        volume_max=50.0,
        volume_step=0.01,
        digits=2
    )

    # Akun $100,000, risiko 0.5% = $500
    # Entry: 2650.00, SL: 2645.00 -> Jarak $5.00 = 500 ticks
    # 500 ticks * $1.00 = $500 per lot
    # Expected lot = $500 / $500 = 1.00 lot
    lot = sizer.calculate_lot(
        equity=100_000.0,
        risk_pct=0.5,
        entry_price=2650.00,
        stop_loss=2645.00,
        symbol="XAUUSD",
        spec=spec_gold
    )
    assert lot == 1.00


# =====================================================================
# INTEGRATION: DRAWDOWN MONITOR + BROKER CLOCK + FUNDING PIPS POLICY
# =====================================================================

def test_drawdown_monitor_with_clock_and_funding_pips():
    """Memverifikasi integrasi DrawdownMonitor dengan MockBrokerClock dan FundingPipsPolicy."""
    dt_start = datetime(2026, 9, 20, 23, 50, tzinfo=timezone.utc)
    mock_clock = MockBrokerClock(dt_start)
    policy = FundingPipsPolicy(max_daily_loss_pct=4.0, max_total_loss_pct=8.0, kill_switch_pct=3.8)

    monitor = DrawdownMonitor(
        initial_balance=100_000.0,
        policy=policy,
        clock=mock_clock
    )

    # Hari 1: Saldo awal $100,000
    assert monitor.start_of_day_baseline == 100_000.0

    # Pukul 23:55: Akun profit, floating equity = $103,000
    snap_midnight = AccountSnapshot(balance=100_000.0, equity=103_000.0)
    monitor.update_snapshot(snap_midnight)
    # Belum lewat tengah malam, baseline masih hari 1
    assert monitor.start_of_day_baseline == 100_000.0

    # Lewat tengah malam (Pukul 00:05 tanggal 2026-09-21)
    mock_clock.advance_minutes(15)
    snap_new_day = AccountSnapshot(balance=100_000.0, equity=103_000.0)
    monitor.update_snapshot(snap_new_day)

    # Baseline hari baru harus naik ke $103,000!
    assert monitor.start_of_day_baseline == 103_000.0
    # 4% of the BASELINE $103,000 = $4,120 -> floor $98,880
    assert monitor.get_daily_floor() == pytest.approx(103_000.0 * (1.0 - 4.0 / 100.0))

    # Jika equity jatuh ke $98,500 (< floor $98,880)
    assert monitor.is_daily_limit_breached(98_500.0) is True
    assert monitor.is_kill_switch_triggered(98_500.0) is True

"""
Unit Tests for Dynamic Position Sizer.
Memvalidasi formula kalkulasi lot sizing untuk berbagai skenario ukuran akun dan stop loss.
"""

import pytest
from src.risk.position_sizer import PositionSizer


def test_position_sizer_eurusd_standard():
    sizer = PositionSizer()
    
    # Akun $100,000, risiko 0.5% = $500
    # Entry: 1.08500, SL: 1.08300 -> SL distance = 0.0020 (20 pips)
    # Pip value = $10 / lot -> 20 pips * $10 = $200 per lot
    # Expected lot = $500 / $200 = 2.50 lot
    lot = sizer.calculate_lot(
        equity=100_000.0,
        risk_pct=0.5,
        entry_price=1.08500,
        stop_loss=1.08300,
        symbol="EURUSD"
    )
    assert lot == 2.50


def test_position_sizer_small_account_rejects_sub_min_lot():
    sizer = PositionSizer(min_lot=0.01)
    
    # Akun kecil $1,000, risiko 0.25% = $2.50
    # Entry: 1.08500, SL: 1.07500 (100 pips)
    # Kalkulasi lot aman = 0.0025 lot < min_lot 0.01
    # P0-4 Hard Risk Floor: WAJIB DITOLAK (0.0), bukan dibulatkan ke 0.01!
    lot = sizer.calculate_lot(
        equity=1_000.0,
        risk_pct=0.25,
        entry_price=1.08500,
        stop_loss=1.07500,
        symbol="EURUSD"
    )
    assert lot == 0.0

    # Jika bendera allow_sub_min_rounding diaktifkan secara eksplisit (opsional):
    lot_rounded = sizer.calculate_lot(
        equity=1_000.0,
        risk_pct=0.25,
        entry_price=1.08500,
        stop_loss=1.07500,
        symbol="EURUSD",
        allow_sub_min_rounding=True
    )
    assert lot_rounded == 0.01


def test_position_sizer_gold_xauusd():
    sizer = PositionSizer()
    
    # Akun $50,000, risiko 1% = $500
    # XAUUSD: pip_size = 0.1, pip_value = $10
    # Entry: 2650.00, SL: 2645.00 -> $5.0 distance = 50 pips
    # 50 pips * $10 = $500 per lot
    # Expected lot = $500 / $500 = 1.00 lot
    lot = sizer.calculate_lot(
        equity=50_000.0,
        risk_pct=1.0,
        entry_price=2650.00,
        stop_loss=2645.00,
        symbol="XAUUSD"
    )
    assert lot == 1.00


def test_position_sizer_invalid_inputs():
    sizer = PositionSizer()
    # Jarak SL 0 atau equity 0 harus menghasilkan lot 0
    assert sizer.calculate_lot(equity=0, risk_pct=0.5, entry_price=1.08, stop_loss=1.07) == 0.0
    assert sizer.calculate_lot(equity=10000, risk_pct=0.5, entry_price=1.08, stop_loss=1.08) == 0.0


def test_position_sizer_usdjpy_dynamic_pip():
    sizer = PositionSizer()
    # Akun $100,000, risiko 0.5% = $500
    # Entry: 155.00, SL: 154.50 (50 pips)
    # Pip value at 155.00: 1,000 / 155.00 = $6.45 per lot
    # Risk = $500. Jarak 50 pips * $6.45 = $322.50 per lot
    # Lot = 500 / 322.50 = 1.55 lot
    lot = sizer.calculate_lot(
        equity=100_000.0,
        risk_pct=0.5,
        entry_price=155.00,
        stop_loss=154.50,
        symbol="USDJPY"
    )
    assert lot == 1.55


def test_position_sizer_eurgbp_cross_pair():
    sizer = PositionSizer()
    # Akun $100,000, risiko 0.5% = $500
    # EURGBP: quote currency is GBP. Misal GBPUSD = 1.28
    # Pip value = 10 * 1.28 = $12.80
    # Entry: 0.8500, SL: 0.8480 (20 pips)
    # 20 pips * $12.80 = $256.00 per lot
    # Lot = $500 / $256.00 = 1.95 lot
    lot = sizer.calculate_lot(
        equity=100_000.0,
        risk_pct=0.5,
        entry_price=0.8500,
        stop_loss=0.8480,
        symbol="EURGBP",
        conversion_rate_to_usd=1.28
    )
    assert lot == 1.95


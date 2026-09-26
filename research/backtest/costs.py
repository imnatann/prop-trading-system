"""
Execution Cost Modeling.
Menghitung friksi pasar nyata: spread, slippage, komisi, dan biaya swap overnight.
"""

from dataclasses import dataclass


@dataclass
class CostModel:
    spread_pips: float = 1.0           # 1.0 pip spread rata-rata EURUSD
    commission_per_lot: float = 3.0    # $3.00 per standard lot per side ($6 round-turn)
    slippage_pips: float = 0.2         # 0.2 pip rata-rata slippage eksekusi
    swap_long_points: float = -0.5     # Swap harian posisi buy
    swap_short_points: float = 0.1     # Swap harian posisi sell

    def compute_entry_costs(self, lot_size: float, pip_value: float = 10.0) -> float:
        """Biaya satu arah saat entry: Komisi + Slippage."""
        comm = self.commission_per_lot * lot_size
        slip_cost = self.slippage_pips * pip_value * lot_size
        return comm + slip_cost

    def compute_exit_costs(self, lot_size: float, pip_value: float = 10.0) -> float:
        """Biaya satu arah saat exit: Komisi + Slippage."""
        comm = self.commission_per_lot * lot_size
        slip_cost = self.slippage_pips * pip_value * lot_size
        return comm + slip_cost

"""
Reconciliation Safety Policies.
Aturan pengambilan keputusan saat terjadi mismatch antara OMS dan Broker.
"""

from dataclasses import dataclass


@dataclass
class ReconciliationPolicy:
    """Kebijakan keamanan rekonsiliasi."""
    allow_auto_adopt_orphan: bool = True
    lock_risk_on_unknown_external: bool = True
    auto_close_ghost_positions: bool = True
    emergency_liquidate_on_breach: bool = True
    expected_comment_prefix: str = "QP-"

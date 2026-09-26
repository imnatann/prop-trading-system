"""
Configuration Validator & Pre-Flight Risk Exposure Boundary Checker.
Mencegah deployment parameter salah atau berbahaya:
- Menolak risk_per_trade berlebihan (misal 5%).
- Memverifikasi Worst-Case Simultaneous Exposure:
  max_possible_loss = risk_per_trade * max_concurrent_positions <= max_daily_loss_pct.
"""

from dataclasses import dataclass, field
from typing import List, Optional
from loguru import logger

from config.prop_rules import PropFirmRules


@dataclass
class ConfigValidationResult:
    is_valid: bool
    max_worst_case_exposure_pct: float
    errors: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)


class ConfigValidator:
    """Validator konfigurasi parameter sebelum sistem diizinkan startup."""

    def __init__(
        self,
        max_allowed_single_risk_pct: float = 1.0,
        firm_ceiling_daily_dd_pct: float = 5.0,
        firm_ceiling_total_dd_pct: float = 10.0
    ):
        self.max_allowed_single_risk_pct = max_allowed_single_risk_pct
        self.firm_ceiling_daily_dd_pct = firm_ceiling_daily_dd_pct
        self.firm_ceiling_total_dd_pct = firm_ceiling_total_dd_pct

    def validate(
        self,
        rules: PropFirmRules,
        risk_per_trade_pct: float,
        max_concurrent_positions: int = 3
    ) -> ConfigValidationResult:
        errors: List[str] = []
        warnings: List[str] = []

        # 1. Validasi single-trade risk
        if risk_per_trade_pct <= 0:
            errors.append(f"Invalid risk_per_trade_pct: {risk_per_trade_pct} must be positive")
        elif risk_per_trade_pct > self.max_allowed_single_risk_pct:
            errors.append(
                f"Dangerous single-trade risk: {risk_per_trade_pct:.2f}% exceeds safety ceiling {self.max_allowed_single_risk_pct:.2f}%"
            )

        # 2. Validasi aturan prop firm terhadap plafon industri
        if rules.max_daily_loss_pct > self.firm_ceiling_daily_dd_pct:
            errors.append(
                f"Configured max_daily_loss_pct ({rules.max_daily_loss_pct}%) exceeds prop ceiling ({self.firm_ceiling_daily_dd_pct}%)"
            )

        if rules.kill_switch_daily_pct >= rules.max_daily_loss_pct:
            errors.append(
                f"Kill switch threshold ({rules.kill_switch_daily_pct}%) must be strictly lower than daily limit ({rules.max_daily_loss_pct}%)"
            )

        # 3. Worst-Case Simultaneous Exposure Validation (Formula Planner)
        # max_possible_loss = risk_per_trade * max_concurrent_positions
        worst_case_loss = risk_per_trade_pct * max_concurrent_positions
        if worst_case_loss > rules.max_daily_loss_pct:
            errors.append(
                f"Worst-Case Exposure Breach! {max_concurrent_positions} positions * {risk_per_trade_pct}% = "
                f"{worst_case_loss:.2f}% which exceeds max daily loss limit ({rules.max_daily_loss_pct}%). "
                "A correlated market gap would instantly disqualify the account!"
            )
        elif worst_case_loss > (rules.max_daily_loss_pct * 0.8):
            warnings.append(
                f"High simultaneous exposure buffer: {worst_case_loss:.2f}% is close to daily limit {rules.max_daily_loss_pct}%"
            )

        is_valid = len(errors) == 0
        if not is_valid:
            for err in errors:
                logger.critical(f"ConfigValidator BLOCK: {err}")

        return ConfigValidationResult(
            is_valid=is_valid,
            max_worst_case_exposure_pct=worst_case_loss,
            errors=errors,
            warnings=warnings
        )

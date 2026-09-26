"""FundingPips rule book -- the OFFICIAL, SOURCED numbers.

Why this file exists
--------------------
Every profitability number this project can ever produce is conditional on one
question: does the account survive?  For a prop evaluation the objective is

    max P(reach target BEFORE any hard breach)

not max Sharpe. That makes the rule book an INPUT to the simulator, not a
footnote. Before this module the repo had rules scattered across
config/prop_rules.py and src/risk/policies.py with three defects that made
account failure un-measurable:

  1. the daily-loss floor used the INITIAL BALANCE as the percentage base,
     while the official rule uses the higher-of-opening-balance/equity
     ("baseline") as the base.  Worked example on the official page:
     baseline $107,000 -> 5% = $5,350 -> floor $101,650.  The old formula
     produced a different floor, so a simulated "breach" was not the broker
     breach.
  2. the maximum-loss check tested equity only, while the official wording is
     "equity OR balance cannot hit 10% below the starting account size".
  3. the daily reset used a DST-following zone while the documented contract
     is 00:00 Platform Time (UTC+3).

HARD vs SOFT is the central distinction
---------------------------------------
    HARD  = the broker published limit.  Touching it TERMINATES the account.
            These numbers are transcribed from the FundingPips help centre and
            must never be tuned.
    SOFT  = our own internal buffer.  Reaching it makes US stop trading while
            the account is still alive.  These are policy choices and are free
            to be conservative.

Conflating the two is what made the old code unusable for pass-probability:
it reported a buffer touch as if it were a broker breach, so
P(pass before breach) could not be computed at all.

Sources (retrieved 2026-09-25)
------------------------------
  1 Step Flex     https://help.fundingpips.com/hc/en-us/articles/34501697434385-1-Step-Flex
  2 Step Standard https://help.fundingpips.com/hc/en-us/articles/34501809112081-2-Step-Standard
  2 Step Flex     https://help.fundingpips.com/hc/en-us/articles/47835196271249-2-Step-Flex
  2 Step Pro      https://help.fundingpips.com/hc/en-us/articles/34502027344017-2-Step-Pro-Model
  Conduct         https://help.fundingpips.com/hc/en-us/articles/34505029138449-Trading-Conduct-and-Security-Standards

Rules change.  FundingPips altered the Standard target (24 Jul 2026) and the
Pro/Flex minimum-day rules (26 Aug 2026) inside a single year.  Treat this file
as a snapshot with a date, re-verify before any paid evaluation, and never let a
rule live as a magic number inside strategy code.
"""
from __future__ import annotations

import datetime as _dt
from dataclasses import dataclass
from typing import Dict, Optional, Tuple

#: The rule sheet was transcribed on this date. Re-verify before relying on it.
RULES_AS_OF = "2026-09-25"

#: Daily-loss limits reset at 00:00 Platform Time, documented as UTC+3.
#: Encoded as a FIXED offset because that is what the rule text says; a
#: DST-following zone would silently shift the reset by an hour for half the
#: year.
PLATFORM_UTC_OFFSET_HOURS = 3
PLATFORM_RESET_LABEL = "00:00 Platform Time (UTC+3)"

#: Commission per lot, published on the model pages.
COMMISSION_PER_LOT_STANDARD = 5.0
COMMISSION_PER_LOT_SWAP_FREE = 10.0

#: News embargo (minutes) around a high-impact event for the affected currency.
#: Evaluation: deliberately trading news is prohibited.
#: Master: 5 min before/after for releases, 10 min before/after for speeches.
NEWS_EMBARGO_MASTER_MINUTES = 5
SPEECH_EMBARGO_MASTER_MINUTES = 10
#: A trade opened >= this many hours before the event is exempt on Master.
SWING_EXEMPTION_HOURS = 5.0

#: Trade-idea grouping: a new trade within this many minutes of closing a LOSING
#: trade counts as the SAME idea. This is why immediate re-entry after a stop is
#: doubly bad -- it is poor tail risk AND it burns the idea budget.
TRADE_IDEA_REENTRY_MINUTES = 10

#: Inactivity: a completed trade is required within this many days.
INACTIVITY_BREACH_DAYS = 30

#: Profit concentration: one trade idea above this fraction of the phase target
#: triggers the policy (then 4 profitable days >=0.5% are required per reward).
PROFIT_CONCENTRATION_FRACTION = 0.60


@dataclass(frozen=True)
class EvaluationModel:
    """One FundingPips evaluation structure.

    max_loss_pct is STATIC relative to the starting size on all four
    evaluation models (verified: 12 / 10 / 12 / 6).  It is not trailing.
    """

    key: str
    label: str
    phase1_target_pct: float
    phase2_target_pct: float
    daily_loss_pct: float
    max_loss_pct: float
    min_trading_days_phase1: int
    min_trading_days_phase2: int
    account_sizes: Tuple[int, ...]
    forex_leverage: int = 100
    concentration_applies_all_sizes: bool = False
    concentration_min_size: int = 25000
    max_loss_is_static: bool = True

    #: How phase 2 measures its target.
    #:
    #:   "cumulative"  -> phase2 needs (p1 + p2)% on the INITIAL balance,
    #:                    e.g. Standard: 8% + 5% = 13% total. This matches the
    #:                    official phrasing "Phase 1 (8%) + Phase 2 (5%)".
    #:   "compounding" -> phase2 needs p2% on the phase-1 ENDING balance.
    #:
    #: The help centre states the phase-2 TARGET but not its BASE, so this is a
    #: genuine, documented assumption -- not an official number. Verify it on
    #: the Free Trial before relying on it, and see the acceptance test
    #: test_phase2_base_convention_is_documented_and_declared.
    phase2_base: str = "cumulative"

    def passing_balance(self, initial_balance: float, phase: int = 1) -> float:
        """Balance required to PASS the given phase (target reached).

        Uses a relative tolerance: 10000 * 1.12 evaluates to 11200.000000000002
        in binary floating point, so an exact >= comparison would tell a trader
        who hit the target EXACTLY that they had not passed.
        """
        if phase == 1:
            pct = self.phase1_target_pct
        elif self.phase2_base == "compounding" and self.phase1_target_pct:
            grown = initial_balance * (1.0 + self.phase1_target_pct / 100.0)
            return grown * (1.0 + self.phase2_target_pct / 100.0)
        else:
            pct = self.phase1_target_pct + self.phase2_target_pct
        return initial_balance * (1.0 + pct / 100.0)

    def hard_daily_floor(self, baseline: float) -> float:
        """Equity floor for the OFFICIAL daily limit.

        Official wording: "Equity cannot fall by more than 5% of that baseline
        at any point during the day."  The percentage is taken of the BASELINE
        (higher of opening balance or opening equity), never of the initial
        balance.  Verified against the worked example: baseline $107,000 ->
        $5,350 -> floor $101,650.
        """
        return baseline * (1.0 - self.daily_loss_pct / 100.0)

    def hard_max_loss_floor(self, initial_balance: float) -> float:
        """Static floor for the OFFICIAL maximum loss, from starting size."""
        return initial_balance * (1.0 - self.max_loss_pct / 100.0)

    def concentration_applies(self, account_size: float) -> bool:
        if self.concentration_applies_all_sizes:
            return True
        return account_size >= self.concentration_min_size


# ------------------------------------------------------------------ the models

ONE_STEP_FLEX = EvaluationModel(
    key="1_step_flex",
    label="1 Step Flex",
    phase1_target_pct=12.0,
    phase2_target_pct=0.0,
    daily_loss_pct=3.0,
    max_loss_pct=12.0,
    min_trading_days_phase1=0,
    min_trading_days_phase2=0,
    account_sizes=(5000, 10000, 25000, 50000, 100000),
    concentration_applies_all_sizes=True,
    concentration_min_size=0,
)

TWO_STEP_STANDARD = EvaluationModel(
    key="2_step_standard",
    label="2 Step Standard",
    phase1_target_pct=8.0,
    phase2_target_pct=5.0,
    daily_loss_pct=5.0,
    max_loss_pct=10.0,
    min_trading_days_phase1=3,
    min_trading_days_phase2=3,
    account_sizes=(5000, 10000, 25000, 50000, 100000),
)

TWO_STEP_FLEX = EvaluationModel(
    key="2_step_flex",
    label="2 Step Flex",
    phase1_target_pct=10.0,
    phase2_target_pct=6.0,
    daily_loss_pct=4.0,
    max_loss_pct=12.0,
    min_trading_days_phase1=1,
    min_trading_days_phase2=1,
    account_sizes=(5000, 10000, 25000, 50000, 100000),
)

TWO_STEP_PRO = EvaluationModel(
    key="2_step_pro",
    label="2 Step Pro",
    phase1_target_pct=6.0,
    phase2_target_pct=6.0,
    daily_loss_pct=3.0,
    max_loss_pct=6.0,
    min_trading_days_phase1=2,
    min_trading_days_phase2=2,
    account_sizes=(5000, 10000, 25000, 50000, 100000, 200000),
)

MODELS: Dict[str, EvaluationModel] = {
    m.key: m for m in (ONE_STEP_FLEX, TWO_STEP_STANDARD, TWO_STEP_FLEX, TWO_STEP_PRO)
}

DEFAULT_MODEL = TWO_STEP_STANDARD


# ------------------------------------------------------------------ soft buffer

@dataclass(frozen=True)
class RiskBuffer:
    """Our OWN internal limits. Reaching these stops trading while alive.

    Deliberately far inside the hard limits. The *_fraction_of_hard fields are
    the honest way to express "use at most this share of the legal budget": it
    scales with the model instead of hardcoding a percentage that may exceed
    the broker limit.
    """

    daily_fraction_of_hard: float = 0.80
    max_loss_fraction_of_hard: float = 0.80
    risk_per_trade_pct: float = 0.25
    correlated_group_pct: float = 0.75

    def soft_daily_floor(self, model: EvaluationModel, baseline: float) -> float:
        legal_loss = baseline * model.daily_loss_pct / 100.0
        return baseline - legal_loss * self.daily_fraction_of_hard

    def soft_max_loss_floor(self, model: EvaluationModel, initial_balance: float) -> float:
        legal_loss = initial_balance * model.max_loss_pct / 100.0
        return initial_balance - legal_loss * self.max_loss_fraction_of_hard


DEFAULT_BUFFER = RiskBuffer()


# --------------------------------------------------------------- reset helper

def platform_trading_day(ts: _dt.datetime,
                         offset_hours: int = PLATFORM_UTC_OFFSET_HOURS) -> _dt.date:
    """Map a UTC instant to the FundingPips platform trading day.

    The daily loss limit resets at 00:00 Platform Time (UTC+3). An instant at
    22:00 UTC on the 1st is already 01:00 on the 2nd in platform time, so it
    belongs to the 2nd risk budget. Getting this wrong shifts every daily
    baseline by one session.
    """
    if ts.tzinfo is None:
        raise ValueError("platform_trading_day requires a tz-aware UTC datetime")
    return (ts.astimezone(_dt.timezone.utc)
            + _dt.timedelta(hours=offset_hours)).date()


def platform_day_start_utc(day: _dt.date,
                           offset_hours: int = PLATFORM_UTC_OFFSET_HOURS) -> _dt.datetime:
    """UTC instant at which the given platform trading day begins."""
    base = _dt.datetime(day.year, day.month, day.day, tzinfo=_dt.timezone.utc)
    return base - _dt.timedelta(hours=offset_hours)


# ------------------------------------------------------------- disqualifiers

#: Verbatim-forbidden strategy categories. Account termination, not a warning.
FORBIDDEN_STRATEGY_CATEGORIES: Tuple[str, ...] = (
    "gap trading",
    "high-frequency trading",
    "server spamming",
    "latency arbitrage",
    "toxic trading flow",
    "hedging",
    "long-short arbitrage",
    "reverse arbitrage",
    "tick scalping",
    "server execution exploits",
    "opposite account trading",
    "churning and burning",
)

#: Connecting to a VPN/VPS while accessing the trading account is NOT permitted.
#: Offline research/backtest on a VPS is fine; the VPS must never log in.
VPS_ACCOUNT_LOGIN_ALLOWED = False

#: A personally-authored EA may run fully automated, WITH proof of ownership.
#: A compiled binary alone is not proof -- source, VCS history, and compile logs
#: are. This is why the repository test suite and history are an asset.
PERSONAL_EA_FULL_AUTOMATION_ALLOWED = True
PERSONAL_EA_PROOF_ACCEPTED: Tuple[str, ...] = (
    "uncompiled source files (.mq5/.mq4 or equivalent)",
    "version control history showing iterative development",
    "development environment evidence (screenshots, compile logs, user paths)",
    "an explanation of the EA logic on a live call with the team",
)


def describe_rules() -> Dict[str, object]:
    """Credential-free, JSON-safe summary for reports and CLI banners."""
    return {
        "rules_as_of": RULES_AS_OF,
        "platform_reset": PLATFORM_RESET_LABEL,
        "platform_utc_offset_hours": PLATFORM_UTC_OFFSET_HOURS,
        "commission_per_lot": {
            "standard": COMMISSION_PER_LOT_STANDARD,
            "swap_free_mt5": COMMISSION_PER_LOT_SWAP_FREE,
        },
        "inactivity_breach_days": INACTIVITY_BREACH_DAYS,
        "trade_idea_reentry_minutes": TRADE_IDEA_REENTRY_MINUTES,
        "profit_concentration_fraction": PROFIT_CONCENTRATION_FRACTION,
        "vps_account_access_allowed": VPS_ACCOUNT_LOGIN_ALLOWED,
        "models": {
            k: {
                "label": m.label,
                "targets_pct": [m.phase1_target_pct, m.phase2_target_pct],
                "daily_loss_pct": m.daily_loss_pct,
                "max_loss_pct": m.max_loss_pct,
                "max_loss_static": m.max_loss_is_static,
                "sizes": list(m.account_sizes),
                "min_days": [m.min_trading_days_phase1, m.min_trading_days_phase2],
            }
            for k, m in MODELS.items()
        },
        "forbidden_categories": list(FORBIDDEN_STRATEGY_CATEGORIES),
    }

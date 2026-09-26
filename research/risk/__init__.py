"""Risk research package: prop-firm rule enforcement.

Kept separate from src/risk on purpose. src/risk enforces limits at RUNTIME
against a live account. This package answers a RESEARCH question: given a
sequence of bars, would this evaluation have survived?
"""
from research.risk.fundingpips_engine import (
    BreachEvent,
    BreachKind,
    EvaluationOutcome,
    OpenPosition,
    Ordering,
    PropRuleEngine,
    SoftStop,
    TickPoint,
    adverse_price_for,
    assert_equity_identity,
    assert_path_finite,
    intrabar_path,
)

__all__ = [
    "BreachEvent", "BreachKind", "EvaluationOutcome", "OpenPosition",
    "Ordering", "PropRuleEngine", "SoftStop", "TickPoint",
    "adverse_price_for", "assert_equity_identity", "assert_path_finite",
    "intrabar_path",
]

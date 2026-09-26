"""
Observability Metrics & Telemetry Collector.
Mengumpulkan metrik latensi eksekusi, slippage empiris, win/loss stats, dan rasio fill.
"""

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Dict, List, Optional
import numpy as np


@dataclass
class ExecutionMetric:
    client_order_id: str
    symbol: str
    action: str
    requested_price: float
    executed_price: float
    slippage_pips: float
    latency_ms: float
    success: bool
    timestamp_utc: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


class MetricsCollector:
    """Kolektor metrik performa eksekusi dan latensi jaringan."""

    def __init__(self):
        self.execution_metrics: List[ExecutionMetric] = []

    def record_execution(
        self,
        client_order_id: str,
        symbol: str,
        action: str,
        requested_price: float,
        executed_price: float,
        latency_ms: float,
        success: bool,
        pip_unit: float = 0.0001
    ) -> ExecutionMetric:
        # Hitung slippage empiris
        if requested_price > 0 and executed_price > 0:
            if action.upper() == "BUY":
                slip_pips = (executed_price - requested_price) / pip_unit
            else:
                slip_pips = (requested_price - executed_price) / pip_unit
        else:
            slip_pips = 0.0

        metric = ExecutionMetric(
            client_order_id=client_order_id,
            symbol=symbol,
            action=action,
            requested_price=requested_price,
            executed_price=executed_price,
            slippage_pips=round(slip_pips, 2),
            latency_ms=round(latency_ms, 2),
            success=success
        )
        self.execution_metrics.append(metric)
        return metric

    def get_summary(self) -> Dict[str, float]:
        if not self.execution_metrics:
            return {
                "total_executions": 0,
                "success_rate_pct": 0.0,
                "mean_latency_ms": 0.0,
                "p95_latency_ms": 0.0,
                "mean_slippage_pips": 0.0,
                "max_slippage_pips": 0.0
            }

        latencies = [m.latency_ms for m in self.execution_metrics]
        slippages = [m.slippage_pips for m in self.execution_metrics if m.success]
        success_count = sum(1 for m in self.execution_metrics if m.success)

        return {
            "total_executions": len(self.execution_metrics),
            "success_rate_pct": round((success_count / len(self.execution_metrics)) * 100.0, 2),
            "mean_latency_ms": round(float(np.mean(latencies)), 2),
            "p95_latency_ms": round(float(np.percentile(latencies, 95)), 2),
            "mean_slippage_pips": round(float(np.mean(slippages)), 2) if slippages else 0.0,
            "max_slippage_pips": round(float(np.max(slippages)), 2) if slippages else 0.0
        }

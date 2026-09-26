"""
Canary & Paper Trading Soak Test Runner.
Menjalankan pengujian integrasi end-to-end dengan injeksi kesalahan (fault injection)
untuk memverifikasi keandalan sistem prop trading sebelum live evaluation.
"""

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional
from loguru import logger

from config.prop_rules import PropFirmRules
from src.data.market import Quote
from src.data.news_calendar import EconomicEvent
from src.data.validation import FreshnessGate
from src.execution.broker_adapter import MockBrokerAdapter
from src.execution.dispatcher import OrderDispatcher
from src.oms.repository import OMSRepository
from src.oms.service import OMSService
from src.portfolio.allocator import PortfolioAllocator
from src.reconciliation.reconciler import ReconciliationEngine
from src.risk.drawdown_monitor import DrawdownMonitor
from src.risk.gatekeeper import RiskGatekeeper
from src.safety.heartbeat import HeartbeatManager
from src.safety.kill_switch import EmergencyKillSwitch
from src.strategy.base import SignalAction, TradeSignal


@dataclass
class CanaryTestReport:
    total_steps: int
    passed_steps: int
    failed_steps: int
    is_fully_healthy: bool
    step_results: List[Dict[str, str]] = field(default_factory=list)


class CanarySoakRunner:
    """Harness pengujian canary dan simulasi soak test."""

    def __init__(self, db_path: str = "storage/canary_trading.db", heartbeat_dir: str = "storage/canary_heartbeats"):
        self.rules = PropFirmRules()
        self.broker = MockBrokerAdapter(initial_balance=100_000.0)
        self.broker.connect()

        self.repo = OMSRepository(db_path=db_path)
        self.oms = OMSService(self.repo)

        self.drawdown_monitor = DrawdownMonitor(
            initial_balance=100_000.0,
            max_daily_loss_pct=self.rules.max_daily_loss_pct
        )
        self.gatekeeper = RiskGatekeeper(self.rules, self.drawdown_monitor)
        self.dispatcher = OrderDispatcher(
            oms=self.oms,
            broker=self.broker,
            gatekeeper=self.gatekeeper
        )
        self.reconciler = ReconciliationEngine(
            oms=self.oms,
            broker=self.broker,
            drawdown_monitor=self.drawdown_monitor
        )
        self.heartbeat = HeartbeatManager(heartbeat_dir)
        self.freshness_gate = FreshnessGate(max_stale_seconds=5.0, max_spread_pips=3.0)
        self.portfolio = PortfolioAllocator()
        self.kill_switch = EmergencyKillSwitch(self.broker, self.rules, self.drawdown_monitor)

    def run_all_canary_checks(self) -> CanaryTestReport:
        results: List[Dict[str, str]] = []

        # Step 1: Heartbeat IPC Liveness
        self.heartbeat.write_heartbeat("trading_engine", {"status": "SOAK_TEST"})
        hb_alive = self.heartbeat.is_alive("trading_engine", max_stale_seconds=5.0)
        results.append({
            "step": "1. IPC Heartbeat Liveness",
            "status": "PASS" if hb_alive else "FAIL",
            "details": "Engine heartbeat verified"
        })

        # Step 2: Freshness Gate & Stale Quote Injection
        now = datetime.now(timezone.utc)
        fresh_quote = Quote("EURUSD", 1.0850, 1.0851, 1.0, timestamp_utc=now)
        stale_quote = Quote("EURUSD", 1.0850, 1.0851, 1.0, timestamp_utc=now - timedelta(seconds=15))
        fresh_ok = self.freshness_gate.validate_quote(fresh_quote).is_valid
        stale_rejected = not self.freshness_gate.validate_quote(stale_quote).is_valid
        step2_pass = fresh_ok and stale_rejected
        results.append({
            "step": "2. Market Data Freshness Gate",
            "status": "PASS" if step2_pass else "FAIL",
            "details": "Stale quote correctly blocked, fresh quote accepted"
        })

        # Step 3: Pre-Trade Risk Gatekeeper & News Blackout Rejection
        news_events = [
            EconomicEvent(
                currency="USD",
                title="Fed Interest Rate Decision",
                impact="HIGH",
                timestamp_utc=now + timedelta(minutes=1)
            )
        ]
        news_signal = TradeSignal("EURUSD", SignalAction.BUY, 1.0850, 1.0800, 1.0950, "News Test")
        res_news = self.dispatcher.dispatch(news_signal, lot_size=0.5, economic_events=news_events)
        step3_pass = (res_news.success is False and ("News" in (res_news.error_message or "") or "Blackout" in (res_news.error_message or "")))
        results.append({
            "step": "3. News Blackout Guard",
            "status": "PASS" if step3_pass else "FAIL",
            "details": "Order blocked during high-impact news blackout window"
        })

        # Step 4: Normal Clean Execution & SQLite WAL State Persistence
        clean_signal = TradeSignal("EURUSD", SignalAction.BUY, 1.0850, 1.0800, 1.0950, "Clean Test")
        res_exec = self.dispatcher.dispatch(clean_signal, lot_size=0.2, economic_events=[])
        order_db = self.repo.get_order_by_client_id(res_exec.client_order_id)
        step4_pass = (res_exec.success and order_db is not None and order_db.state.value == "FILLED")
        results.append({
            "step": "4. Safe Execution & WAL State Persistence",
            "status": "PASS" if step4_pass else "FAIL",
            "details": f"Order #{res_exec.client_order_id} persisted to SQLite as FILLED"
        })

        # Step 5: Discrepancy & Reconciliation Engine Check
        reconcile_sum = self.reconciler.reconcile()
        step5_pass = (reconcile_sum.is_clean and not reconcile_sum.risk_locked)
        results.append({
            "step": "5. Discrepancy Reconciliation",
            "status": "PASS" if step5_pass else "FAIL",
            "details": "Local OMS and Broker state 100% reconciled"
        })

        # Step 6: Portfolio Concentration Allocation Check
        ok_alloc, reason_alloc, _ = self.portfolio.evaluate_allocation("EURUSD", "BUY", 0.2, self.broker.get_positions())
        results.append({
            "step": "6. Portfolio Risk Allocation",
            "status": "PASS" if ok_alloc else "FAIL",
            "details": reason_alloc
        })

        passed_count = sum(1 for r in results if r["status"] == "PASS")
        total_count = len(results)

        return CanaryTestReport(
            total_steps=total_count,
            passed_steps=passed_count,
            failed_steps=total_count - passed_count,
            is_fully_healthy=(passed_count == total_count),
            step_results=results
        )


if __name__ == "__main__":
    runner = CanarySoakRunner()
    report = runner.run_all_canary_checks()
    print("\n=== CANARY SOAK TEST REPORT ===")
    for s in report.step_results:
        print(f"[{s['status']}] {s['step']} - {s['details']}")
    print(f"Total: {report.passed_steps}/{report.total_steps} Passed. Fully Healthy: {report.is_fully_healthy}\n")

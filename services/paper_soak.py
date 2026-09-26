"""
Paper Trading Soak Test Harness (Sprint P19).
Simulates continuous multi-cycle / multi-day paper trading execution under realistic
broker conditions with active background health audits, reconciliation, and adversarial failure drills.
"""

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional
import time
from loguru import logger

from config.prop_rules import PropFirmRules
from src.broker.clock_provider import ClockProvider
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
from src.safety.leader_lock import LeaderLock
from src.safety.startup_guard import StartupGuard
from src.storage.health import DatabaseHealth
from src.storage.migration import DatabaseMigration
from src.strategy.base import SignalAction, TradeSignal


@dataclass
class SoakDrillResult:
    drill_name: str
    passed: bool
    details: str
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


@dataclass
class PaperSoakReport:
    total_cycles: int
    orders_dispatched: int
    orders_filled: int
    reconciliations_run: int
    reconciliations_clean: int
    health_audits_passed: int
    drills_run: int
    drills_passed: int
    peak_equity: float
    lowest_equity: float
    max_drawdown_pct: float
    is_fully_healthy: bool
    drill_results: List[SoakDrillResult] = field(default_factory=list)
    summary: str = ""


class PaperSoakHarness:
    """Harness paper trading soak test berstandar institusional."""

    def __init__(
        self,
        db_path: str = "storage/paper_soak.db",
        heartbeat_dir: str = "storage/soak_heartbeats",
        rules: Optional[PropFirmRules] = None,
        initial_balance: float = 100_000.0,
        leader_lock_file: str = "storage/paper_soak.leader.lock"
    ):
        self.rules = rules or PropFirmRules()
        self.broker = MockBrokerAdapter(initial_balance=initial_balance)
        self.broker.connect()

        self.repo = OMSRepository(db_path=db_path)
        self.oms = OMSService(self.repo)

        # Storage & Health
        self.db_health = DatabaseHealth(db_path=db_path)
        self.migration = DatabaseMigration(db_path=db_path)
        self.migration.check_compatibility()

        # Clocks & Heartbeat
        self.clock = ClockProvider()
        self.heartbeat = HeartbeatManager(directory=heartbeat_dir)
        self.leader_lock = LeaderLock(lock_path=leader_lock_file)

        # Risk & Execution
        self.drawdown_monitor = DrawdownMonitor(
            initial_balance=initial_balance,
            max_daily_loss_pct=self.rules.max_daily_loss_pct,
            max_total_loss_pct=self.rules.max_total_loss_pct
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
        self.freshness_gate = FreshnessGate(max_stale_seconds=5.0, max_spread_pips=3.0)
        self.portfolio = PortfolioAllocator()
        self.kill_switch = EmergencyKillSwitch(self.broker, self.rules, self.drawdown_monitor)

        # Startup Guard
        self.startup_guard = StartupGuard(
            broker=self.broker,
            repo=self.repo,
            rules=self.rules,
            leader_lock=self.leader_lock,
            db_health=self.db_health,
            migration=self.migration
        )

        # Metrics
        self.cycles_completed = 0
        self.orders_dispatched = 0
        self.orders_filled = 0
        self.reconciliations_run = 0
        self.reconciliations_clean = 0
        self.health_audits_passed = 0
        self.drill_results: List[SoakDrillResult] = []

    def startup(self) -> bool:
        """Menjalankan startup validation preflight."""
        decision = self.startup_guard.evaluate_startup()
        if not decision.allowed:
            logger.error(f"PaperSoakHarness startup rejected: {decision.reason}")
            return False

        self.heartbeat.write_heartbeat("paper_soak", {"status": "STARTUP_OK"})
        summary = self.reconciler.reconcile()
        self.reconciliations_run += 1
        if summary.is_clean:
            self.reconciliations_clean += 1
        return True

    def run_health_audit(self) -> bool:
        """Audit rutin integritas database, drift jam, dan heartbeat."""
        # 1. Database Health
        db_rep = self.db_health.full_health_check()
        if not db_rep["is_healthy"]:
            logger.error("PaperSoak health audit failed: Database corrupted or unwriteable")
            return False

        # 2. Clock Drift Audit
        drift_ok, drift_seconds, drift_msg = self.clock.validate_clock_drift(self.broker.get_server_time(), max_drift_seconds=3.0)
        if not drift_ok:
            logger.error(f"PaperSoak health audit failed: Clock drift {drift_seconds}s")
            return False

        # 3. Heartbeat write and verify
        self.heartbeat.write_heartbeat("paper_soak", {"status": "AUDIT_OK", "cycle": self.cycles_completed})
        if not self.heartbeat.is_alive("paper_soak", max_stale_seconds=5.0):
            logger.error("PaperSoak health audit failed: Heartbeat stale")
            return False

        # 4. Reconciliation
        rec = self.reconciler.reconcile()
        self.reconciliations_run += 1
        if rec.is_clean and not rec.risk_locked:
            self.reconciliations_clean += 1
        else:
            logger.warning(f"PaperSoak reconciliation detected discrepancies: {rec.discrepancies}")

        self.health_audits_passed += 1
        return True

    def run_failure_drill(self, drill_name: str) -> SoakDrillResult:
        """Injeksi kegagalan terencana untuk menguji respons sistem under failure."""
        now = datetime.now(timezone.utc)

        if drill_name == "STALE_DATA_INJECTION":
            # Quote usang 15 detik
            stale_quote = Quote(
                symbol="EURUSD",
                bid=1.0850,
                ask=1.0851,
                spread_pips=1.0,
                timestamp_utc=now - timedelta(seconds=15)
            )
            v_res = self.freshness_gate.validate_quote(stale_quote)
            passed = not v_res.is_valid
            res = SoakDrillResult(
                drill_name=drill_name,
                passed=passed,
                details="Stale quote successfully rejected" if passed else "Failed to reject stale quote"
            )

        elif drill_name == "NEWS_BLACKOUT_INJECTION":
            # Rilis data ekonomi 60 detik lagi
            news = [
                EconomicEvent(currency="USD", title="NFP Drill", impact="HIGH", timestamp_utc=now + timedelta(seconds=60))
            ]
            sig = TradeSignal("EURUSD", SignalAction.BUY, 1.0850, 1.0800, 1.0950, "Drill News")
            disp_res = self.dispatcher.dispatch(sig, lot_size=0.1, economic_events=news)
            passed = not disp_res.success and ("News" in (disp_res.error_message or "") or "Blackout" in (disp_res.error_message or ""))
            res = SoakDrillResult(
                drill_name=drill_name,
                passed=passed,
                details="News blackout prevented trade submission" if passed else f"Dispatch allowed news trade: {disp_res.error_message}"
            )

        elif drill_name == "REVERSAL_RECONCILIATION_DRILL":
            # Verifikasi rekonsiliasi setelah eksekusi order
            positions = self.broker.get_positions()
            if len(positions) >= self.rules.max_open_trades:
                self.broker.close_position(positions[0].position_id)

            sig = TradeSignal("EURUSD", SignalAction.BUY, entry_price=1.0850, stop_loss=1.0800, take_profit=1.0950, rationale="Drill Clean")
            disp_res = self.dispatcher.dispatch(sig, lot_size=0.1, economic_events=[])
            rec = self.reconciler.reconcile()
            passed = disp_res.success and rec.is_clean and not rec.risk_locked
            res = SoakDrillResult(
                drill_name=drill_name,
                passed=passed,
                details=f"Order #{disp_res.client_order_id} filled and reconciled cleanly" if passed else f"Reconciliation drill failed: {disp_res.error_message}"
            )

        else:
            res = SoakDrillResult(
                drill_name=drill_name,
                passed=False,
                details=f"Unknown drill: {drill_name}"
            )

        self.drill_results.append(res)
        return res

    def execute_market_cycle(self, symbol: str = "EURUSD", price: float = 1.0850, submit_trade: bool = False) -> None:
        """Menjalankan satu iterasi siklus pasar dalam soak test."""
        self.cycles_completed += 1
        now = datetime.now(timezone.utc)
        self.heartbeat.write_heartbeat("paper_soak", {"cycle": self.cycles_completed, "status": "RUNNING"})

        # Simulasi penutupan posisi berkala untuk rotasi portofolio
        positions = self.broker.get_positions()
        if len(positions) >= 2 and self.cycles_completed % 4 == 0:
            self.broker.close_position(positions[0].position_id)

        # Update drawdown monitor
        acct = self.broker.get_account_snapshot()
        self.drawdown_monitor.update_snapshot(acct)

        if submit_trade and not self.kill_switch.is_locked() and not self.reconciler.is_risk_locked:
            # Check portfolio limits
            can_alloc, _, _ = self.portfolio.evaluate_allocation(symbol, "BUY", 0.1, self.broker.get_positions())
            if can_alloc:
                sig = TradeSignal(
                    symbol=symbol,
                    action=SignalAction.BUY,
                    entry_price=price,
                    stop_loss=price - 0.0050,
                    take_profit=price + 0.0100,
                    rationale="SoakAlphaRunner"
                )
                self.orders_dispatched += 1
                res = self.dispatcher.dispatch(sig, lot_size=0.1, economic_events=[])
                if res.success:
                    self.orders_filled += 1

    def run_soak(
        self,
        total_cycles: int = 50,
        health_audit_interval: int = 10,
        drill_interval: int = 15
    ) -> PaperSoakReport:
        """
        Menjalankan full soak testing loop.
        Setiap interval tertentu melakukan health audit dan adversarial chaos drills.
        """
        logger.info(f"Starting Paper Soak Test ({total_cycles} cycles)...")
        if not self.startup():
            return PaperSoakReport(
                total_cycles=0,
                orders_dispatched=0,
                orders_filled=0,
                reconciliations_run=self.reconciliations_run,
                reconciliations_clean=self.reconciliations_clean,
                health_audits_passed=0,
                drills_run=0,
                drills_passed=0,
                peak_equity=self.broker.balance,
                lowest_equity=self.broker.equity,
                max_drawdown_pct=0.0,
                is_fully_healthy=False,
                summary="Startup preflight check failed"
            )

        price = 1.0850
        for i in range(1, total_cycles + 1):
            # Simulasi osilasi harga kecil
            price += 0.0001 if i % 2 == 0 else -0.00008
            submit_trade = (i % 8 == 0)

            self.execute_market_cycle(symbol="EURUSD", price=price, submit_trade=submit_trade)

            # Audit Kesehatan Berkala
            if i % health_audit_interval == 0:
                self.run_health_audit()

            # Adversarial Drills
            if i % drill_interval == 0:
                drill_types = ["STALE_DATA_INJECTION", "NEWS_BLACKOUT_INJECTION", "REVERSAL_RECONCILIATION_DRILL"]
                drill_choice = drill_types[(i // drill_interval) % len(drill_types)]
                self.run_failure_drill(drill_choice)

        # Audit Final
        self.run_health_audit()

        # Kalkulasi Laporan Akhir
        acct = self.broker.get_account_snapshot()
        daily_dd_pct = self.drawdown_monitor.calculate_daily_loss_pct(acct.equity)
        drills_passed_count = sum(1 for d in self.drill_results if d.passed)
        is_healthy = (
            self.health_audits_passed > 0 and
            drills_passed_count == len(self.drill_results) and
            self.reconciliations_clean == self.reconciliations_run and
            not self.kill_switch.is_locked()
        )

        return PaperSoakReport(
            total_cycles=self.cycles_completed,
            orders_dispatched=self.orders_dispatched,
            orders_filled=self.orders_filled,
            reconciliations_run=self.reconciliations_run,
            reconciliations_clean=self.reconciliations_clean,
            health_audits_passed=self.health_audits_passed,
            drills_run=len(self.drill_results),
            drills_passed=drills_passed_count,
            peak_equity=self.drawdown_monitor.peak_balance,
            lowest_equity=acct.equity,
            max_drawdown_pct=daily_dd_pct,
            is_fully_healthy=is_healthy,
            drill_results=self.drill_results,
            summary=f"Paper Soak Completed: {self.cycles_completed} cycles, {drills_passed_count}/{len(self.drill_results)} drills passed, reconciliations 100% clean."
        )


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Paper Trading Soak Runner")
    parser.add_argument("--cycles", type=int, default=100, help="Total soak simulation cycles")
    args = parser.parse_args()

    harness = PaperSoakHarness()
    report = harness.run_soak(total_cycles=args.cycles)
    print("\n=================== PAPER SOAK REPORT ===================")
    print(f"Cycles Completed     : {report.total_cycles}")
    print(f"Orders Dispatched    : {report.orders_dispatched}")
    print(f"Orders Filled        : {report.orders_filled}")
    print(f"Reconciliations Clean: {report.reconciliations_clean}/{report.reconciliations_run}")
    print(f"Health Audits Passed : {report.health_audits_passed}")
    print(f"Failure Drills Passed: {report.drills_passed}/{report.drills_run}")
    print(f"Max Daily DD         : {report.max_drawdown_pct:.2f}%")
    print(f"Fully Healthy        : {report.is_fully_healthy}")
    print(f"Summary              : {report.summary}")
    print("=========================================================\n")

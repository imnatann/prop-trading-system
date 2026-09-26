# FundingPips MT5 Execution Integration

Target: **FundingPips Free Trial** on **MetaTrader 5**, server **`FundingPips-Trial`**, initial
instrument **EURUSD**.

> ## ⚠️ DO NOT PUT CREDENTIALS IN SOURCE CODE
> No login, password, investor password, or email may ever appear in a source file, a
> test, a docstring, a comment, a notebook, a JSON manifest, a pytest snapshot, a
> command-line argument, or a log line. Credentials come from the **environment only**.
> See [Security model](#security-model).

---

## 1. Architecture

The single most important property of this integration is that **research and execution
never touch each other's concerns**.

```
HISTORICAL RESEARCH                          LIVE / FORWARD EXECUTION
(provider-agnostic)                          (FundingPips only)

  historical / canonical data                  FundingPips MT5 terminal
            |                                            |
            v                                            v
   Alpha / Research / Walk-Forward            FundingPipsMT5Adapter
            |                                 (src/execution/fundingpips_mt5.py)
            v                                            |
   research/backtest/simulator_v2              +--> symbol specification
   (deterministic, offline,                    +--> live Bid/Ask
    broker-independent)                        +--> spread telemetry
            ^                                  +--> account information
            |                                  +--> order execution + preflight
            |                                  +--> fills, commission, swap
            |                                  +--> execution/slippage telemetry
            |                                            |
            |                                            v
            |                                  data/execution/fundingpips/
            |                                  (calibration telemetry)
            |                                            |
            +--------------------------------------------+
             FundingPipsExecutionProfile
                  -> SimulationCostParameters
                  (explicit conversion, applied by the OPERATOR)
```

Three rules make that diagram true in code, and each is guarded by a test:

1. **simulator_v2 never imports MT5 or the execution layer.**
   `tests/execution/test_simulator_independence.py` parses `simulator_v2.py` with
   `ast` and asserts it references neither.
2. **Execution never imports a concrete alpha.** No file under `src/execution/` may
   import `alpha_v2_mtf`, `zscore_scalper`, or `trend_v1`. Strategy and risk code
   consume generic `OrderIntent` objects and depend on the `ExecutionBroker`
   *protocol*, never on `FundingPipsMT5Adapter`.
3. **Alpha never learns FundingPips exists.** No file under `src/strategy/` may
   contain the string "fundingpips" or "MetaTrader5".

### Module map

| Path | Responsibility |
| :--- | :--- |
| `src/execution/base.py` | `ExecutionBroker` protocol, legacy↔modern bridges, provider factory |
| `src/execution/fundingpips_mt5.py` | The MT5 adapter: connect, read, preflight, execute |
| `src/execution/models.py` | Broker-agnostic domain models (`OrderIntent`, `SymbolInfo`, `Tick`, …) |
| `src/execution/errors.py` | Domain exceptions; every message redacted on construction |
| `src/execution/redaction.py` | `Secret`, `redact()`, `mask_account()` — the single scrubbing choke point |
| `src/execution/symbol_mapper.py` | Deterministic `EURUSD` → provider-symbol resolution |
| `src/execution/execution_profile.py` | Measured broker characteristics + simulator cost conversion |
| `src/execution/telemetry.py` | Spread recording, structured events, single-counted cost attribution |
| `config/fundingpips.py` | Env-only credentials, the trading gate, fail-closed validation |
| `research/execution/calibrate_fundingpips.py` | Profile → `SimulationCostParameters` + calibration report |

### Migration is non-destructive

The pre-existing `BaseBrokerAdapter` ABC, `MockBrokerAdapter`, `MT5BrokerAdapter`,
`OrderDispatcher`, OMS, reconciliation, and safety stack are **unchanged**. They are
bridged rather than forked:

* `BrokerExecutionAdapter` — drives a modern `ExecutionBroker` from code that expects
  the legacy ABC.
* `LegacyExecutionAdapter` — exposes the legacy mock/MT5 adapters through the modern
  protocol.

Provider selection is centralised in exactly one place:

```bash
EXECUTION_PROVIDER=fundingpips_mt5   # default
# other values: mt5_legacy | mock
```

There are no scattered `if fundingpips:` branches anywhere in the repository, and a test
enforces that.

---

## 2. Security model

### Environment variables only

| Variable | Required | Meaning |
| :--- | :--- | :--- |
| `FUNDINGPIPS_MT5_LOGIN` | yes | The MT5 **account number**. **Not the email.** |
| `FUNDINGPIPS_MT5_PASSWORD` | yes | The **TRADING** password. Never the investor password. |
| `FUNDINGPIPS_MT5_SERVER` | yes | Must be `FundingPips-Trial`. |
| `FUNDINGPIPS_MT5_PATH` | no | Full path to `terminal64.exe` on Windows. |
| `FUNDINGPIPS_ALLOW_TRADING` | no | `false` by default. See [Real-trading safety](#4-real-trading-safety). |

> **The investor password is read-only and must never be used by the execution engine.**
> Only the trading password is used, and only by `mt5.login()`.

Copy `.env.example` to `.env` (gitignored) and fill it in. Values already present in the
real process environment always win over the file (`load_dotenv(override=False)`).

### Defence in depth

| Control | Implementation |
| :--- | :--- |
| No credential in `repr()` | The password lives in a `Secret` wrapper whose `__repr__`/`__str__` return `***REDACTED***`; the real value needs an explicit `.reveal()` |
| No credential in exceptions | `ExecutionError.__init__` runs the message through `redact()` before `RuntimeError` ever sees it |
| No credential in logs/telemetry | `exec_event()` redacts every field before emitting |
| No credential in JSON | `redact_mapping()` blanks secret-keyed fields; the account number is emitted only as `login_masked` |
| No credential in argv | Nothing takes a password as a CLI argument, by design |
| No credential in the profile artifact | `FundingPipsExecutionProfile.to_dict()` passes through `redact_mapping` |
| `Secret` cannot be serialized | `__reduce__` raises `TypeError`, so it cannot reach a manifest or snapshot |
| Account masking | `mask_account(12345678)` → `****5678` for operator-facing output |

A **static AST scan** (`tests/execution/test_secret_redaction.py`) walks every
FundingPips source file looking for credential-shaped string literals and long numeric
literals. It is AST-based rather than grep-based so that a docstring saying the word
"password" is not a false positive while a real credential literal is caught.

`.gitignore` excludes `.env`, `.env.*`, key material, `credentials.json`,
`secrets.json`, and `data/execution/` — while explicitly keeping `!.env.example`.

---

## 3. MT5 prerequisites and platform support

The official `MetaTrader5` Python package is **Windows-only**. This repository does not
pretend otherwise.

| Host | Behaviour |
| :--- | :--- |
| **Windows** + MT5 terminal + `pip install MetaTrader5` | Full functionality |
| **macOS / Linux (incl. this dev machine)** | `UnsupportedExecutionEnvironmentError` with an actionable message. **Connectivity is never faked.** |

On an unsupported host the adapter still:

1. preserves the execution interface,
2. raises a clear `UnsupportedExecutionEnvironmentError` (`MT5UnavailableError` subclass),
3. documents the supported deployment path,
4. is ready to run unchanged on a Windows MT5 host/VPS.

**Importing the repository never requires MetaTrader5.** `MetaTrader5` is imported lazily
inside `import_mt5()` and only there; a test asserts that no FundingPips module imports it
at module level. `pytest` passes in full on a machine with no MT5 runtime.

MT5 is **not** in `requirements.txt`. Install it only on the Windows host:

```bat
pip install -r requirements-mt5.txt
```

There is no MQL5 bridge in this repository, and this task deliberately did not invent a
networking protocol. The supported deployment is a Windows MT5 host/VPS.

---

## 4. Real-trading safety

**Real trading is DISABLED BY DEFAULT.**

With `FUNDINGPIPS_ALLOW_TRADING=false` the adapter may:

* connect
* inspect the account
* inspect symbols
* read quotes and record spread
* inspect open positions and working orders

It may **NOT** submit an order, modify an order, close a position, or cancel an order.
Those paths raise `TradingDisabledError`.

### Two independent opt-ins

An order requires **both**:

1. **Configuration:** `FUNDINGPIPS_ALLOW_TRADING=true`
2. **Invocation:** the explicit `--allow-order` CLI flag

Neither alone is sufficient. The environment flag alone raises
`TradingDisabledError("... explicit ... flag ...")`; the flag alone leaves
`can_trade` false. Setting the environment variable never enables real trading by
itself. Tests cover all four combinations.

### Fail-closed connection

`connect()` fails **closed** on: missing credentials, a server that is not
`FundingPips-Trial`, failed initialization, failed login, a terminal that is not
connected, an account that does not match the requested login, or a server the terminal
reports as unexpected.

* There is **no fallback** to another broker or server.
* Authentication failures are **never retried** — a test asserts `login` is called
  exactly once, because repeating a bad credential can lock the account.

---

## 5. Commands

Set the environment first (or use a gitignored `.env`):

```bash
export FUNDINGPIPS_MT5_LOGIN=...        # MT5 account number
export FUNDINGPIPS_MT5_PASSWORD=...     # trading password
export FUNDINGPIPS_MT5_SERVER=FundingPips-Trial
# export FUNDINGPIPS_MT5_PATH="C:\Program Files\MetaTrader 5\terminal64.exe"
```

### 5.1 Connection check (read-only)

```bash
python -m scripts.fundingpips_connection_check
```

Prints masked account identity, server, trading permission, resolved symbol metadata, and
the live quote. Shows `Can place orders: NO (two gates required)`. **Structurally cannot
place an order** — the AST test asserts it never calls a write method.

### 5.2 Symbol probe

```bash
python -m scripts.fundingpips_symbol_probe EURUSD
python -m scripts.fundingpips_symbol_probe EURUSD --save-profile
python -m scripts.fundingpips_symbol_probe EURUSD --list-matches 20
```

Prints every relevant MT5 symbol field as human-readable text **and** JSON-safe output.
`--save-profile` writes `data/execution/fundingpips/execution_profile.json`, which
contains **no credentials** (the account appears only as `login_masked`).

### 5.3 Spread recorder

```bash
python -m scripts.fundingpips_record_spread --symbol EURUSD --duration 3600
```

Connects read-only, resolves EURUSD, samples live bid/ask, and appends UTC-stamped JSONL
to `data/execution/fundingpips/canonical/`. Ctrl+C finalises cleanly. Persistence uses
`O_APPEND` + `fsync`, so a crash cannot rewrite earlier observations. **Places no order
and imports no strategy.**

### 5.4 Safe smoke test

```bash
# Dry run (default): full preflight, sends nothing
python -m scripts.fundingpips_smoke_order --symbol EURUSD --volume 0.01

# Real single order: BOTH gates
FUNDINGPIPS_ALLOW_TRADING=true \
python -m scripts.fundingpips_smoke_order --symbol EURUSD --volume 0.01 --allow-order
```

Purpose is to **validate execution plumbing and accounting, not profit**. Manual sequence:

1. `BUY 0.01` → record bid/ask, request, fill, then close → record fill, commission, swap
2. separately `SELL 0.01` → record the same fields

It never loops, never martingales, and never retries a rejected order with a larger
volume. The default hold respects `FUNDINGPIPS_2STEP.min_trade_duration_seconds` (60 s)
taken from the existing prop rules, because FundingPips treat sub-second round trips as
prohibited high-frequency activity.

---

## 6. EURUSD symbol discovery

The provider symbol is **never assumed** to be exactly `EURUSD`. Resolution is
deterministic, in this order:

1. exact — `EURUSD`
2. dotted suffix — `EURUSD.*`
3. prefix — `EURUSD*`
4. known variant affixes — `EURUSD.r`, `EURUSD_raw`, `#EURUSD`, …
5. separator-insensitive — `EUR/USD`

If two candidates **tie at the same rank**, resolution fails with
`AmbiguousSymbolError` listing every candidate. It never silently selects one — that is
precisely how an order lands on the wrong instrument. The result records both the
canonical symbol and the provider symbol.

---

## 7. Execution profile

`FundingPipsExecutionProfile` is built from **live MT5 metadata**, not retail defaults.
It captures `digits`, `point`, `trade_tick_size`, `trade_tick_value`,
`contract_size`, `volume_min/max/step`, `stops_level`, `freeze_level`,
`trade_mode`, `swap_long/short`, `swap_mode`, `swap_rollover3days`,
`currency_base/profit/margin`, `execution_mode`, `filling_modes`.

**Pip size is derived, never hardcoded**: a 3- or 5-digit quote uses `point * 10`;
other instruments use `point`. That is why USDJPY correctly yields a 0.01 pip and
XAUUSD a 0.01 pip, without a hardcoded branch per symbol. Swap, which MT5 quotes in
points, is converted to pips using the derived pip size.

---

## 8. Calibration telemetry

```
data/execution/fundingpips/raw/         <- un-normalised provider records
data/execution/fundingpips/canonical/   <- normalised UTC spread samples
data/execution/fundingpips/execution_profile.json
data/execution/fundingpips/calibration_report.json
```

These are **execution calibration** data, written only under `data/execution/`. They are
deliberately kept out of any historical research directory, because the two serve
different purposes: one calibrates execution, the other would validate alpha. A test
asserts the directories never converge.

> **Note:** the TwelveData historical data integration was removed along with the
> research data pipeline (`research/data/`). Execution reads live MT5 data directly and
> requires no historical data provider. See section 14.

Recorded fields: `timestamp_utc`, `canonical_symbol`, `provider_symbol`, `bid`,
`ask`, `mid`, `spread_price`, `spread_points`, `spread_pips`, plus optional
`terminal_time_utc`, `local_receipt_utc`, and a latency proxy.

The calibration report contains **spread by UTC hour, spread by session, and median /
p75 / p90 / p95 / p99**. It contains **no performance metric** — no Sharpe, P&L, profit
factor, win rate, or configuration ranking. Two tests enforce that, one of them AST-based.

### Cost attribution cannot double-count

Once real fills exist, `attribute_round_trip()` decomposes a round trip into gross /
spread / slippage / commission / financing. Two design points matter:

* The **observed bid/ask spread must be supplied**, because a fill price alone cannot
  distinguish spread from slippage. Inferring it from `|fill − mid|` would force
  slippage to be identically zero — the exact bug the signature exists to prevent.
* Slippage is measured as the per-side deviation from the expected touch price, and the
  function **asserts** that `spread_cost + slippage_cost` reconstructs the true
  execution cost. If they disagree, it raises rather than reporting a wrong split.

---

## 9. How simulator_v2 consumes calibrated values

`simulator_v2` is **not modified** and stays broker-independent. The conversion is an
explicit operator step:

```python
from research.execution.calibrate_fundingpips import to_simulation_cost_parameters
from research.backtest.simulator_v2 import CostSpec

params = to_simulation_cost_parameters(profile, spread_quantile="p50")
costs = CostSpec(
    spread_pips=params.spread_pips,
    slippage_pips=params.slippage_pips,
    commission_per_lot_per_side=params.commission_per_lot_per_side,
    swap_long_pips=params.swap_long_pips,
    swap_short_pips=params.swap_short_pips,
)
```

The dependency arrow points **execution → research**, never the reverse. Nothing changes
simulator_v2's constants automatically; observations are collected first, and a profile
with no telemetry records its own weakness in `notes` (`no_spread_telemetry...`,
`no_fill_evidence...`) rather than looking precise.

---

## 10. Order preflight

Every order passes one centralised check, and the policy is **reject over repair**:

symbol visibility, trade mode, direction permission, volume > 0, `volume_min`,
`volume_max`, alignment to `volume_step`, SL/TP distance against `stops_level`,
account trading permission, quote validity (`bid < ask`), spread ceiling, margin
plausibility, and duplicate `client_order_id` exposure.

Failures raise `OrderPreflightError` with a structured `reason_code`
(`volume_below_minimum`, `volume_not_aligned_to_step`, `stop_loss_too_close`, …).
**The volume is never silently adjusted.** If risk sizing returns a volume below the
broker minimum, the correct outcome is **NO TRADE** — which is exactly what
`PositionSizer` already does and what its existing tests already assert.

---

## 11. Risk engine integration

`PositionSizer` and `RiskGatekeeper` are reused **unchanged**. The FundingPips adapter
respects the risk engine's output and does not re-size. The only place a 0.01 lot is
forced is the explicit smoke-test utility. Production strategy behaviour is untouched:

```
risk_size < broker volume_min  =>  NO TRADE
```

---

## 12. News / policy gate

Policy is kept **separate from alpha**. The strategy asks the policy object, and the
policy object may consume events of the form `timestamp, currency, event_name, impact,
restricted` (for EURUSD the relevant currencies are EUR and USD). No undocumented
FundingPips rules are hardcoded; only timing rules already present in `PropFirmRules` or
explicitly supplied by the operator are applied.

---

## 13. Trial-environment discipline

FundingPips Free Trial is an **execution calibration** environment. Its short sample is
**not** used to optimise Alpha v2, tune parameters, or rank configurations. It may be
used for: spread calibration, symbol metadata, fill behaviour, commission and swap
observations, execution plumbing, EA connectivity, risk controls, order lifecycle, and
telemetry.

It is **not** the primary historical alpha-validation dataset.

---

## 14. Research protocol protection

`docs/RESEARCH_PROTOCOL_v2.md` is **unmodified**. The following are unchanged: the four
frozen Alpha v2 configurations, the Sharpe hurdle, the t-stat hurdle, the cost-stress
requirement, the cross-pair breadth requirement, the walk-forward folds, the VR
methodology, and the data-admission threshold.

This integration was **not** used as an excuse to change the research hypothesis. No Alpha
v2 run occurred, and no performance metric was generated: no Sharpe, P&L, profit factor,
win rate, or configuration ranking exists for Alpha v2 as a result of this work. Tests
assert that no research-protocol module even mentions FundingPips or MT5.

---

## 15. Error model

`MT5UnavailableError`, `UnsupportedExecutionEnvironmentError`, `MissingCredentialError`,
`CredentialFormatError`, `MT5InitializationError`, `MT5AuthenticationError`,
`UnexpectedServerError`, `AccountMismatchError`, `SymbolNotFoundError`,
`AmbiguousSymbolError`, `TradingDisabledError`, `OrderPreflightError`,
`ConnectionLostError`, `DuplicateOrderError`.

Each is domain-specific for auditability, inherits `ExecutionError`, and redacts its own
message and context on construction.

---

## 16. Observability

`exec_event()` emits one structured, redacted JSON line per event with fields
`timestamp`, `component`, `event`, and optional `success`, `reason`,
`canonical_symbol`, `provider_symbol`, `server`.

Connection events are `login_attempt`, `login_success`, `login_failure` — never
containing the password. The account appears only as `login_masked`.

---

## 17. Running the tests

```bash
pytest                                    # full suite, no MT5 required
pytest tests/execution -q                 # the FundingPips integration
```

No test requires a real FundingPips connection, a terminal, a network, or credentials.
`tests/execution/fake_mt5.py` provides a fake gateway that mirrors the MT5 API surface
and **records every call**, which is how tests prove properties like "authentication
failure is never retried" and "trading disabled sends no order".

---

## 18. Deployment on a supported MT5 host

1. Provision a **Windows** VPS or host.
2. Install the MetaTrader 5 terminal and log into **FundingPips Free Trial** once, manually.
3. `pip install -r requirements-mt5.txt`
4. Copy `.env.example` to `.env` and fill in the three required variables.
5. Set `FUNDINGPIPS_MT5_PATH` to `terminal64.exe` if the package cannot locate it.
6. Keep the terminal **running** — the Python API attaches to a live terminal.
7. Verify with `python -m scripts.fundingpips_connection_check` (read-only).
8. Record spread with `python -m scripts.fundingpips_record_spread`.
9. Only then consider the smoke-order phase, and only with both opt-ins.

Leave `FUNDINGPIPS_ALLOW_TRADING=false` for all read-only and calibration work.

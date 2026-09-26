# Research Protocol — Alpha v2 (locked before any result is seen)

**Status: FROZEN.** Written before Alpha v2 is run. Editing this file after seeing
results invalidates the run. Any change must be recorded with the date, the reason,
and an explicit note that prior results are demoted to exploratory.

## Why this file exists

The previous round produced a spurious "winner" (`XSMOM(6m/3m)`, OOS Sharpe 0.613)
that survived only because the search was not pre-registered: 35 configurations were
tried and the best one was reported. The expected best-of-35 Sharpe from pure luck at
T = 2.75y is **1.29**. Pre-registration is the cheapest available defence.

## 1. Hypothesis under test (single, pre-specified)

**H1:** A genuinely multi-timeframe trend/pullback/continuation model on FX majors has a
positive net expectancy after realistic costs, out of sample, under rolling walk-forward.

Specific form, deliberately minimal (low degrees of freedom):

```
H4  regime   : EMA50 > EMA200  => long-only allowed   (mirror for short)
H1  setup    : price retraces toward EMA50 (pullback within the regime)
M15 trigger  : close breaks the previous N-bar high (mirror for low)
```

Exit: ATR-based stop and target, plus a time stop. **Fixed at 2.0 R minimum.**

**Explicitly NOT in scope for H1:** Z-score mean reversion as an entry, order-book
imbalance, ML/RL models, sub-minute data, more than one pattern family.

## 2. Pre-registered parameter grid (this is the ENTIRE search space)

| Knob | Values | Count |
|---|---|---|
| H4 fast EMA | 50 | 1 |
| H4 slow EMA | 200 | 1 |
| H1 pullback depth (ATR) | 0.5, 1.0 | 2 |
| M15 breakout lookback N | 8, 20 | 2 |
| ATR stop multiple | 1.5 | 1 |
| R multiple | 2.0 | 1 |
| Time stop (M15 bars) | 96 | 1 |

**Total: 4 configurations.** Not 4 per fold, not "we will explore" — four.

The grid is intentionally tiny. With 4 configs at T = 2 years the luck hurdle is
Sharpe ≈ 1.11 (see §6). Adding knobs mid-run is a protocol violation.

## 3. Instruments and the independence problem

Universe: **EURUSD, GBPUSD, AUDUSD, USDJPY, USDCAD, USDCHF** (6 majors).

Purpose is NOT more trades. It is to distinguish *"this is an FX phenomenon"* from
*"this was EURUSD in one particular period."* A pattern of `+0.8, +0.6, +0.5, +0.4,
+0.3, +0.2` across pairs is interesting; `+4.2, -0.5, -0.3, ...` is not.

**Independence caveat, enforced in reporting:** these pairs share USD exposure, so N
trades across 6 pairs is NOT N independent observations. Effective breadth is much
lower than the trade count. All headline statistics must therefore be computed on the
**equal-weight portfolio return series**, and per-pair results reported separately as
a robustness view — never pooled into one inflated sample size.

## 4. Ex-ante discipline (the rule that prevents the second kind of look-ahead)

Every gate must use only information available strictly before the trade:

```
ExpectedEdge_ex_ante(t)  >  ExpectedCost(t) + SafetyMargin
```

- `ExpectedEdge_ex_ante` comes from the **training window only**, or from a rolling
  estimate computed on data up to t-1. It is **frozen** before the OOS period begins.
- **Forbidden:** "the backtest shows this setup yields 3 pips, so allow it." That is
  look-ahead in disguise and is the exact error this section exists to prevent.
- The OOS period is used **only** to observe whether the frozen decision was profitable.

Cost estimate itself is ex-ante: a rolling realised spread/slippage estimate, not a
number chosen after seeing the results.

## 5. No-look-ahead on higher timeframes

Resampling alone is not sufficient. At 12:15 the H4 candle 12:00–16:00 is **unfinished**;
its high/low/close are unknown and must never be used.

**Rule:** an HTF signal may only use the **last fully closed** HTF bar, forward-filled
onto the lower timeframe and advanced exactly when the next HTF bar closes.

This is covered by a dedicated test (`test_no_lookahead_htf`) that asserts the
forward-filled HTF value at time t equals the value computed from strictly closed bars.

## 6. Decision thresholds (pre-registered)

**Simon / Harvey-Liu-Zhu:** a newly discovered factor needs **t > 3.0**, not 2.0.

Luck hurdle, expected best Sharpe from pure luck with N configs over T years:

| T | N=4 | N=10 | N=35 |
|---|---|---|---|
| 1y | 1.57 | 1.57 | 2.53 |
| 2y | 1.11 | 1.11 | 1.79 |
| 10y | 0.50 | 0.50 | 0.80 |

**Verdict rule, fixed now:**

- **PASS** requires ALL of:
  1. OOS net Sharpe (portfolio series) **> luck hurdle** for the actual N and T,
  2. OOS t-stat **> 3.0**,
  3. survives **2× cost stress**,
  4. **at least 4 of 6 pairs** positive,
  5. survives Phase 6 falsification (parameter neighbourhood, remove-best-pair,
     remove-best-year).
- **FAIL** is a valid and expected outcome. It means the hypothesis is not supported.
- **Suggested fix for a FAIL is NOT a larger search.** Per the agreed rule: if MTF
  fails, stop optimising and report it.

## 7. VR used only as an ex-ante regime veto

VR is **not** an entry signal. It gates whether trading is allowed at all.

```
sd(VR)_t  <  P20( sd(VR) )   computed on TRAIN ONLY   ->  too random, no trade
|mean(VR)_t - 1| < eps        computed on TRAIN ONLY   ->  no trade
```

Thresholds are estimated on the training window and then frozen. Optimising the
threshold against results (e.g. `sdVR < 0.01372`) is forbidden.

## 8. Walk-forward: fit on train, freeze, test

```
2015-2019 TRAIN -> 2020 TEST
2016-2020 TRAIN -> 2021 TEST
2017-2021 TRAIN -> 2022 TEST   ... rolling
```

- Parameters are selected **only** on the train fold and then frozen.
- Purge + embargo between train and test to kill label overlap leakage.
- **Forbidden:** choosing a parameter because it looked best across the aggregate of
  all test folds, then re-running everything. That is another layer of overfitting.

## 9. Phase 6 falsification (run before any PASS is announced)

The goal is to **kill** the strategy, not to confirm it.

- spread ×1.5 and ×2.0
- slippage stress
- entry delay of 1 bar
- parameter perturbation (neighbouring values must survive)
- remove-best-year
- remove-best-pair
- bootstrap / Monte Carlo on trade order
- subperiod stability
- session subsets
- multiple-testing adjustment with the true N
- portfolio exposure / correlation between pairs

A strategy that only works at EMA 47 but fails at 45 and 50 is not an edge.

## 10. Accounting invariants (Phase 1 acceptance criteria)

These are enforced by tests, not by convention:

```
Equity_t          = Balance_t + UnrealizedPnL_t
FinalEquity       = FinalBalance            (after explicit liquidation)
DeltaBalance      = sum(NetPnL_i)           (all positions closed)
NetPnL_i          = GrossPnL_i - SpreadCost_i - SlippageCost_i - Commission_i - Financing_i
```

Per trade, the following are stored **separately** (never one opaque `pnl`):
`gross_pnl`, `spread_cost`, `slippage_cost`, `commission`, `financing`, `net_pnl`.

`spread_cost` is **attribution only** — it is already embedded in the bid/ask
execution prices and must NOT be debited from the balance a second time.

## 11. Known-and-accepted limitations

- Free data (Yahoo) is indicative OHLC, not executable quotes. Spreads are modelled.
- No tick data, no order book, no real forward points or swap curves.
- Policy rates proxy the forward discount; nominal prices proxy real exchange rates.
- These limitations bound what any PASS can mean: a PASS is a hypothesis that deserves
  forward paper-trading, not a deployable money-maker.

## 12. Protocol amendments (recorded BEFORE Phase 2 ran)

Amendments made before any Alpha v2 result was observed. Strategy hypothesis,
parameter grid, success thresholds and pair universe are unchanged.

| ID | Date | Change | Reason | Effect on prior results |
|---|---|---|---|---|
| A01 | pre-Phase-2 | Intraday data source replaced. Yahoo cannot serve multi-year M15/H1/H4. | Yahoo caps intraday history (M15 ≈ 60d, H1 ≈ 2y). The smallest timeframe is the binding constraint, so switching to D1/H4/H1 would NOT have recovered 20 years: H1/H4 would still be short. | None — no Alpha v2 result existed yet. |
| A02 | pre-Phase-2 | Dukascopy bid/ask and OANDA REJECTED as sources. | Both are network-blocked from this environment. DNS resolves but `datafeed.dukascopy.com`, `www.dukascopy.com` and `api-fxpractice.oanda.com` all resolve to the same address 43.173.57.48 and time out at TLS. Measured, not assumed. | None. |
| A03 | pre-Phase-2 | HistData M1 REJECTED despite being reachable. | `histdata.com` returns 200, but the download endpoint `/get.php` returns 0 bytes and the required `tk` token is populated only by client-side JS. The site is deliberately gated against automated retrieval. We do NOT bypass that. | None. |
| A04 | pre-Phase-2 | Source selected: **TwelveData** (`time_series`), single feed, self-resampled. | Reachable and returns real OHLC intraday. One feed keeps timestamps, timezone, session boundaries and candle construction internally consistent. Depth is materially less than hoped: see §13. | None. |
| A05 | pre-Phase-2 | Spread is MODELLED, not historical. | The preferred bid/ask source (Dukascopy) is unreachable, so no historical spread series is available. `CostSpec.spread_pips` stays a parameter and MUST be stressed in Phase 6 (×1.5, ×2.0). This weakens any PASS. | None. |

### Consequence for statistical power (recorded now, not after seeing results)

Section 6 sets the luck hurdle as a function of T. The reachable intraday depth is
substantially shorter than the 8–10 years targeted, so the hurdle is HIGH and a PASS
is correspondingly hard. This is stated in advance so that a FAIL cannot later be
explained away, and a marginal PASS cannot be oversold.

If the reachable depth proves insufficient for a meaningful walk-forward, the correct
action is to report that the experiment is **underpowered** — not to lower the bar,
and not to substitute a different hypothesis (e.g. weekly/monthly value+momentum)
mid-flight.

## 13. Protocol violation log

## 14. Data admission outcome (recorded 2026-09-24, BEFORE any Alpha v2 run)

The 18-call `earliest_timestamp` probe was executed against a personal TwelveData key.
Result artifact: `data/admission_probe.json`.

| Pair | M15 | H1 | H4 | Pair usable from |
|---|---|---|---|---|
| EUR/USD | 2020-01-30 | 2020-01-30 | 2020-01-30 | 2020-01-30 |
| GBP/USD | 2020-01-30 | 2020-01-30 | 2020-01-30 | **2020-01-30** |
| AUD/USD | 2020-01-29 | 2020-01-29 | 2020-01-29 | 2020-01-29 |
| USD/JPY | 2020-01-29 | 2020-01-29 | 2020-01-29 | 2020-01-29 |
| USD/CAD | 2020-01-29 | 2020-01-29 | 2020-01-29 | 2020-01-29 |
| USD/CHF | 2020-01-29 | 2020-01-29 | 2020-01-29 | 2020-01-29 |

```
T_start = max(all earliest) = 2020-01-30   (binding pair: GBP/USD)
T_end   = min(all latest)   = 2026-09-24
usable  = 6.65 years
required= 7.00 years                       (train 4y + 3 folds x 1y)
VERDICT = UNDERPOWERED
```

All 18 series returned data; there was no entitlement or coverage failure. The
shortfall is depth, and it is a single binding constraint: GBP/USD M15.

### Decision: STOP, per protocol sections 8 and 13

The pre-registered rule applies exactly as written:

> "If the reachable depth proves insufficient for a meaningful walk-forward, the
> correct action is to report that the experiment is **underpowered** - not to lower
> the bar, and not to substitute a different hypothesis mid-flight."

Therefore, the following are explicitly NOT permitted as responses to this result:

1. Lowering `MIN_YEARS` from 7 to 6.65 to make it pass.
2. Reducing the fold count from 3 to 2 to fit the available window.
3. Shortening train from 4y to 3y to manufacture a third fold.
4. Swapping in a different hypothesis (weekly/monthly value+momentum) and presenting
   it as Alpha v2.
5. Widening the pair universe or dropping GBP/USD to move T_start earlier.

Each of these would convert a clean negative into an uninterpretable positive.

### What WOULD legitimately change this outcome

Only a genuine change in available DATA, decided and recorded before any run:

- A paid TwelveData plan exposing longer FX intraday history, verified with the same
  18-call probe, OR
- A different reachable bid/ask or M1 source (Dukascopy and OANDA remain network-blocked
  from this environment; HistData is JS-gated and was not bypassed), OR
- A documented change in the pre-registered fold geometry (see below), which is a
  genuine protocol amendment and must be justified on statistical grounds alone,
  never on observed performance.

### Note on the fold geometry (recorded, not acted upon)

The 7-year requirement is `train 4y + 3 folds x 1y`, chosen so that a 1-year test fold
contains enough trades for the t > 3.0 hurdle. With 6.65 usable years a defensible
alternative geometry exists: `train 4y + 2 folds x 1y = 6y`. That is a REAL option but
it was not the pre-registered one, it weakens the walk-forward, and choosing it now -
after seeing the data window - requires an amendment that must be argued on its own
merits rather than on the fact that it would let the experiment proceed.

It is recorded here so that the option is not quietly forgotten, and equally so that it
is not quietly taken.

## 15. Protocol violation log

| Date | Change | Reason | Effect on prior results |
|---|---|---|---|
| — | none | — | — |



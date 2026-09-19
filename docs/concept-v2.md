# VPA AI Trading Scanner — Concept v2 (Specification)

**Status:** Draft for review by colleague, then approval and freezing as `concept-v2.0`.

**Purpose:** This is the *pre-registered signal definition* referenced throughout Architecture v2. Once frozen, any change to anything in Sections 2–13 is a Class 2 change requiring an experiment-ledger entry. Sections 14–17 are context and may be updated freely.

**Supersedes:** Concept v1. Incorporates the three expert reviews.

**Plain-language summary for the trader:** this document pins down exactly what the scanner measures, exactly what counts as a candidate, and exactly what number it has to beat to be considered worth keeping. It exists so that six months from now nobody can quietly move the goalposts — including us.

---

## 1. Objective and Scope

**What this is:** an attention-ranking tool. Each morning it scans a defined universe of US mid-cap stocks and returns 5–15 tickers worth opening in TradingView, with a short explanation of why.

**What this is not:** a price predictor, a trade recommender, or an execution system. No orders are placed by any component in v2.

**Scope of v2, deliberately narrow:**

| Dimension | v2 scope |
|---|---|
| Universe | ~400 US mid-cap common stocks (Section 2) |
| Timeframes | Daily and 1-hour only. **4-hour is excluded from v2.** |
| Pattern families | Two only (Section 7) |
| Inputs to AI | Numeric features and raw OHLCV. **No chart images in v2.** |
| Output | Ranked shortlist, delivered by email plus push |

The 4-hour timeframe is dropped because US regular hours are 6.5 hours and do not divide into 4-hour bars; the resulting stub bars break cross-bar volume comparison, and the boundaries would not match what TradingView displays. It may return in v3 once the daily/hourly system is validated.

---

## 2. Universe Definition (Point-in-Time)

The universe is a **function evaluated on each rebalance date**, not a fixed list. This is what prevents survivorship bias.

**Rebalance:** first trading day of each calendar month.

**Inclusion criteria, all evaluated as of the rebalance date using only data available on that date:**

1. US common stock, primary listing on NYSE, Nasdaq, or NYSE American
2. Security type is common stock — excludes ETFs, ETNs, closed-end funds, warrants, units, preferred shares
3. Excludes ADRs (different flow characteristics, FX and ex-dividend complications)
4. Market capitalisation between **$2bn and $50bn**
5. Trailing 60-trading-day median dollar volume ≥ **$15 million**
6. Closing price ≥ **$10**
7. At least **250 trading days** of price history

**Selection:** of the qualifying set, take the **top 400** by trailing 60-day median dollar volume.

**Storage:** each monthly snapshot is written once to `universe/YYYY-MM-DD.parquet` and never modified. Historical snapshots retain tickers that have since been delisted or acquired. Every backtest reads the snapshot in force on the date being tested.

**Rationale for mid-caps:** the largest US stocks have volume dominated by index flow, ETF creation/redemption and options hedging — mechanical activity that VPA theory does not describe. The $2bn–$50bn band retains genuine single-name order flow while remaining liquid enough to be tradeable.

---

## 3. Data Contract

**Primary vendor:** Polygon.io, Stocks **Developer** tier — unlimited API calls, ten years of history, minute aggregates, flat files for bulk download, and corporate actions. The 15-minute delay is irrelevant because the scan runs pre-market on the prior session's closed data. *(Verify current pricing and terms at purchase; plans change.)*

**Ingestion:** bulk historical load via flat files; daily incremental via REST after the close.

### 3.1 Price adjustment — critical

- **Store raw, unadjusted OHLCV.** Never overwrite it.
- **Store split and dividend factors in a separate corporate-actions table.**
- **Apply split adjustment only, at read time.**
- **Never apply dividend adjustment to price.**

Reason: standard "adjusted" data scales price for dividends but not volume. Since the entire system compares price movement against volume, dividend adjustment silently corrupts that ratio at every dividend in the history. Dividends are handled as event flags instead (Section 6).

### 3.2 Session handling

- **Regular trading hours only:** 09:30:00–15:59:59 ET. Pre-market and after-hours data are ingested and stored but excluded from all features.
- All timestamps stored in UTC, converted to ET for session logic using an exchange calendar library (not manual offsets — US and EU daylight-saving dates differ).
- Half days (session ends 13:00 ET) are handled explicitly and flagged.

### 3.3 Reconciliation gate

**Before any feature code is written**, run a reconciliation test: 20 tickers across 5 event days (a split, an ex-dividend, an earnings gap, a half day, an index rebalance), compared bar-by-bar against TradingView. Document every discrepancy and its cause. This gates all downstream work.

---

## 4. Bar Construction

### 4.1 Hourly bars

Built from 1-minute regular-hours data, anchored to the session open. Seven slots per normal session:

| slot_index | Window (ET) | Duration |
|---|---|---|
| 0 | 09:30–10:30 | 60 min |
| 1 | 10:30–11:30 | 60 min |
| 2 | 11:30–12:30 | 60 min |
| 3 | 12:30–13:30 | 60 min |
| 4 | 13:30–14:30 | 60 min |
| 5 | 14:30–15:30 | 60 min |
| 6 | 15:30–16:00 | **30 min (stub)** |

Every bar stores `slot_index`, `slot_minutes`, and `is_half_day`. On half days the session ends at 13:00 ET, producing slots 0–2 plus a 30-minute stub.

**The stub is why slot-wise normalisation is mandatory** (Section 5.1). Comparing a 30-minute bar's volume against 60-minute bars measures bar length, not participation.

### 4.2 Daily bars

Aggregated from regular-hours minutes only, 09:30–16:00 ET. This will differ slightly from vendor daily bars, which typically include off-hours activity. **Ours is the authoritative version**; the difference is documented in the reconciliation report.

### 4.3 Data quality

- Minutes with no trades produce no vendor bar; treat as zero volume.
- If a 1-hour slot contains fewer than 5 minutes with trades, mark `low_quality = true`. Low-quality bars are excluded from candidate detection but retained in the data.

---

## 5. Feature Specification

All rolling windows are **strictly trailing and exclude the bar being evaluated.** A window that includes the current bar is look-ahead bias and is the most common silent bug in systems of this kind. This rule has no exceptions.

### 5.1 Volume normalisation

**Hourly (`vol_pct_slot_60`):** percentile rank of `log(volume + 1)` among the same `slot_index` over the trailing **60 sessions**. Requires ≥40 valid observations; otherwise the feature is null and the bar cannot be a candidate.

**Hourly, short window (`vol_pct_slot_20`):** identical, trailing 20 sessions.

**Hourly robust z-score (`vol_z_slot_60`):** `(log(v) − median) / (1.4826 × MAD)` over the same slot and window. Median and MAD rather than mean and standard deviation, because a single volume spike would otherwise inflate the baseline it is being measured against.

**Daily (`vol_pct_d_120`, `vol_pct_d_20`, `vol_z_d_120`):** same construction, trailing 120 and 20 sessions, no slot dimension.

Both the short and long windows are computed and passed forward. Disagreement between them indicates a regime boundary and is itself informative.

### 5.2 Volatility and spread

- `atr20`: Wilder ATR(20) on the relevant timeframe, computed **through the previous bar only**.
- `spread_atr` = true range ÷ `atr20`.
- `spread_pct_slot_60`: percentile rank of true range within the same slot, trailing 60 sessions.

### 5.3 Candle geometry

With `TR` = true range of the bar:

- `body_frac` = |close − open| ÷ TR
- `upper_wick_frac` = (high − max(open, close)) ÷ TR
- `lower_wick_frac` = (min(open, close) − low) ÷ TR
- `close_loc` = (close − low) ÷ (high − low), where 0 = closed at the low, 1 = closed at the high

### 5.4 Price progress

- `ret_atr` = (close − previous close) ÷ `atr20`
- `progress_3`, `progress_5`, `progress_10`: net move over the trailing 3, 5, 10 bars in ATR units
- `cum_vol_pct_3`, `cum_vol_pct_5`: percentile of cumulative volume over the same spans
- `efficiency` = |`ret_atr`| ÷ max(volume ÷ median slot volume, 0.1) — movement achieved per unit of participation

### 5.5 Market-relative features

This addresses the problem that on a day when the index falls 2%, hundreds of stocks show weak closes and elevated volume, and the shortlist would return twelve tickers that are economically one position.

- `beta_60`: OLS beta of daily returns against SPY over the trailing 60 sessions, refreshed weekly.
- `resid_ret_atr` = (`ret` − `beta_60` × `ret_SPY`) ÷ `atr20`
- `sector_resid_ret_atr`: same, against the relevant sector ETF.
- `market_ret_day`, `sector_ret_day`: recorded as context.

### 5.6 Structure

Support and resistance are encoded as **continuous distances in ATR units**, never as binary "at resistance" flags, and never as fitted trendlines.

- `dist_prior_day_high/low`, `dist_prior_week_high/low`, `dist_prior_month_high/low`
- `dist_20d_high/low`, `dist_60d_high/low`
- `dist_nearest_swing_pivot`: pivots identified by ATR-filtered zigzag requiring a counter-move of **1.5 × ATR**. One parameter, scale-invariant.
- `dist_nearest_hvn`: nearest high-volume node from a volume-at-price histogram over the trailing 60 sessions, 50 bins
- `level_touch_count`, `level_age_bars` for the nearest level
- `dist_round_number`: distance to the nearest whole-dollar or half-dollar level, in ATR

### 5.7 Sequence

- `repeat_hv_narrow_5`: count of bars in the trailing 5 with `vol_pct_slot_60` ≥ 90 and `spread_atr` ≤ 0.6
- `repeat_upper_reject_5`: count of bars in the trailing 5 with `vol_pct_slot_60` ≥ 90 and `upper_wick_frac` ≥ 0.5
- `failed_new_high`: bar's high exceeded the prior 10-bar high, but its close did not exceed the prior 10-bar high close
- `failed_new_low`: mirror image

**All parameters in Section 5 are pre-registered and frozen.** They were chosen by judgement, not optimisation. Changing any of them is a Class 2 change.

---

## 6. Event Flags

Every ticker-day carries flags. These exist so the system never has to guess why volume was unusual.

**Company events:** earnings (D−1, D0, D+1), ex-dividend, split, index addition or deletion, trading halt.

**Calendar events:** monthly option expiry (third Friday), quarterly triple witching, month-end, quarter-end, half day, day after a market holiday.

**Macro events:** FOMC decision day, CPI release, non-farm payrolls.

**Usage differs by context, deliberately:**

- **In validation:** flagged days are **excluded**. We want to know whether the pattern carries information under clean conditions before asking whether it survives contamination.
- **In production:** flagged days are **included but prominently marked**, so the trader sees "elevated volume; ex-dividend today" and discounts accordingly.

**The AI must never infer the cause of an anomaly.** If the event table says nothing, the correct output is "no known event," not a plausible guess.

---

## 7. Pattern Families

Two families only in v2. Both are defined as **permissive candidate filters** — deliberately loose, designed for recall, not to make the final judgement. That is the AI layer's job.

### 7.1 Family A — Effort/Result Anomaly

*The idea in plain terms: a lot of trading happened, and the price barely moved.*

An hourly bar is a Candidate-A if **all** of the following hold:

1. `vol_pct_slot_60` ≥ **90**
2. `spread_atr` ≤ **0.60**
3. |`ret_atr`| ≤ **0.25**
4. |`resid_ret_atr`| ≤ **0.25** *(the stock didn't move even after removing the market's move)*
5. `low_quality` is false
6. No event flag on that date *(validation only; in production, flagged and retained)*
7. **Location:** min distance to any of {20-day high, 20-day low, prior-week high, prior-week low} ≤ **1.5 × daily ATR**

Sequence information (`repeat_hv_narrow_5`) is passed as a **feature**, not used as a filter — the filter stays permissive by design.

### 7.2 Family B — Upper Rejection / Supply

*The idea in plain terms: heavy trading, price pushed up, and then gave it all back and closed weak, right where it previously failed.*

An hourly bar is a Candidate-B if **all** of the following hold:

1. `vol_pct_slot_60` ≥ **90**
2. `upper_wick_frac` ≥ **0.50**
3. `close_loc` ≤ **0.35**
4. `failed_new_high` is true
5. `low_quality` is false
6. No event flag on that date *(validation only)*
7. **Location:** within **2.0 × daily ATR** of a resistance reference — 20-day high, prior-week high, prior-month high, or nearest swing pivot high

### 7.3 Candidate volume budget

Expected yield is roughly 30–60 candidates per day across 400 tickers. **Hard cap: 60 per day.**

If more than 60 fire, rank deterministically by `vol_pct_slot_60` descending and take the top 60, subject to a maximum of 8 per GICS sector so a single sector move cannot consume the entire budget. **The number dropped is recorded**, because a systematically truncated candidate set changes the meaning of every downstream statistic.

---

## 8. What the AI Receives and Returns

### 8.1 Input

For each candidate the AI receives:

- The candidate bar's complete feature vector
- Features and raw OHLCV for the **preceding 30 hourly bars**
- Features and raw OHLCV for the **preceding 60 daily bars**
- All event flags for the ticker over the surrounding week
- Market and sector context for the day
- Cluster information: how many other candidates that day share the same sector or direction

**No chart images in v2.** They are expensive, vision models read chart geometry imprecisely, and the numeric layer already contains the geometry. Images become an ablation experiment in v3: same candidates, same prompt, with and without, measured against the blind labels.

### 8.2 Output — strict JSON schema

```
{
  "ticker": string,
  "signal_timestamp": ISO-8601,
  "pattern_family": "A" | "B" | "neither",
  "evidence_for": [{"claim": string, "feature": string, "value": number}],
  "evidence_against": [{"claim": string, "feature": string, "value": number}],
  "uncertainty": "low" | "medium" | "high",
  "attention_score": 1 | 2 | 3 | 4 | 5,
  "what_to_inspect": string,
  "known_events": [string]
}
```

### 8.3 Constraints on the AI layer

- **Every evidence item must name a feature that exists in the input.** A response citing an unknown feature name is rejected by schema validation and logged as malformed. This makes fabrication mechanically detectable rather than invisible.
- `evidence_against` must be non-empty. If the model can find no contrary evidence, `uncertainty` must be "high".
- `attention_score` is an **ordinal 1–5**, not a continuous score. Language models do not produce well-calibrated numeric scores; asking for one creates false precision.
- The model may return `pattern_family: "neither"` and a low score. Rejecting candidates is a valid and expected output.
- **Model and version are pinned in configuration.** A model upgrade is a Class 2 change requiring re-validation, not a free improvement.

---

## 9. Ranking and Output

**Final ordering:** sort by `attention_score` descending, then `vol_pct_slot_60` descending as a deterministic tiebreak. Take the top 15. No more than 2 per sector.

**Every morning report contains:**

- Ticker, timestamp, timeframes to inspect
- Pattern family and attention score
- Evidence for, evidence against, uncertainty
- Event flags, or "no known event"
- Cluster warning where candidates share a driver
- **Explicitly: "inspect chart — no trade recommendation"**
- Provenance: release tag, feature-code hash, config version, model version

**When there are no candidates**, the report says so explicitly. **When a check fails**, it says `SCAN UNAVAILABLE`. Silence is never a valid state.

---

## 10. Reference Exit Rule (Measuring Stick)

This exists **only to make signals comparable**. It is not a trading strategy and must never be optimised. Frozen permanently.

- **Entry:** open of the next trading day after the signal
- **Stop:** 1.5 × daily ATR(20) adverse
- **Target:** 3.0 × daily ATR(20)
- **Timeout:** 10 trading days, exit at close
- **Family A is direction-agnostic.** Its hypothesis is that something notable follows, not that price falls. Primary metric is the **absolute** ATR-normalised move.
- **Family B is directionally short-biased.** Primary metric is the **signed** move in the hypothesised direction.

Forward returns are additionally recorded at 1, 3, 5 and 10 trading days, ATR-normalised, independent of the exit rule.

---

## 11. Evaluation Protocol

### 11.1 Controls

For each candidate, sample **5 control ticker-days** from the same date and the same universe snapshot, matched on ATR-percentile decile and dollar-volume decile, which did **not** trip any filter.

### 11.2 Baselines

Two, both mandatory:

1. **Random:** N tickers drawn at random from that day's universe.
2. **RVOL screener:** top N by `vol_pct_slot_60` alone, no pattern logic, no AI. **This is the benchmark that matters** — it is what the system claims to improve on.

### 11.3 Metrics

- Full **distribution** of ATR-normalised forward moves at 1/3/5/10 days, versus matched controls. Distribution, not just the mean: for an attention tool, dispersion is the goal.
- R-multiple distribution under Section 10.
- **Precision@10** against blind labels, inverse-probability weighted (Section 12).
- Cohen's kappa between AI pattern labels and trader labels, benchmarked against the trader's own test-retest kappa.

### 11.4 Statistical discipline

- **Standard errors clustered by date.** Candidates on the same day are not independent observations.
- **Walk-forward:** 18 months train / 6 months test, with a **3-month purge gap** between them.
- A result must hold in **at least two thirds of walk-forward folds**, not merely in aggregate.
- **Final 12-month holdout, examined exactly once**, at the end.
- Every variant tried is recorded in the experiment ledger; a false-discovery-rate correction is applied over that count.
- The holdout and evaluation data live **outside the repository**, in a directory neither the cloud coding agent nor Remote Control can read.

---

## 12. Blind Label Protocol

**Purpose:** create an unbiased measure of "worth looking at" that is independent of both the system and hindsight.

**Mechanism:** the chart-reviewer tool shows a chart **truncated at the bar close**. No future price action is rendered or available.

**Sampling — stratified with recorded weights:**

- **One third** drawn from bar-dates on which some candidate fired somewhere in the universe
- **Two thirds** drawn uniformly at random from universe ticker-days
- The **stratum and its inverse-probability weight are recorded**; all analysis reweights to the population. The trader is **never told which stratum a chart came from**.

Pure uniform sampling would yield too few positives to measure precision. Pure candidate sampling would make it impossible to detect what the system misses. Stratification with weights gives both, and is statistically correct.

**Response:** Interesting / Not interesting. Optional one-line note. Optional pattern guess.

**Volume:** 10 charts per session, targeting **300+ labels** over roughly four months.

**Test-retest:** 10% of charts are re-shown after at least 28 days. This measures the trader's self-consistency, which is the **ceiling** on what any model can achieve. If it is 0.5, no system can meaningfully exceed 0.5, and knowing that changes what "good" means.

**Storage:** in the holdout directory, outside the repository.

---

## 13. Kill Criterion

**Stated as numbers, before any data is examined.**

### 13.1 The deterministic filter must earn its place

Retired unless the candidate set's mean absolute 5-day ATR-normalised move exceeds matched controls by **≥ 0.15 ATR**, at **p < 0.01** with date-clustered standard errors, holding in **≥ 2 of 3** walk-forward folds.

### 13.2 The AI layer must earn its cost

Retired — and the system reverts to the deterministic filter alone — unless **both**:

1. The AI-ranked top 10 achieves **precision ≥ 1.3×** the RVOL baseline's top-10 precision against blind labels, on the same days
2. The AI-ranked top 10 shows a **higher dispersion** of forward ATR-normalised moves than the deterministic filter's top 10, at p < 0.05

### 13.3 Minimum sample before any criterion is applied

- **≥ 250 date-clustered effective occurrences per pattern family** — effective meaning after clustering, which will be far fewer than the raw count
- **≥ 3 calendar years**, including at least one SPY drawdown of 15% or more

### 13.4 If the criteria are not met

The pattern family is retired and documented. **This is a successful outcome of the project, not a failure.** The system was built to be capable of returning this answer credibly.

---

## 14. What Is Never Delegated to the AI

- Feature computation, normalisation, bar aggregation, event tagging — all deterministic and reproducible
- Inferring the cause of a price move or volume spike
- Running or re-running the frozen evaluation
- Order placement, in any form
- Treating a pattern label as certainty; alternatives and uncertainty are always preserved
- Optimising toward a return target

---

## 15. Cost Budget

| Item | Estimate |
|---|---|
| Polygon.io Stocks Developer | $79 / month |
| AI runtime (≈50 candidates/day × ~4k tokens, 21 trading days) | $20–40 / month |
| Object storage for raw-data backup (~10 GB) | $1–3 / month |
| Push notification service | $0–5 / month |
| Dead-man monitoring | $0 (free tier) |
| **Total** | **≈ $105–130 / month** |

Comfortably inside the $150–300 budget, with headroom for a heavier AI layer or a wider universe later. **The data subscription dominates; the AI layer is cheap.** That is worth knowing, because it means the AI layer's cost is not a reason to skip the kill-criterion test — its *complexity* is.

Set a hard monthly API spend cap plus an anomaly alert.

**Storage estimate:** 400 tickers × 390 minutes × 252 days × 10 years ≈ 390M rows, roughly 8–15 GB as compressed Parquet. Trivial for the Mac mini.

---

## 16. Open Items

Deliberately unresolved, to be decided with evidence rather than now:

1. **Delisted-security coverage.** Polygon's ticker endpoint includes inactive symbols; confirm during reconciliation that historical minute data for delisted names is actually retrievable. If not, a supplementary source is needed for point-in-time universe integrity.
2. **Sector classification source.** Needed for clustering and the per-sector cap.
3. **Chart images.** Deferred to a v3 ablation.
4. **4-hour timeframe.** Deferred; reconsider only after daily/hourly is validated.

---

## 17. Relationship to Architecture v2

- Sections 2–13 constitute the **pre-registered signal definition**. Code implementing them lives under `src/signal/`. Any pull request touching that path is automatically Class 2 and fails CI without a linked ledger entry.
- Sections 14–17 are context and may be updated without ceremony.
- Section 13's kill criterion is the project's decision gate. It is not advisory.

---

## 18. Reviewer Checklist

For the colleague reviewing this document. Please specifically challenge:

1. Are the thresholds in Section 7 permissive enough? Estimate the actual candidate yield against real data before freezing. If it fires 5 times a day or 500, the numbers need revisiting **now**, not after validation begins.
2. Is the 60-session slot window long enough given seven slots per day?
3. Does the market-residualisation in 5.5 adequately handle sector-wide moves, or is a sector ETF regression also required in v2 rather than as a recorded feature?
4. Is the 1/3–2/3 stratification in Section 12 the right split given a target of 300 labels?
5. Is the 0.15 ATR effect size in 13.1 realistic, or set so high that a real but modest effect would be discarded?

Question 5 matters most. Set the bar too low and we validate noise; too high and we discard something real. It deserves an explicit argument before freezing.

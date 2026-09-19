# VPA Scanner - Mobile-First Development & Operation Architecture v2

Cloud-first coding, isolated runtime, reproducible research, and mobile supervision

**Status: Approved.** Reviewed and accepted — see the second expert review for amendments folded in below (mechanical Class 1/Class 2 enforcement via CI, evaluation-run budgeting, CI credential isolation).

**Purpose:** enable weekday development and review entirely from iPhone/iPad while preserving the statistical and operational discipline of the VPA Scanner. This architecture separates the coding plane from the runtime/data plane so that a home-machine outage cannot stop development, and a coding iteration cannot silently alter the next production scan.

---

## 1. Executive Decision

- **Primary weekday coding route:** Claude Code on the web, working against the private GitHub repository on an Anthropic-managed cloud environment. The Mac mini is not required to be awake for normal coding iterations.
- **Runtime and research route:** Mac mini at home for the scanner, immutable minute-data store, historical replays and data-heavy validation during the research phase.
- **Remote Control:** exception path for tasks that genuinely require the Mac mini's local data, environment or MCP/project configuration — primarily weekend or deliberate research sessions.
- **Integration boundary:** Git. Cloud coding and local runtime do not share a mutable working directory.

## 2. Architecture by Plane

| Plane | Primary location | Purpose | Mobile interaction |
|---|---|---|---|
| Coding / change plane | Claude Code cloud + GitHub | Write/refactor code, unit tests, fixtures, docs, PRs, CI | Claude app/browser + GitHub mobile/web |
| Runtime plane | Mac mini initially | Scheduled morning scan, production feature engine, AI runtime calls, report generation | Receive report; no direct editing of prod |
| Research/data plane | Mac mini + immutable raw store | Historical replay, data reconciliation, large feature jobs, evaluation harness | Triggered deliberately via Remote Control when needed |
| Notification plane | Email + push channel | Durable archive plus immediate alert | Phone/iPad |
| Control / release plane | GitHub + explicit promotion | PR review, CI, tags, production promotion | Mobile approval; promotion remains explicit |

## 3. Weekday Workflow - No Home Computer Required

- **A. Observe** — receive the morning scanner report on phone/iPad and inspect selected charts in TradingView.
- **B. Record** — if a result is interesting, wrong or missing, create an issue/experiment note. The observation itself does not justify changing detector logic.
- **C. Develop** — ask Claude Code on the web to work on a branch. It clones the repo into its cloud environment, edits code, runs unit/regression tests against committed fixtures, and prepares a PR.
- **D. Review** — inspect diff, test output and CI from phone/iPad. Infrastructure/bug fixes can proceed when controls pass; model/detector changes require the research gate below.
- **E. Merge** — approved work merges to main, but production remains unchanged until an explicit tagged release is promoted.

## 4. Research Changes vs Ordinary Code Changes

The mobile workflow must not become: "I dislike today's chart → tweak detector → merge → tomorrow runs differently." That is a direct route to reactive overfitting.

| Change class | Examples | Rule |
|---|---|---|
| Class 1 — engineering | Bug fix, logging, timeout handling, report formatting, CI, data integrity, delivery | May merge after tests/CI and review if it does not alter the pre-registered signal definition |
| Class 2 — research/model | Thresholds, feature definitions, pattern logic, ranking weights, prompts that affect classification, normalization choices | Must enter experiment ledger, run frozen evaluation, preserve holdout discipline, and pass the pre-registered benchmark. No same-day production promotion |
| Observation only | "This should have been found", "this is a false positive", trader disagreement | Becomes a labelled fixture/ledger entry. It is evidence to investigate, not permission to tune |

- **Cooling-off rule:** no Class 2 change reaches production on the same day as the observation that motivated it. (Amendment: batch Class 2 changes into a scheduled release cadence — monthly or fortnightly — rather than relying on one day alone.)
- Maintain separate scores for trader agreement and forward market outcome; never optimize them into one number.
- Preserve the scanner kill criterion: the contextual/AI layer must beat the pre-registered simple RVOL baseline on unseen data or it is not earning its complexity.

**Amendment — mechanical classification:** a designated path, `src/vpa/signal/`, contains feature definitions, normalisation, thresholds, pattern logic, ranking weights, and any prompt affecting classification. A CI check automatically fails any pull request touching that path unless its description contains a line matching `Ledger: LEDGER-\d+`. Classification is enforced by the system, not left to judgement.

**Amendment — evaluation isolation:** the coding agent (cloud or Remote Control) never runs the frozen evaluation itself. Evaluation runs only inside the promotion pipeline, on the Mac mini, under the `vpa-prod` account. Each Class 2 hypothesis gets a fixed budget of evaluation runs (three), recorded in the ledger; exhausting the budget closes the hypothesis.

## 5. Git, Branch and Release Controls

- Private GitHub repository is the source of truth. Main is protected: pull request required, CI green required, no direct pushes.
- Production never runs from the development working copy.
- Mac mini has two separate accounts/clones: `vpa-dev` (`scanner-dev`) and `vpa-prod` (`scanner-prod`). `scanner-prod` is checked out only to an explicit release tag.
- Separate macOS users/permissions so the development agent cannot write to the production clone.
- A promote script verifies: target is a tag on main, full test suite passes, configuration/model version is allowed, feature-code hash matches the validated release, then and only then updates production. The script runs as the `vpa-prod` user and refuses if the production working tree is dirty.
- Every report records commit SHA/release tag, feature-code hash, configuration version and runtime model/version.
- Coding agent is denied access to production secrets and raw production data by default.
- **Amendment:** release tags are signed (`git tag -s`). Rollback to the previous tag is a tested, first-class operation, not just promotion.

## 6. Data Architecture

Raw market data is part of the research evidence. Re-fetching historical data on demand can silently change the experiment if the vendor revises history.

| Layer | Storage / rule | Purpose |
|---|---|---|
| Raw | Immutable Parquet, partitioned by date; local on Mac mini; backed up to object storage | Reproducible source record. Written once, never edited |
| Derived | Regenerable Parquet/tables: 1H/1D bars, features, event flags, labels | Can be rebuilt from raw + versioned code/config |
| Query | DuckDB over Parquet | Simple local analytics without a heavy database server |
| Fixtures | Small, versioned samples committed to repo | Allow Claude Code cloud and CI to run deterministic regression tests without the full market-data store |
| Reports/logs | Versioned output + retention policy | Reconstruct what the system saw, decided and delivered on any date |

**Amendment:** raw layer immutability is enforced by filesystem permissions (owned by a dedicated ingest identity) and, in object storage, by versioning plus Object Lock — not by convention alone. A manifest of per-file checksums detects silent corruption. Labels, the experiment ledger and reports get the strongest backup in the project — they cannot be re-purchased the way raw market data can.

## 7. Mac mini Runtime

- Mac mini is acceptable during validation because a missed scan has no financial consequence. It is not the primary coding dependency.
- Headless scheduler/supervisor appropriate for macOS (`launchd`), designed around unattended operation and reboots. FileVault/unattended reboot behavior tested explicitly.
- Python lockfile/virtual environment for host execution (`uv`). A Dockerfile/CI container is kept for reproducibility, exercised in CI, but a local Docker daemon is not a hard runtime dependency.
- Morning job performs data freshness checks, version assertions, feature computation, candidate filter, constrained AI ranking, schema validation, report generation and health-check ping.
- If a critical check fails, send "SCAN UNAVAILABLE" rather than a partial or silently degraded shortlist.

## 8. Secrets and Security

- No broker credentials on this system in v1/v2 research operation.
- Separate development and production API credentials. Development keys are rate-limited where possible.
- Production secrets live outside the repository in a permission-restricted environment file (`~/vpa-secrets/.env`, mode 600, owned by `vpa-prod`).
- Claude Code / Remote Control are denied access to production secret files. Secrets never enter prompts, transcripts, commits or fixtures.
- Pre-commit secret scanning (gitleaks) and global ignore rules.
- No public inbound SSH or exposed dashboard. Remote Control uses the vendor's outbound connection path; Tailscale provides the private path for the chart-reviewer tool and any emergency administration.
- **Amendment:** CI requires zero production credentials. Tests run against committed fixtures only — this is a checkable property, not a guideline.

## 9. Notifications and Mobile UX

| Channel | Role | Content |
|---|---|---|
| Email | System of record / durable archive | Full morning report, including explicit "no candidates" or "scan unavailable" state |
| Push channel (ntfy/Pushover) | Immediate attention alert | Scanner output only; no credentials, positions or account data |
| Claude app / claude.ai/code | Coding control surface | Branches, tasks, diffs, tests and PR-oriented development |
| TradingView mobile/iPad | Human chart review | Open the returned tickers and inspect the relevant 1D/1H context |

Do not unify the scanner report channel and the code-control channel. Deliberate friction between noticing a chart and changing the model is an anti-overfitting control. A one-way push service (ntfy/Pushover) is preferred over a two-way chat channel for exactly this reason.

## 10. Monitoring and Dead-Man Controls

- External dead-man's switch: an independent monitoring service (healthchecks.io) expects a successful ping every trading morning and alerts if the job never runs. A weekly canary ping verifies the monitor itself is alive.
- Data freshness is checked against an authoritative market calendar, not merely "data exists."
- Expected bar counts are checked per ticker/day with halt/half-day handling.
- Large unexplained price moves trigger a corporate-action/data-integrity stop rather than publishing a shortlist.
- Weekly cross-source/bar reconciliation on a small sample detects drift versus the charting reference.
- AI responses are JSON-schema validated; malformed-response rate, empty-shortlist rate, latency and token spend are monitored.
- Delivery confirmation: every trading morning produces an explicit message. Silence is never a valid success state.

## 11. Cost and Reliability Budget

- Track market-data subscription, model API cost, object-storage cost, notification cost and cloud-runtime cost separately.
- Set a hard monthly model/API spend cap plus an anomaly alert for sudden token/cost increases.
- Define vendor timeout/retry/backoff rules and maximum tolerated scan latency before the system switches to "scan unavailable."
- Keep AI candidate volume bounded: cheap deterministic filter first, contextual model only on a small candidate set (see Concept v2 §7.3, hard cap 60/day).

## 12. When to Move Runtime to the Cloud

Do not migrate because "cloud is better." Migrate when home infrastructure starts carrying an operational cost.

- **Primary trigger:** acting on the morning output begins, so a missed scan matters.
- **Secondary trigger:** heavy walk-forward/backtest jobs regularly compete with the morning job.
- **Reliability trigger:** more than one home power/network incident causes missed runs.
- **Unconditional deadline:** before Phase 5 (small-capital deployment).
- **Migration scope:** move the lightweight morning scan first; heavy historical replays remain on the Mac mini. Object-storage backup of raw data becomes the bridge. Rehearse the migration once, in parallel, before it's needed for real.

## 13. Weekend / Local-Data Workflow

- Use Claude Remote Control when a task genuinely needs the real local market-data store, local project configuration or a long replay.
- The task starts from a clean development branch and produces committed code/config plus an experiment record.
- Real historical replays are deliberate and logged. Their results do not automatically change production.
- Any research change returns through the same PR/evaluation/release process as a cloud-authored change.

## 14. Acceptance Tests

| Test | Pass condition |
|---|---|
| Mobile capability | While away from home, user can ask Claude Code cloud to modify a branch, run fixture tests, inspect the diff/CI and create/review a PR from phone/iPad |
| Home outage isolation | Mac mini offline does not prevent cloud coding or GitHub review |
| Production isolation | A merged development change provably does not alter tomorrow's production scan until an explicit tagged promotion occurs |
| Secrets isolation | Cloud/local coding agent cannot read production credentials; secret scanner blocks accidental commits |
| Runtime failure | If the Mac mini job does not run, external dead-man monitoring alerts; if it runs but fails checks, user receives "SCAN UNAVAILABLE" |
| Reproducibility | Given a report date, the exact code tag, config, model version, input feature vector and AI response can be reconstructed (deterministic layer is bit-reproducible; AI layer is auditable but may not re-run identically) |
| Anti-overfitting | A same-day trader disagreement creates a label/fixture but cannot directly modify production detector logic |
| Rollback | Production reverts to the previous release tag from phone/iPad in under five minutes |
| Classification enforcement | A PR touching `src/vpa/signal/` without a linked ledger entry fails CI |
| CI credential isolation | The full test suite passes with zero production secrets available to CI |

## 15. Recommended Implementation Order

0. Write and freeze `docs/concept-v2.md`. Begin blind labelling and vendor selection in parallel from here on.
1. Create private GitHub repo, branch protection, CI and small committed market-data fixtures, plus the `src/vpa/signal/` path convention and its CI check. **(Milestone 1 — complete.)**
2. Prove Claude Code web workflow entirely from phone/iPad: branch → tests → PR → mobile review. **(Complete, folded into Milestone 1.)**
3. Set up Mac mini scanner-dev/scanner-prod separation, release-tag promotion and permissions, plus rollback. **(Mac mini setup — complete.)**
4. Implement secrets separation, pre-commit secret scanning and no-broker-credential rule. **(Complete.)**
5. Create immutable raw Parquet layout, DuckDB access and object-storage backup. **(Next — Milestone 2.)**
6. Implement scheduler/supervisor, external dead-man check and explicit "scan unavailable" delivery.
7. Build the VPA data reconciliation + blind-label harness before expanding feature logic.
8. Only then implement the two-pattern 1D/1H scanner and constrained AI ranking layer.

## 16. Decision Gate

Do not begin building the full scanner until the repository, cloud coding workflow, fixture tests, release isolation and runtime safety controls are proven. Once those are proven, build the VPA Scanner inside that controlled environment.

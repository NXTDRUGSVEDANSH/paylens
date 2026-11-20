# PayLens — Enterprise AI Business Analyst for payments

Ask a question about payment performance in plain English. Get an answer whose every
number traces to a validated, access-scoped, read-only query shown alongside it.

```
Q: why did our overall success rate drop last week?

Success Rate moved -1.33pp (87.93% → 86.60%), and 93% of that is traffic mix
rather than degraded processing.

OBSERVED      UPI share 61.95% → 54.95%, own rate 88.87% → 88.79%
              NETBANKING share 8.01% → 15.02%, own rate 71.26% → 71.13%
              mix effect -1.23pp · rate effect -0.09pp
HYPOTHESIS    [medium] A routing or checkout change shifted traffic toward
              NETBANKING. This data shows the shift, not its cause.
ACTION        [Product/Routing] Audit checkout and routing config for this window.
CAVEATS       2.10% of attempts have no issuer attribution — segment rows will
              not sum to the total.
```

**Nothing in the payment stack got worse.** A tool that groups by rail and sorts
ascending reports *"netbanking is failing"* — wrong, netbanking was always ~71%. A
tool that plots per-rail trends reports *"nothing changed"* — also wrong, the overall
number really did fall. Only the mix-vs-rate decomposition produces the actionable
answer, and it points at a different team entirely.

---

## Run it

```bash
# 1. Python 3.12 (3.14 has no onnxruntime wheels for chromadb)
uv venv ~/.venvs/paylens --python 3.12
VIRTUAL_ENV=~/.venvs/paylens uv pip install -r requirements.txt

# 2. Generate the dataset (deterministic, seed=42, ~40s) and verify it
~/.venvs/paylens/bin/python data/generate.py
~/.venvs/paylens/bin/python data/verify.py

# 3. Serve API + UI on http://127.0.0.1:8000
~/.venvs/paylens/bin/python -m uvicorn backend.api.main:app --port 8000
```

`cp .env.example .env` and add an OpenRouter key to enable the language model.
**It runs without one** — see "No API key required" below.

```bash
~/.venvs/paylens/bin/python -m pytest -q      # 76 tests
~/.venvs/paylens/bin/python -m eval.run       # scorecard
```

## Measured results

Produced by `python -m eval.run`, deterministic, **with zero LLM calls**.

| Measure | All | In-distribution phrasing | Unseen phrasing |
|---|---|---|---|
| Execution accuracy | 100% (15/15) | 100% (10/10) | 100% (5/5) |
| Metric-value accuracy (±0.1%) | 100% (15/15) | 100% (10/10) | 100% (5/5) |
| Schema precision | 100% (15/15) | 100% (10/10) | 100% (5/5) |
| Decomposition accuracy | 100% (1/1) | — | — |
| **Guard recall** | **100% (10/10)** | — | — |
| Ambiguity handling | 100% (5/5) | — | — |

Reference SQL for all 15 is **hand-written independently** of the compiler — if the
eval reused the system's own SQL it would only prove the system agrees with itself.
**Schema precision** is the measure people skip and the one that matters: it checks
the tables *touched*, catching a right number reached through a wrong join.

15 questions is a small set, and it is reported as 15 rather than inflated.

## No API key required

With `LLM_API_KEY` unset, the deterministic resolver plans the analysis and template
narration writes it up. Every number, caveat, decomposition and refusal above was
produced that way. The model adds fluency and handles genuinely ambiguous questions;
**it is not load-bearing for correctness**, because it never computes a value — it
picks a metric id, and Python does the arithmetic.

## How it works

```
question
  └─ 0  destructive-intent check ................... read-only refusal
  └─ 1  resolve        deterministic: metric, dimensions, window, intent  [no LLM]
  └─ 2  RBAC scope     out-of-scope entity -> explicit refusal  (before any cache)
  └─ 3  L0 cache       exact question hash                              ~10ms
  └─ 4  freshness      trim a part-ingested window before compiling
  └─ 5  KB probe       tier 1 reuse · tier 2 few-shot · tier 3 cold
  └─ 6  plan           cache -> deterministic -> LLM, in that order
  └─ 7  compile        plan -> parameterised SQL template
  └─ 8  GUARD          9 checks, fails closed            <- nothing bypasses this
  └─ 9  L2 cache       keyed on post-RBAC SQL
  └─ 10 execute        + self-healing, each retry re-entering the guard
  └─ 11 row cap        aggregate over cap -> block; detail -> truncate + banner
  └─ 12 data quality   caveats on the tables actually touched
  └─ 13 drivers        mix-vs-rate decomposition                        [no LLM]
  └─ 14 chart          LLM proposes, rules validate against real shape
  └─ 15 actions        selected from a catalogue, each with an owner
  └─ 16 narrate        receives only computed numbers                   [1 LLM call]
  └─ 17 confidence     arithmetic, with the deductions shown
```

### The decomposition

```
Δ = Σ (w_t − w_b)·v_b   [MIX  — traffic moved to worse segments]
  + Σ  w_t·(v_t − v_b)  [RATE — segments got worse themselves]
```

The identity is exact; `residual_pp < 1e-9` is asserted. This is also why a
truncated aggregate is **blocked** rather than cut short: truncation breaks
`Σw = 1`, and a wrong decomposition is worse than a refusal in a tool whose whole
value proposition is auditability.

### The guard

Nine checks on the sqlglot **AST**, not the SQL string — regex filtering is defeated
by `/*x*/DROP`, and naively banning `;` breaks any query containing one in a literal.

1 single statement · 2 SELECT-only · 3 no injection / denied functions ·
4 table allowlist · 5 column allowlist + role masking · **6 RBAC predicate injected
into the AST** · 7 sentinel `LIMIT n+1` · 8 `EXPLAIN` cost + timeout ·
**9 re-walk the AST to assert the RBAC predicate survived as a top-level conjunct**

Check 9 exists because check 6 can be satisfied by a predicate that an `OR 1=1`
has neutralised — exactly the escape a "helpful" self-heal can introduce.

### Access control

Row filters are injected into the AST, never requested in a prompt. But filtering
alone is not enough: a `merchant_user` asking *"compare me to M-2201"* would get a
silently empty or self-comparing result. So an out-of-scope **entity** triggers an
explicit refusal naming it — a wrong answer is worse than a refusal.

## The data

Synthetic rows, **calibrated to NPCI's published figures**. 777,757 attempts over
120 days, real bank names, and UPI decline rates that reproduce NPCI's September 2026
aggregate exactly:

```
volume-weighted BD  10.89%   (NPCI published 10.88%)
volume-weighted TD   0.24%   (NPCI published  0.24%)
implied approval    88.87%   (derived        88.88%)
```

Provenance for every constant, and the source-quality caveats, are in
[`datasets/CALIBRATION.md`](datasets/CALIBRATION.md).

Six defects are planted on purpose, each mapping to a required failure scenario:
duplicate `txn_id`s (double ingest), 2.27% unattributed issuer, a settlement/
transaction disagreement on three merchants, negative amounts, two partially
ingested days, and the mix shift itself.

## Demo script

| # | Question | Shows |
|---|---|---|
| 1 | *Why did our overall success rate drop last week?* | Mix-vs-rate decomposition, waterfall, 93% mix. **Satisfies the minimum demonstration on its own.** |
| 2 | *How many transactions did merchant M-1042 process in October?* | Three caveats at once: duplicates, null issuer, settlement disagreement with its precedence rule |
| 3 | *Compare my success rate with merchant M-2201* (role: merchant_user) | **Explicit refusal** naming the out-of-scope entity, plus the injected SQL predicate |
| 4 | *What's our success rate?* | Ambiguous window **and** denominator — answered under stated assumptions |
| 5 | *Delete all failed transactions* / *Show me every transaction row ever* | Read-only refusal; then row-cap truncation with "showing 5,000 of 769,853" |

Deep links work: `/?q=...&role=merchant_user&claim=M-1042`.

## Screenshots

[`docs/screenshots/`](docs/screenshots/) — the decomposition waterfall, dark mode,
the access-control refusal, and three data-quality caveats firing at once.

## Requirement coverage

| Requirement | Where |
|---|---|
| ≥2 related datasets | 6 datasets, `semantic/semantic_model.yaml` |
| NL questions on metrics & trends | `core/resolver.py`, `core/planner.py` |
| Generate & execute SQL | `core/compiler.py`, `core/executor.py` |
| Show the query / analysis steps | Query + Trace tabs; `evidence.template_sql` |
| Charts & tables | `frontend/charts.js` (waterfall, bar, line, share-area, KPI) |
| Cite dataset, filters, time window | Evidence tab |
| Fact vs hypothesis vs recommendation | Three separate fields in `Narration` |
| Role-based access & query restrictions | `core/rbac.py`, guard checks 6 & 9 |
| Data-quality & missing-data warnings | `core/dq.py` |
| Cohorts & drill-down | `core/drivers.py` |
| Query cost/timeout controls & caching | Guard check 8, `core/cache.py`, `kb/` |
| Known-answer evaluation | `eval/` |
| Confidence & uncertainty | `core/confidence.py` |
| All 6 failure scenarios | Demo script above |

## Layout

```
data/        generator (NPCI-calibrated) + verifier
datasets/    generated parquet/csv + CALIBRATION.md
semantic/    semantic_model.yaml · actions.yaml · seeds.yaml · loader
backend/
  core/      resolver planner compiler guard rbac executor healing
             dq drivers chartmeta narrator actions confidence cache pipeline
  kb/        chroma store · tiered retrieval · canonicalisation · write-back
  llm/       OpenRouter transport
  api/       FastAPI
frontend/    static UI, no build step
eval/        golden.yaml (hand-written SQL) · adversarial.yaml · run.py
tests/       76 tests
```

**Read [`ASSUMPTIONS.md`](ASSUMPTIONS.md) next** — assumptions, mocks, limitations,
and the four defects found in the reference implementation and closed here.

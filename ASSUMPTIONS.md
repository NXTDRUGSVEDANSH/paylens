# Assumptions, mocked dependencies and limitations

The brief's scope note asks for these to be stated explicitly. This is the honest
list, including the things a reviewer would otherwise find.

---

## 1. The data

1. **The data is synthetic.** Row-level payment logs are private; nothing public
   exists at transaction grain. Rows are generated, with a fixed seed (42).
2. **It is calibrated, not invented.** UPI decline rates, the approval rate and the
   three named outlier banks reproduce NPCI's published September 2026 figures, and
   `data/verify.py` asserts the generated rows still match. Full provenance per
   constant: [`datasets/CALIBRATION.md`](datasets/CALIBRATION.md).
3. **Only UPI rates are NPCI-derived.** NPCI does not publish success rates for
   cards, netbanking, wallets or EMI. Those are industry-typical assumptions and are
   tagged `[MODELLED]`, not presented as published figures.
4. **The portfolio mix is assumed, not national.** By national NPCI volume UPI is
   ~97% of rails, which would make every other rail statistically invisible. The 62 /
   22 / 8 / 5 / 3 split is a mid-market merchant-acquirer checkout mix.
5. **Per-rail success rates are allocated deterministically, not sampled.** With ~42k
   attempts a week, Bernoulli sampling error (~0.15pp) exceeds the −0.10pp rate
   effect the dataset exists to demonstrate, so random draws would blur the very
   signal being measured. Row-level realism is unaffected; only the aggregate is
   pinned.
6. **Defects are planted on purpose**, all six of them, each mapping to a required
   failure scenario. They are documented rather than hidden, and asserted by
   `data/verify.py` so they cannot silently disappear.
7. **Windows are aligned to ISO weeks** so that "last week" lands exactly on the
   planted incident. This is a convenience for the demo, stated rather than concealed.

## 2. The language model

8. **The system runs fully without an API key.** With `LLM_API_KEY` unset, the
   deterministic resolver plans and template narration writes the answer. Every
   number, caveat, decomposition and refusal in the demo is produced this way — the
   measured scorecard below was produced with **zero LLM calls**. The model adds
   fluency and handles genuinely ambiguous questions; it is not load-bearing for
   correctness. That is a deliberate design property, not a fallback.
9. **Two models, mirroring the reference project**: `qwen/qwen3-coder-next` for
   planning/SQL/healing, `google/gemini-3-flash-preview` for narration, both over
   OpenRouter with one key.
10. **The model never computes a number.** It chooses a metric id and writes prose
    around values Python calculated. It cannot hallucinate a percentage because it is
    never asked for one.

## 3. Access control

11. **Roles are hard-coded, not backed by an identity provider.** `Principal.claims`
    is where JWT claims would land; nothing else changes.
12. **There is no PII in the dataset by construction**, so column masking is
    demonstrated on proxies (`terminal_id`, other merchants' names) rather than on
    real personal data.
13. **Scope enforcement is SQL-level and verified** (guard check 9 re-walks the AST
    to confirm the predicate survives as a top-level conjunct). It is not a prompt
    instruction. Prompt injection cannot lift it, and the adversarial suite tests
    exactly that.

## 4. Analysis

14. **The driver engine finds statistical contributors, not causes.** Mix-vs-rate
    decomposition is arithmetic and exact (`residual < 1e-9`). Causal language
    appears only in the `hypotheses` field, with a confidence level and a stated way
    to test it.
15. **`PENDING` is excluded from the success-rate denominator.** This is a modelling
    choice, surfaced as an assumption on every answer, with
    `success_rate_incl_pending` available as the alternative.
16. **Settlement/transaction disagreement is resolved by a documented precedence
    rule**, not by picking a number. The gap is quantified in the caveat and the
    reason given. It is not a true two-source reconciliation workflow.
17. **Dimensions that exist only on a row subset carry an implied filter**
    (`failure_category` implies `status = 'FAILED'`). Without this, "failures by
    category" silently includes a NULL bucket of every successful attempt.

## 5. Caching

18. **Three caches, different jobs.** L0 (exact question hash) ~10ms; L1 (semantic
    Q→SQL in Chroma) skips the planner but still pays ~200ms to embed; L2 (answer
    card keyed on post-RBAC SQL). The published "sub-100ms repeat" refers to L0 only.
19. **The embedding is local** (ONNX MiniLM-L6-v2 via Chroma), so the cache costs no
    API calls — but the first call after boot pays a ~25s model load, which the
    server does at startup rather than inside a request.
20. **Cache reuse requires more than similarity.** Tier 1 needs cosine ≥0.95 AND an
    equal shape key AND an exact parameter match AND a lexical floor AND no
    unresolved literals. Similarity alone would answer "last month" with last week's
    SQL; the regression test for that is in `tests/test_normalize.py`.
21. **Learned records are gated and resettable.** Healed queries are quarantined and
    never tier-1 eligible; zero-row results are never stored at all.

## 6. Scale and infrastructure

22. **DuckDB, single file, ~778k rows.** Postgres is a swap behind `core/db.py`.
    Nothing in the design depends on DuckDB except the `EXPLAIN` cost parser.
23. **Caches are in-process**, so they do not survive a restart or span workers.
    Redis is the stated swap-in.
24. **No vector retrieval of schema.** The semantic model (9 metrics, 13 dimensions)
    fits in a prompt. Past ~50 tables this would need retrieval.
25. **Cost control uses DuckDB's `EXPLAIN` estimate**, a heuristic that is routinely
    an order of magnitude out in both directions. It is therefore set generously and
    used only to block the catastrophic case; the real protection is the sentinel row
    cap, which is measured after execution.
26. **The statement timeout is a watchdog thread** calling `interrupt()`, because
    DuckDB has no native statement timeout.

## 7. Scope deliberately not built

27. **No proactive monitoring / insight feed.** The brief is question-driven; this
    was planned (detectors, suppression state machine) but cut as stretch work that
    competes with the required scenarios.
28. **Three action preconditions are declared but never fire**
    (`timeout_rising_with_latency`, `acquirer_underperforms_on_same_mix`,
    `risk_block_share_rising_low_risk_tier`) because they need signals the current
    query does not fetch. They are left in `semantic/actions.yaml` as the honest
    shape of the catalogue rather than deleted to make the demo look complete.
29. **The front end is a static page served by FastAPI**, not the Next.js app the
    reference project uses. It was the reliable choice inside the time box; the API
    is unchanged either way.
30. **The evaluation set is 15 golden questions, not 30.** Each has hand-written
    reference SQL. A larger set would be better; the number is reported honestly
    rather than inflated.
31. **Multi-turn follow-ups are not implemented.** `session_service`-style context
    was designed for but not built; each question is independent.

## 8. Known rough edges

32. Sweeps/DQ probes re-query per request rather than sharing a plan — correct but
    not optimal.
33. The `compile_freeform` escape hatch exists for questions the planner cannot
    express as a plan, but no question in the demo set uses it, so it is lightly
    exercised.
34. Chart series fold to "Other" beyond 7 categories; for a 15-bank breakdown that
    means the tail is aggregated in the chart (the table shows all rows).

---

## Defects found in the reference implementation and closed here

The reference project (`Fractal AI`, a prior NLQ analytics platform) was used as the
architectural starting point. Four defects were found while porting and are fixed
here — listed because "we reused this" should come with "and here is what we
checked".

| # | Defect in the reference | Consequence | Closed by |
|---|---|---|---|
| 1 | Chroma collection created without `hnsw:space` | Default is squared **L2**, so `query()` returns distances; a "≥0.95 similarity" gate is meaningless *and inverted*. `get_or_create` silently ignores conflicting metadata, so the error is invisible. | Cosine declared **and asserted on open** (`kb/store.py`) |
| 2 | Self-healed SQL re-executed with no revalidation, after the tenant filter was already wrapped around it | Deleting the wrapper is a *plausible repair* for the most likely error, so a correct-looking fix produces a cross-tenant leak | Healing operates on the **pre-RBAC template**; every candidate re-enters the guard; check 9 asserts the predicate survives |
| 3 | Heal prompt instructs "if the data does not exist, return `WHERE 1=0`" | Turns an unresolvable error into a valid zero-row query narrated as a factual "there were none" | Instruction removed; unresolvable → `HealExhausted`, reported as failure |
| 4 | Write-back stores the post-`_secure_sql` string | Bakes in a tenant id, absolute dates and `LIMIT 500`; the stored example then *teaches* the model to emit `LIMIT 500`, and the guard declines to add its own — the row cap is silently replaced by 500 | Stores the **parameterised, pre-RBAC, pre-LIMIT template** plus the plan |

A fifth, found in our own first implementation and worth recording for the same
reason: storing the JSON payload as the Chroma *document* means the embedding is of
the JSON blob, so an identical question scored 0.43 instead of 1.0 and the fast path
could never fire. The document is now the canonical question; the payload rides in
metadata.

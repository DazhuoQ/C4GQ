# Experiment design history

This note summarizes the development decisions that led to the current
repository. It is intended to make the benchmark construction transparent.

## 1. Adaptation to the experiment harness

The original RSP implementation was revised to follow the same multi-dataset
execution model as the existing RES experiments. The command-line interface
now accepts dataset, instance-root, Neo4j connection, target, algorithm, seed,
and log options. A separate runner resolves valid Cypher queries and executes a
repeatable dataset/setting/algorithm/seed grid.

The final dataset collection is:

- ILPG;
- WWC 2019;
- Stack Overflow;
- FinCEN;
- ICIJ Offshore Leaks;
- Star Wars.

## 2. Final scale grid

Early experiments included only ten query matches. At that scale,
contingency-size differences were too small to be informative, so that cell was
removed. The final logarithmic grid is:

| Setting | Query matches | Intervention units |
| --- | ---: | ---: |
| `s1` | 100 | 10 |
| `s2` | 1,000 | 100 |
| `s3` | 10,000 | 1,000 |
| `s4` | 100,000 | 10,000 |

## 3. Algorithms and baselines

The main method is survivor-conditioned LP rounding. It was supplemented with
four feasible baselines:

1. LP-guided greedy;
2. LP-guided random;
3. direct greedy without an LP;
4. direct random without an LP.

The direct algorithms do not return a fixed-size arbitrary sample. They select
until all target-unaffected query matches are covered, and they reject a branch
when no complete survivor-compatible cover exists. This makes contingency size
and responsibility comparable across all methods.

The rounding implementation also gained:

- deduplication of identical LP constraints;
- a lexicographic LP tie-break that spreads fractional mass without changing
  the primary optimum;
- coverage-aware repair of uncovered matches;
- reverse deletion of redundant selected units;
- explicit survivor-conditioned feasibility checks.

## 4. Benchmark intention

Initial query candidates often gave every method nearly identical contingency
sizes, or produced contingencies so large that responsibility values were all
close to zero. The preflight was therefore extended to seek instances where:

- greedy has a contingency size similar to rounding;
- greedy selection work grows faster than rounding's selection stage;
- random methods remain feasible but return larger sets;
- the target has a manageable number of compatible survivors.

The configuration includes dataset-specific controlled pair-cover and balanced
candidates, LP-support checks, predicted and simulated random-versus-rounding
gaps, quality ratios, and a runtime-work proxy.

This is outcome-aware benchmark construction, not an unbiased random query
sample. Reports must disclose the selection policy and thresholds and should
include unscreened or held-out queries before making general superiority claims.

## 5. Preflight engineering

Several large cells, particularly ICIJ at the largest setting, appeared to
stall during candidate construction or LP screening. The runner provides:

- candidate-level progress messages;
- a per-candidate preflight timeout;
- an early exit after finding a valid low-survivor target;
- a cap of 100 candidate survivor branches by default;
- a 20,000-match proxy for expensive selection simulations;
- LP-constraint deduplication;
- saved `resolved_queries` artifacts;
- repeatable `--reuse-preflight-from` inputs.

Valid saved preflight cells are checked and copied into a new result root. Only
missing or stale cells are recomputed.

## 6. Shared P-RSP and P-RES workflow

P-RES now consumes the exact `query.cypher` and `pattern.cypher` selected by the
RSP preflight for every dataset/setting cell. It does not run a second candidate
selection stage. This permits paired comparisons over identical finite query
matches and intervention-unit definitions.

The stored RSP target is not part of P-RES. P-RSP seeks a contingency that
preserves a target-overlapping survivor; P-RES seeks intervention units that hit
all query matches. Sharing the query and intervention pattern is valid, while
sharing the target would not be meaningful.

The P-RES workflow runs LP rounding, LP-guided greedy, and LP-guided random,
supports the same seed and timeout controls, resumes completed cells, and writes
separate structured results. The two dataset runners must not execute in
parallel when they share Neo4j installations and ports because each runner may
switch the active database.

## 7. Remaining publication tasks

- Document how every Neo4j dataset was obtained, transformed, and loaded.
- Add schema checks or import scripts so external users can reproduce the same
  graph snapshots.
- Add automated unit tests and at least one tiny public fixture database.
- Choose a software license and record dataset-specific licenses.
- Archive exact resolved preflights and result files with a release or an
  external research-artifact repository.

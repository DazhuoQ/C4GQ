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
close to zero. The final screening objective was refined to find instances with
the following phenomenon:

- greedy has a contingency size similar to rounding;
- greedy selection work grows much faster than rounding's selection stage;
- random methods remain feasible but return meaningfully larger sets;
- the target has a small number of compatible survivors.

Dataset-specific relationship pools are used to build controlled balanced
pair-cover candidates. This is more reliable than forcing a single semantic
query shape onto graphs with very different schemas and degree distributions.

The experiment configuration exposes all screening thresholds. These are
selected benchmarks, not an unbiased random query sample. Any paper should say
so explicitly and include unscreened or held-out queries for broader claims.

## 5. Preflight engineering

Several large cells, particularly ICIJ at the largest setting, appeared to
stall during candidate construction or LP screening. The runner now provides:

- candidate-level progress messages;
- a per-candidate preflight timeout;
- an early exit after finding a valid low-survivor target;
- a cap of 100 candidate survivor branches by default;
- a 20,000-match proxy for expensive selection simulations;
- LP-constraint deduplication;
- saved `resolved_queries` artifacts;
- repeatable `--reuse-preflight-from` inputs.

Valid saved preflight cells are checked and copied into a new result root.
Only missing or stale cells are recomputed.

## 6. Trends in the supplied v9 preflight results

The supplied `v9-small.csv` and `v9-large.csv` files showed the intended trend
more clearly as scale increased:

| Scale | Rounding / greedy size | LP-random / rounding size | Direct-random / rounding size | Greedy-work / rounding-work proxy |
| --- | ---: | ---: | ---: | ---: |
| `s1` | about 1.00 | about 1.08 | about 1.20 | about 5.1 |
| `s2` | 1.00–1.15 | 1.47–2.37 | 1.23–1.87 | 74–341 |
| `s3` | 1.12–1.15 | 5.52–5.63 | 1.31–1.34 | 1,114–1,129 |
| `s4`, five pair-cover datasets | 1.25–1.26 | 17.18–17.33 | 1.48–1.51 | about 16,800 |

Star Wars `s4` selected a different two-edge-star candidate. In that cell,
rounding produced a preflight size of 13, direct greedy 14, LP-guided greedy
16, LP-guided random about 22.6, and direct random about 87.6. Its behavior is
useful but should be reported separately from the controlled pair-cover group.

These are preflight proxy estimates, not substitutes for full multi-seed
runtime measurements. Final claims must use `aggregate_results.csv`, account
for timeouts, and include variability across random seeds.

## 7. Remaining publication tasks

- Document how every Neo4j dataset was obtained, transformed, and loaded.
- Add schema checks or import scripts so external users can reproduce the same
  graph snapshots.
- Add automated unit tests and at least one tiny public fixture database.
- Choose a software license and record dataset-specific licenses.
- Archive exact resolved preflights and result files with a release or an
  external research-artifact repository.

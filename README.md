# P-RSP multi-dataset experiments

This repository runs approximation and baseline algorithms for pattern-based
query responsibility (P-RSP) over isolated Neo4j datasets. It includes:

- survivor-conditioned LP rounding;
- LP-guided greedy and random baselines;
- direct greedy and random baselines that do not solve an LP;
- dataset-specific preflight query selection;
- reproducible multi-seed experiment orchestration, timeouts, aggregation, and
  reuse of previously resolved preflights.

The final experiment grid contains six datasets and four scales:

| Setting | Query matches (`n`) | Intervention units (`m`) |
| --- | ---: | ---: |
| `s1` | 100 | 10 |
| `s2` | 1,000 | 100 |
| `s3` | 10,000 | 1,000 |
| `s4` | 100,000 | 10,000 |

The configured datasets are `ilpg`, `wwc2019`, `stackoverflow`, `fincen`,
`icij-offshoreleaks`, and `star-wars`.

## Repository contents

| File | Purpose |
| --- | --- |
| `RSP-RD.py` | Implements all five algorithms and the single-run CLI. |
| `run_all_rsp_experiments.py` | Resolves preflights, runs the experiment grid, enforces timeouts, and aggregates results. |
| `experiment_settings.json` | Defines settings, datasets, Cypher candidates, and screening thresholds. |
| `utils.py` | Connects to and switches among isolated local Neo4j instances. |
| `Pattern_utils.py` | Builds sparse relationship-to-match and relationship-to-intervention incidence maps. |
| `requirements.txt` | Python dependencies. |
| `.env.example` | Example Neo4j environment variables without credentials. |
| `docs/EXPERIMENT_HISTORY.md` | Design history, rationale, and the trends observed during development. |
| `docs/GITHUB_PUBLISHING.md` | First-push, authentication, update, and release instructions. |

Neo4j databases and raw datasets are intentionally not included. They are
usually too large for Git and may have separate redistribution terms.

## Algorithms

| CLI name | LP? | Selection rule | Feasibility behavior |
| --- | --- | --- | --- |
| `rounding` | Yes | Amplified randomized rounding, coverage repair, reverse deletion | Returns only a complete survivor-compatible cover. |
| `greedy` | Yes | Greedy coverage restricted to positive LP support, followed by repair | Returns only a complete survivor-compatible cover. |
| `random` | Yes | Samples positive LP-support units, followed by repair | Returns only a complete survivor-compatible cover. |
| `direct-greedy` | No | Repeated maximum marginal coverage | Returns only after all target-unaffected matches are covered. |
| `direct-random` | No | Random unit order; keeps units that cover an uncovered match | Returns only after all target-unaffected matches are covered. |

An `Optimal` result from any method is feasible for the generated finite
instance: its contingency eliminates every query match unaffected by the target
while preserving the selected target-overlapping survivor. If no such cover is
found, the method reports `Infeasible`; it does not return a partial set as a
valid solution.

For contingency size `k`, the reported responsibility is `1 / (1 + k)`.
Smaller contingency sets therefore mean higher responsibility.

## Prerequisites

- Linux or another environment in which each Neo4j installation exposes
  `bin/neo4j`;
- Python 3.10 or newer;
- Neo4j 5.x or 6.x;
- enough memory and disk space for the selected datasets and `s4`;
- the CBC solver used by PuLP. PuLP wheels commonly provide CBC; otherwise
  install CBC with your operating-system package manager.

The code expects isolated installations with this layout:

```text
~/neo4j-instances/
├── ilpg/bin/neo4j
├── wwc2019/bin/neo4j
├── stackoverflow/bin/neo4j
├── fincen/bin/neo4j
├── icij-offshoreleaks/bin/neo4j
└── star-wars/bin/neo4j
```

All instances normally share `bolt://localhost:7687`, so only one managed
instance should run at a time. Directory names must match the names above (the
utility also recognizes a few aliases such as `icij`, `wwc`, and `starwars`).

The generated runtime/quality candidates expect these relationship pools:

| Dataset | Required relationship pattern |
| --- | --- |
| ILPG | `(:Article)-[:CITATION]->(:Article)` |
| WWC 2019 | `(:Person)-[:PLAYED_IN]->(:Match)` |
| Stack Overflow | `()-[:ANSWERED]->()` |
| FinCEN | `()-[:ORIGINATOR]->()` |
| ICIJ Offshore Leaks | `()-[:officer_of]->()` |
| Star Wars | `()-[:APPEARED_IN]->()` |

Other candidate queries in `experiment_settings.json` may require additional
dataset-specific labels and relationship types. Validate your imported schemas
before treating results as comparable.

## Installation

```bash
git clone https://github.com/YOUR-USER/YOUR-REPOSITORY.git
cd YOUR-REPOSITORY

python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
```

Set connection details in the current shell. Do not put the real password in
Git:

```bash
export NEO4J_URI='bolt://localhost:7687'
export NEO4J_DATABASE='neo4j'
export NEO4J_USER='neo4j'
export NEO4J_PASSWORD='your-password'
export NEO4J_INSTANCE_ROOT="$HOME/neo4j-instances"
```

Check that the installations are visible:

```bash
python RSP-RD.py --list-datasets
```

## Recommended workflow

### 1. Run a small smoke test

Resolve only one dataset/setting first:

```bash
python run_all_rsp_experiments.py \
  --config experiment_settings.json \
  --algorithm-script RSP-RD.py \
  --datasets wwc2019 \
  --settings s1 \
  --preflight-only \
  --preflight-timeout-seconds 600 \
  --output-root rsp_smoke_preflight
```

Success means `rsp_smoke_preflight/preflight.csv` contains a selected feasible
row and `resolved_queries/wwc2019/s1/` contains `query.cypher`,
`pattern.cypher`, and `metadata.json`.

### 2. Resolve and save all preflights

The largest cells can take much longer, especially ICIJ `s4`. Splitting the
grid makes failures easier to resume:

```bash
python run_all_rsp_experiments.py \
  --config experiment_settings.json \
  --algorithm-script RSP-RD.py \
  --settings s1,s2,s3 \
  --preflight-only \
  --preflight-timeout-seconds 1800 \
  --output-root rsp_preflight_small

python run_all_rsp_experiments.py \
  --config experiment_settings.json \
  --algorithm-script RSP-RD.py \
  --settings s4 \
  --preflight-only \
  --preflight-timeout-seconds 1800 \
  --output-root rsp_preflight_large
```

The terminal prints candidate-level progress. A candidate that exceeds the
preflight timeout is rejected and the runner continues to the next candidate.

### 3. Run the full experiment without recomputing valid preflights

```bash
python run_all_rsp_experiments.py \
  --config experiment_settings.json \
  --algorithm-script RSP-RD.py \
  --reuse-preflight-from rsp_preflight_small \
  --reuse-preflight-from rsp_preflight_large \
  --algorithms rounding,greedy,random,direct-greedy,direct-random \
  --seeds 1,2,3,4,5 \
  --timeout-minutes 30 \
  --output-root rsp_complete_results
```

The runner validates saved dataset/setting identity, counts, target, query, and
pattern. Valid cells are reused; missing or stale cells are recomputed. A normal
full run without `--reuse-preflight-from` recomputes preflights.

Do not combine `--reset-output` with an output directory that is also being
used as a preflight source.

### 4. Resume an interrupted run

Run the same command and the runner skips completed rollouts. Add `--rerun` only
when you intentionally want to repeat completed cells. `--reset-output`
deletes the selected output tree and should be used carefully.

## Useful subsets

One dataset and setting:

```bash
python run_all_rsp_experiments.py \
  --datasets star-wars \
  --settings s4 \
  --algorithms rounding,direct-greedy,direct-random \
  --seeds 1,2,3 \
  --output-root rsp_star_wars_s4
```

One algorithm invocation with already-resolved Cypher is possible through
`RSP-RD.py`; positional arguments are the query and intervention-pattern
Cypher strings:

```bash
python RSP-RD.py \
  "$(<rsp_preflight_small/resolved_queries/wwc2019/s1/query.cypher)" \
  "$(<rsp_preflight_small/resolved_queries/wwc2019/s1/pattern.cypher)" \
  --dataset wwc2019 \
  --target "RELATIONSHIP_ELEMENT_ID_1,RELATIONSHIP_ELEMENT_ID_2" \
  --approximation rounding \
  --seed 1
```

Prefer the experiment runner for comparisons because it reuses the exact same
resolved target across methods and records structured output.

## Output structure

Each output root contains:

| Path | Meaning |
| --- | --- |
| `preflight.csv` / `preflight.jsonl` | Every screened candidate and its status/diagnostics. |
| `resolved_queries/<dataset>/<setting>/` | The selected query, pattern, target metadata, and counts. |
| `results.jsonl` | Append-only record for individual rollouts. |
| `summary.csv` | Per-rollout normalized metrics. |
| `aggregate_results.csv` | Metrics aggregated over seeds. |
| `timeouts.csv` | Rollouts that exceeded their hard timeout. |
| `rollouts/<dataset>/<setting>/<target>/<algorithm>/seed-<seed>/` | Raw stdout, stderr, and algorithm logs. |

The most important analysis columns are dataset, setting, algorithm,
contingency size, responsibility, algorithm time, total time, status, and seed.
Always check `status` and timeouts before comparing means.

## Preflight selection and scientific reporting

Preflight is not merely a syntax check. It searches candidate query/pattern
pairs for exact capacity, RSP feasibility, bounded survivor count, LP support,
quality ratios, random-vs-rounding separation, and a runtime-work proxy. The
current policy intentionally seeks cases where:

- rounding and greedy have comparable contingency sizes;
- LP-guided greedy performs substantially more selection work;
- random baselines produce larger feasible contingencies;
- all methods operate on the same target and finite query instance.

Because this is outcome-aware screening, publications should disclose the
selection thresholds in `experiment_settings.json` and label these results as
screened benchmark instances. To support claims of general superiority, also
report an unscreened or held-out query suite. Do not silently present selected
instances as random samples of all possible queries.

## Parallel execution

Multiple terminals are safe only if they cannot switch the same Neo4j
installations underneath each other. With the default shared instance root and
shared Bolt port, do **not** run RSP and RES dataset runners concurrently: each
runner may stop the database the other runner is using.

Safe parallel options are:

1. separate Neo4j instance roots and different Bolt/HTTP ports;
2. separate machines or containers; or
3. a single already-active dataset with both jobs configured never to switch
   instances.

Also use distinct `--output-root` paths for every concurrent job.

## Troubleshooting

### `unrecognized arguments: --config ...`

You invoked `RSP-RD.py` instead of `run_all_rsp_experiments.py`, or you have an
older runner. `--config`, `--preflight-only`, and `--reuse-preflight-from`
belong to the experiment runner.

### `Unknown setting: s4` (or another setting)

Confirm that the command reads this repository's `experiment_settings.json`.
The file contains `s1` through `s4`. Use an absolute `--config` path when
running from another directory.

### The preflight appears stuck

- Check the last printed dataset, setting, and candidate.
- Wait for the configured `--preflight-timeout-seconds` boundary.
- Run `s4` separately and increase the timeout if the database is still doing
  useful work.
- Inspect Neo4j's query log and memory pressure.
- Keep the existing small preflight root and reuse it rather than restarting
  successful cells.

### `ModuleNotFoundError: neo4j` or `pulp`

Activate the virtual environment and install `requirements.txt`.

### CBC is unavailable

Verify PuLP can see a solver:

```bash
python -c "import pulp; print(pulp.listSolvers(onlyAvailable=True))"
```

Install CBC if `PULP_CBC_CMD` is absent.

### Wrong dataset or connection failure

Check `NEO4J_INSTANCE_ROOT`, directory names, executable `bin/neo4j` files,
credentials, and whether another unmanaged Neo4j process occupies port 7687.

## Reproducibility checklist

- Record the Git commit hash: `git rev-parse HEAD`.
- Preserve `experiment_settings.json` with each result set.
- Record Python, Neo4j, PuLP, CBC, OS, CPU, and memory versions.
- Use the same seed list for stochastic methods.
- Preserve resolved preflight artifacts and their metadata.
- Verify zero unexpected timeouts before interpreting aggregate results.
- Never compare runs generated from different database snapshots without
  documenting the difference.

## License and dataset terms

No open-source license is included yet. Add an appropriate license before
inviting external reuse; without one, others generally do not have permission
to copy, modify, or redistribute the code. Dataset licenses and attribution
requirements are separate and must be documented for every imported dataset.

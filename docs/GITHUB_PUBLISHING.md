# Publishing this repository on GitHub

This guide assumes the project directory is named `rsp-rd-experiments` and is
not already a Git repository.

## What belongs in the repository

Commit these files:

```text
.env.example
.gitignore
README.md
RSP-RD.py
Pattern_utils.py
experiment_settings.json
requirements.txt
run_all_rsp_experiments.py
utils.py
docs/
```

Do not commit:

- `.env` or any Neo4j password;
- Neo4j database directories;
- generated run logs and result trees;
- Python virtual environments or caches;
- large raw datasets unless their licenses allow redistribution and Git LFS is
  intentionally configured.

The included `.gitignore` covers the common local files, but it is not a
substitute for reviewing `git status`.

## Before the first commit

From the parent directory:

```bash
cd rsp-rd-experiments

python -m py_compile \
  RSP-RD.py \
  Pattern_utils.py \
  run_all_rsp_experiments.py \
  utils.py
python -m json.tool experiment_settings.json >/dev/null

git init
git add .
git status
git diff --cached --check
git diff --cached
```

Read the staged diff. In particular, search for accidental credentials:

```bash
git grep --cached -n -I -E '(password|passwd|secret|token|api[_-]?key)'
```

Expected documentation references such as `NEO4J_PASSWORD` will appear; real
credential values must not.

Create the first commit:

```bash
git commit -m "Add reproducible multi-dataset P-RSP experiments"
git branch -M main
```

## Create the GitHub repository

On GitHub, choose **New repository**, select a name, and create an empty
repository. Do not initialize it with a README, `.gitignore`, or license because
those choices would create a separate initial commit.

Then connect and push, replacing the URL:

```bash
git remote add origin https://github.com/YOUR-USER/YOUR-REPOSITORY.git
git remote -v
git push -u origin main
```

GitHub does not accept an account password for command-line Git over HTTPS. Use
one of these authentication methods:

- sign in with the GitHub CLI using `gh auth login`; or
- use a fine-grained personal access token when Git asks for the HTTPS
  password; or
- configure an SSH key and use
  `git@github.com:YOUR-USER/YOUR-REPOSITORY.git` as the remote.

The GitHub CLI can create and push the repository in one flow:

```bash
gh auth login
gh repo create YOUR-REPOSITORY --source=. --public --remote=origin --push
```

Use `--private` instead of `--public` until the license, dataset provenance,
and documentation are ready.

## Subsequent updates

```bash
git status
git add README.md RSP-RD.py run_all_rsp_experiments.py \
  experiment_settings.json utils.py Pattern_utils.py docs
git diff --cached
git commit -m "Describe the change"
git push
```

Prefer focused commits: separate algorithm changes, experiment-configuration
changes, and documentation changes when possible. This makes result provenance
easier to audit.

## If the remote was initialized already

The cleanest option is to clone that repository and copy these project files
into the clone. If the remote contains only an intentional initial README or
license, connect it and rebase before pushing:

```bash
git remote add origin https://github.com/YOUR-USER/YOUR-REPOSITORY.git
git fetch origin
git pull --rebase origin main
git push -u origin main
```

Resolve conflicts rather than using a force push. Never use `--force` merely to
make the first push succeed.

## Results and research releases

Keep large generated outputs out of the main source history. For a reproducible
release:

1. tag the exact source commit;
2. archive `experiment_settings.json`, `preflight.csv`, `resolved_queries/`,
   `aggregate_results.csv`, environment/version information, and checksums;
3. attach a compressed archive to a GitHub Release or deposit it in a research
   artifact repository;
4. document dataset versions and redistribution restrictions.

Example tag:

```bash
git tag -a v1.0.0 -m "First reproducible P-RSP experiment release"
git push origin v1.0.0
```

Before making the repository public, add a software license, dataset
provenance/import instructions, and a citation file containing the real author
and repository metadata.

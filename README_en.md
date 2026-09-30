<div align="center">

<a href="README.md">中文</a> | <b>English</b>

<img src="docs/images/panel-board.png" width="820" alt="Score mode: weighted leaderboard (the sample pair is drawn at random)">

# AI PK · Model Arena

**Same tasks, same ruler — but you decide how the ruler is weighted.**

![python](https://img.shields.io/badge/Python-3.10%2B-blue?style=flat-square)
![deps](https://img.shields.io/badge/deps-httpx%20%2B%20pyyaml-blueviolet?style=flat-square)
![license](https://img.shields.io/badge/License-MIT-green?style=flat-square)
![gates](https://github.com/xmyl-153/ai-pk/actions/workflows/gates.yml/badge.svg)

This project exists to solve exactly one thing: **measure model performance the way users actually experience it.**

It does not rank thrones or chase leaderboards — a leaderboard only tells you "who scored higher", not "how fast, how expensive, how stable, how different under another harness".
This project measures exactly those differences you pay for every day, and lets anyone recompute them from the raw evidence.

</div>

---

## Features

| | Feature | What it does |
|---|---|---|
| 🎮 | **Arena panel · two modes** | Double-click `启动对战台.bat` (or `python -m aipk panel`) to open a local web page. **Score mode** (default tab) picks 1–8 contestants and produces a weighted leaderboard; multiple submissions are automatically **queued**. **PK mode** drags two contestants into left/right arenas and declares a winner. An **About** tab shows version / update check / contact |
| 💡 | **Test idea pool** | "See-it-at-a-glance" brain-teaser prompts (SVG pelican riding a bicycle, pure-CSS prisoner's dilemma, …): one-click copy, each with "what to look at" + "limitations", and an explicit statement of what fun tests can and cannot tell you versus the scored benchmark |
| 🎚️ | **Custom weighting** | Four dimensions — quality / speed / cost / reasoning share — with presets (balanced / performance-first / cheap-first / speed-first) plus sliders; change weights and the leaderboard **re-ranks live**. "Default score high ≠ feels good to use" |
| 🔑 | **Bring your own models** | The top-right "＋ Connect my models" gives a 3-step guide and a config template; keys come from environment variables and are never written to disk |
| 🖥️ | **CLI runs** | `python -m aipk run` finishes (models × task families × repeats) in one command and emits HTML / Markdown / `summary.json` reports |
| 🧩 | **13 task families** | Shift scheduling, tool-chain following, cascading debugging, dirty-data transformation, hard-format compliance, multi-turn requirement changes, long-context rules, find-the-flaw rewriting, small-repo debugging, long-horizon state, premise checking, minimal diff, long-session consistency |
| ⚖️ | **Code grading first** | Whatever can be graded by code never goes to a judge (real code execution, AST semantic comparison, workspace snapshots); only open-ended text is blind-judged, and then always double-judged with positions swapped, reporting the flip rate |
| 🧰 | **Harnesses are testable too** | Run the same tasks with the same model under different harnesses (including real agent CLIs) and measure what the "shell" itself contributes |
| 📁 | **All evidence on disk** | Every run's raw records, per-task verdicts and reports live in `runs/<timestamp>/`; reports can be recomputed offline from the records |
| 🔌 | **Playable offline** | With no gateway and zero spend, four offline robots (perfect / half-right / slow / wrong) exercise the whole pipeline |
| 🔐 | **Never touches your keys** | Keys are read only from environment variables or a credentials file — never written to disk, never uploaded; the panel listens on `127.0.0.1` only |

## Good to know

The important stuff is deliberately up front; it saves a lot of time:

1. **Real-model runs must be rate-limited** (default `--qps 1.2`). Without limiting, gateways 429 you into a "fake zero" that looks like the model got dumber but is really a queueing problem.
2. **Don't run two sessions at once.** Two runs hammering the same gateway trigger the same rate limits.
3. **Give staged tasks more turns.** Start `decay` / `longstate` with `--max-turns 40`, otherwise sessions get truncated — that is truncation, not model drift.
4. **Conclusions apply to this run only.** The framing is always "in the run with seed=X, config=Y"; **never "model X is better overall"**.
5. **Offline robots' latencies are scripted.** They only verify "is this ruler accurate"; they say nothing about any real model.
6. **Panel runs are real runs**: picking real models really costs money (billed by your gateway) — read the contestant card's note before you start.

## Install

### What you need

- Python 3.10 or newer (developed on 3.12; CI runs 3.10 and 3.12)
- Two dependencies: `httpx`, `pyyaml`. **No GPU, no torch, no API key required**

### Two steps

```bash
git clone https://github.com/xmyl-153/ai-pk && cd ai-pk
pip install -r requirements.txt
python -m aipk demo          # offline end-to-end check: make sure the rig works
```

Windows users can also double-click **`启动对战台.bat`**, which starts the panel on the default port and opens the browser.

### Bring your own gateway (optional; offline play works without it)

Copy `aipk.config.yaml.example` to `aipk.config.yaml` (or run `python -m aipk init` for a template) and fill in any OpenAI-compatible endpoint:

```yaml
providers:
  mygateway:
    base_url: https://api.example.com/v1
    api_key_env: MY_GATEWAY_API_KEY     # key comes from this env var; don't commit it
roster:
  - [mygateway, strong-model, "Some flagship", flagship]
  - [mygateway, fast-model,   "Some light one", light]
```

Then run `python -m aipk list` to confirm it was read; "connected models" light up in the panel.

### Packaging & common pitfalls

- **There really are only two dependencies**: install failures are almost always network issues — add a mirror index if needed.
- **Gateway doesn't report usage**: tokens are estimated from character counts; reports mark them with `≈` — don't treat them as an exact bill.
- **Gateway 400 "temperature not supported"**: such parameter errors are not counted as model mistakes; the program automatically downgrades and retries, and the report marks "parameter downgrade".
- **Port in use**: the panel automatically probes the next free port; read the address it prints at startup.
- **`runs/` never enters the repo** (excluded by `.gitignore`): it lives only on your machine, delete anytime.

## Usage

### 1. Play with the panel first (no config, no spend)

```bash
python -m aipk panel          # or double-click 启动对战台.bat
```

The browser opens `http://127.0.0.1:8771/` automatically (next free port if occupied). The default tab is **Score mode**:

**Score mode** — weighted leaderboard for 1–8 models:
1. Click contestants in the **entry list** to add / remove them (**one model works**, up to 8);
2. Pick **weights**: click a preset (balanced / performance-first / cheap-first / speed-first) or drag the four sliders (quality / speed / cost / reasoning share);
3. Choose scope and click **Start scoring**. **Multiple submissions queue automatically** (one run at a time, to avoid hammering gateways into rate limits); when done you get a leaderboard that **re-ranks live** as you change weights — no re-run needed.
4. No gateway? Still look: click **"See real cases"** to **draw 2 models at random** from this repo's 630 real runs (10 models × 63 tasks) and inspect their four-dimensional metrics; drag weights to watch the ranking flip; click again for another pair.

**PK mode** — two models, left vs right: drag into the left/right arena (or click a card then an arena), click **Fight**, and get the score, per-task right/wrong cells and a one-line verdict (ties are broken by speed and bill).

**Test idea pool** — copy a brain-teaser prompt (e.g. SVG pelican riding a bicycle) and paste it to any model that can write code; each entry lists "what to look at" and "limitations". The division of labour is explicit: the idea pool is for **getting a feel**, scoring is for **drawing conclusions** — neither represents "overall intelligence".

**About tab** — version, **update check**, author contact (issues / PRs), and the author's blog.

**Connect your own models**: top-right "＋ Connect my models", follow the 3-step guide to fill `aipk.config.yaml` (keys via env vars). Real models really cost money; offline robots don't.

### 2. CLI cheat sheet

```bash
python -m aipk selfcheck                    # rig self-check: reference answers must be graded right
python -m aipk demo                         # offline end-to-end (perfect robot 100%, wrong robot 0%)
python -m aipk preview --family repofix     # see what one task looks like
python -m aipk init                         # generate a config template
python -m aipk run --reps 3 --tasks-per-family 3 --qps 1.2 --workers 6
python -m aipk report --run runs/<timestamp>    # HTML + Markdown + summary.json
```

### 3. Full flow for a real-model run

1. `python -m aipk list` — confirm gateways and roster were read;
2. `python -m aipk smoke --model <your-model-name> --family premise` — single-task smoke test first;
3. `python -m aipk run --qps 1.2` — the real run; read the report at `runs/<timestamp>/report.html`.

### 4. The 13 task families

| Family | What it probes | How it is graded |
|---|---|---|
| `constraint` shift scheduling | real reasoning vs pattern recall | backtracking solver guarantees a unique solution |
| `toolchain` tool-chain following | can it use tools, stay on task | exact numeric comparison (with nonexistent and unreachable files) |
| `cascade` cascading debugging | runs code to verify, or guesses | **really executes** the fixed code + AST semantic comparison |
| `datatransform` dirty-data transformation | attention to detail | field-by-field comparison against a reference implementation |
| `compliance` hard-format compliance | does it follow instructions | 9 constraints checked mechanically, one by one |
| `multiturn` following requirement changes | multi-turn state tracking | satisfies v2 with no v1 residue |
| `longcontext` long-context rules | really read it, or pretended | exact count/sum |
| `writing` find-the-flaw rewriting | judgment + expression | code locates the flaw + **blind-judged** quality |
| `repofix` small-repo debugging | truly agentic: navigate + debug + execute | repo written to disk and **really run** + output value comparison |
| `longstate` long-horizon state | keeps state across many turns | batched delivery, exact batch-by-batch comparison |
| `premise` premise checking | obeys / corrects / clarifies when the user slips in a **false premise** | three scenarios cross-exampled to block all three arbitrage routes |
| `mindiff` minimal diff | touches ten places to fix one? | workspace snapshot + touched-line budget |
| `decay` long-session consistency | does the same fact drift when asked twice | real multi-turn + cross-contamination fuse + self-reported history check |

## Screenshots

(real local runs, for reference only)

### Score mode: pick contestants, set weights

![Score mode](docs/images/panel-score.png)

### Weighted leaderboard: change weights, change ranking

![Weighted leaderboard](docs/images/panel-board.png)

### PK mode: left vs right, winner declared

![PK mode](docs/images/panel-pk.png)

### Test idea pool: brain-teaser prompts + what to look at / limitations

![Test idea pool](docs/images/panel-ideas.png)

### CLI report

![HTML report](docs/images/report.jpg)

## Room for improvement

**This project has not been tested at scale by real users; improvement suggestions from anyone are very welcome.**

The author is a student — fixes come slowly, but everything submitted gets read. The four most wanted (see [`CONTRIBUTING.md`](CONTRIBUTING.md)):

1. **Report a measurement defect** (top priority) — you find "some model behaves abnormally badly", investigate, and it turns out to be the measurement code: open an issue with the reproduction command and raw records;
2. **Add a task family** — new task types, new grading methods;
3. **Wire up a real shell** — wrap CLIs like Claude Code behind the same interface to thicken the "harness sensitivity" dimension;
4. **Clarify a pitfall** — any sentence in the docs that misleads people, just fix it.

Before asking, glance at the common pitfalls in [`docs/DEPLOY.md`](docs/DEPLOY.md) and the capability boundaries in [`docs/ASSESSMENT.md`](docs/ASSESSMENT.md).

## Statements

- This project is open source, free, MIT-licensed, for learning and exchange only.
- **It gives no "absolute intelligence score" and supports no conclusion of the form "model X is better than Y overall"** — the task distribution is human-chosen, and choosing is bias;
  it only supports "in the run with seed=X, config=Y, time=T, system Z behaved as …". Full boundaries are in [`docs/ASSESSMENT.md`](docs/ASSESSMENT.md), section 6.
- The panel wallpaper comes from the author's personal blog project (personal material; contact the author for removal if licensing is an issue).
- Costs of real-model runs are borne by the user's own gateway.

## Acknowledgements

Standing on the shoulders of these projects — and borrowing plenty of their good habits:

- **Terminal-Bench / Harbor** — "zero-cost oracle self-check" (`aipk demo` follows exactly this idea);
- **agent-harness** — the hard requirement "must run without any API key by default";
- **SWE-rebench (NeurIPS 2025)** — decontamination and continuously refreshed regression sets;
- **Claw-SWE-Bench** — treating the harness as a controlled variable;
- **LLM-as-a-Judge research** — position bias and judge effective votes, turned into engineering constraints here;
- and everyone who shares evaluation pitfall stories on the internet.

## More docs

| File | Contents |
|---|---|
| [`docs/DESIGN.md`](docs/DESIGN.md) | Design: four principles, five layers, the trap planted in each family (Chinese) |
| [`docs/FINDINGS.md`](docs/FINDINGS.md) | Measured findings with all numbers and provenance (Chinese) |
| [`docs/MEASUREMENT_DEFECTS.md`](docs/MEASUREMENT_DEFECTS.md) | 21 measurement defects + general rules — a self-check list for evaluators (Chinese) |
| [`docs/ASSESSMENT.md`](docs/ASSESSMENT.md) | Is this design valid? Can it keep up with new models? + peer comparison (Chinese) |
| [`docs/DEPLOY.md`](docs/DEPLOY.md) | Deployment, gateways, adding task families, common pitfalls (Chinese) |
| [`CONTRIBUTING.md`](CONTRIBUTING.md) | How to contribute (the four most valuable kinds) (Chinese) |
| `HANDOFF.md` / `CODEX.md` | Maintainer-view current state; handover instructions for the next AI/human (Chinese) |

<details>
<summary>Project layout</summary>

```
aipk/
  config.py      gateways / roster / budgets (user yaml first, DSH credentials fallback; keys never on disk)
  provider.py    OpenAI-compatible adapter: reasoning normalization, tool-call normalization, rate limiting, downgrade retries
  scripted.py    offline robots (perfect / half-right / slow / wrong; no key needed)
  oracle.py      reference-answer construction (shared by selfcheck and demo)
  tasks/         13 task families: generators + reference implementations + oracles
  harness.py     frozen protocol + fault injection + staging + repo-on-disk + workspace snapshots + external CLIs
  panel.py       arena panel: local web page + background runs (progress read straight from runs/<id>/runs.jsonl)
  web/           panel frontend (one HTML + one CSS + one JS)
  grade.py       position-debiased blind judging + judge audit
  metrics.py     Wilson CI / pass^k / consistency / saturation diagnostics / per-family sub-checks / judge audit
  arena.py       Bradley-Terry + bootstrap + harness sensitivity
  report.py      HTML + Markdown + summary.json
  runner.py      orchestration + raw evidence on disk
tests/           regression tests for measurement defects (this project's immune system)
tools/           judge calibration, multi-judge re-judging, variance reports, harness comparison, probes, troubleshooting
examples/        sample evidence (8 trimmed records, to see what the data looks like)
runs/            raw evidence per run (not in the repo)
```

</details>

<details>
<summary>Tests & gates: is this ruler itself trustworthy</summary>

- `python -m aipk selfcheck` — oracle self-consistency across the 13 families: "correct answers must not be graded wrong";
- `python -m aipk demo` — end-to-end gate needing no key at all: the perfect robot must score 100%, the wrong robot 0%;
- `python tests/test_measurement.py` — regression tests for the 21 measurement defects;
- CI (`.github/workflows/gates.yml`) runs all three plus a secret self-scan on every push, entirely key-free.

The common thread is one sentence: **every time "some model behaved abnormally badly" or "some metric looked too good to be true", the culprit turned out to be the measurement code.**
All 21 cases are documented, each with a regression test; the list lives in [`docs/MEASUREMENT_DEFECTS.md`](docs/MEASUREMENT_DEFECTS.md).

</details>

## License

MIT

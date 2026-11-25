# UXAgent (fork)

Fork of [neuhai/UXAgent](https://github.com/neuhai/UXAgent): LLM personas walk a website in a real browser, think aloud, and answer a questionnaire. This fork adds what was needed to audit real products with it: per-persona devices, subscription-backed LLM calls, a resumable driver and a report.

**This repository is public.** Nothing about a product under test belongs in it: no URLs of private servers, no selectors, no scenario texts, no findings, no run output. All of that lives in the project folder of the audit (see below).

## Commands

Run everything from the repository root; the config paths are relative.

```
uv sync                                                        # once
uv run python uxaudit.py personas <project>/audit.yaml         # demographics -> personas.json
uv run python uxaudit.py plan     <project>/audit.yaml         # personas x flows -> plan.json
uv run python uxaudit.py run      <project>/audit.yaml [flow|run_id ...]   # sessions + survey, resumable
uv run python uxaudit.py survey   <project>/audit.yaml         # questionnaire for sessions that have none
uv run python uxaudit.py report   <project>/audit.yaml         # runs/* -> results.json + tables
uv run python uxaudit.py quota    <project>/audit.yaml         # Codex subscription windows
```

There is no test suite. Verify a change with a two-session run against a site you control, `UXAGENT_PROVIDER=claude`, then `report`.

## The project folder

One audit = one folder outside this repository, usually in the audited product's ops or testing area.

| File | Written by | What |
|---|---|---|
| `audit.yaml` | you | demographics, devices, flows, questionnaire, milestones, limits. Documented in `examples/audit.example.yaml` |
| `hooks.py` | you, optional | what this one site needs: Chrome flags, sign-in, route stand-ins, waking faded controls, where a session must stop. Documented in `examples/hooks.example.py` |
| `personas.json` | `personas` | one entry per persona, with `pid` and `device` |
| `plan.json` | `plan`, or your own script | one entry per session: persona + `run_id`, `flow`, `intent`, `start_url`, `max_steps`, optional `context_options` (e.g. a signed-in `storage_state`), `upload_file`, `upload_label` |
| `runs/<stamp>/` | `run` | per session: actions, thoughts (`memory_trace.json`), simplified and raw HTML, a screenshot and a layout probe per step, `events.jsonl`, `usage.jsonl`, `survey_result.json`, `error.txt` on a crash |
| `results.json` | `report` | one row per session |

Flows that need seeded state (accounts, test data, a signed-in browser) get their `plan.json` from a script in the project folder. `uxaudit.py plan` only builds the plain persona x flow product.

## Where things are

- `uxaudit.py`: the driver. Chunks the plan, picks the LLM backend per chunk, runs the survey, builds the report.
- `src/simulated_web_agent/main/experiment.py`: one session. Device and context options, project hooks, event log, layout probe, notes for the persona (`tell`), loop guard, step timeouts.
- `src/simulated_web_agent/agent/cli_llm.py`: LLM calls through headless `codex exec` and `claude -p`, the usage ledger, local embeddings, the Codex quota probe.
- `src/simulated_web_agent/agent/gpt.py`: provider switch. `openai`, `aws`, `anthropic` go through litellm as upstream; `codex`, `claude`, `auto` go through `cli_llm.py`.
- `src/simulated_web_agent/agent/agent.py`, `memory.py`, `main/model.py`: the persona loop (perceive, feedback, plan, act, reflect, wonder, memory).
- `src/simulated_web_agent/executor/env.py`, `executor/parser/parser.js`: the browser and the page parser that turns the DOM into the simplified HTML the persona reads.
- `conf/base.yaml`: browser and timeout settings. `llm_provider` comes from `UXAGENT_PROVIDER`.

## Environment variables

| Variable | Default | Meaning |
|---|---|---|
| `UXAGENT_PROVIDER` | `openai` (`auto` under `uxaudit.py`) | `auto` = Codex until its usage window is spent, then Claude |
| `UXAGENT_LEAN` | `1` | CLI providers skip `wonder()` and score memory importance by kind: 5 calls per step. `0` restores the full loop with one batched importance call |
| `UXAGENT_CODEX_SMALL` / `_LARGE` | `gpt-6-luna` / `gpt-6.2-sol` | Codex models; planning uses the large one |
| `UXAGENT_CLAUDE_SMALL` / `_LARGE` | `haiku` / `sonnet` | Claude models |
| `UXAGENT_OPENAI_LARGE` | `openai/gpt-5` | planning model on the `openai` provider |
| `UXAGENT_CLI_CONCURRENCY` | `12` | CLI calls in flight at once |
| `UXAGENT_HOOKS` | `<project>/hooks.py` under `uxaudit.py` | project hooks file |
| `UXAGENT_RUNS_DIR` | `./runs` (`<project>/runs` under `uxaudit.py`) | where sessions are written |
| `UXAGENT_STUB_URLS` | empty | `glob=status,...`: non-GET requests matching a glob are answered locally |

## Things that cost time once

- **The persona reads HTML, not pixels.** It cannot see overlap, clipping or contrast. The layout probe (`probe/`) and the screenshots are there for that. Treat what a persona says as a lead and confirm it with a scripted browser before calling it a bug.
- **An open `aria-modal` dialog hides the rest of the page from the persona** (parser.js). Without that it clicks controls behind the dialog and every click times out.
- **`display: contents` wrappers** have no box. Upstream dropped them and their whole subtree.
- **Native UI is invisible**: file pickers, permission prompts, new tabs, blocked links. The harness handles them and uses `tell` to say so in the next observation. Add a `tell` whenever a hook does something a person would have seen.
- **A fresh Codex or Claude Code session prepends tens of thousands of tokens** of tools and instructions. The flags in `cli_llm.py` remove them (Codex: about 23k tokens per call down to about 5.6k). Re-measure after a CLI update: `codex exec --json` prints the usage.
- **Models copy quoted page text into JSON strings unescaped.** `gpt.py` repairs that. A session that retries the same call ten times is usually this.
- **Upstream's reflect loop never stopped** and spun as fast as the LLM answered. It now runs once per step and is cancelled when the session ends.
- **Parallel agent sessions on one machine kill each other's headless Chrome.** A session whose browser vanished gets `error.txt` and is run again by `run`, three attempts at most.
- **Cautious personas stop early.** A scenario that leaves the decision open ("if you want it, order it") measures the decision. To reach the screens behind it, write the decision into the scenario ("you have decided to...") and measure the decision in a separate flow.

## Rules for changes

- Keep upstream's entry points working (`run.py`, `experiment_async`, the litellm providers). The fork adds, it does not fork the API.
- Anything specific to one site goes into that project's `hooks.py` or `audit.yaml`, not into `src/`.
- No option without a need that came up in a real audit.
- Upstream is the `upstream` remote (add it with `git remote add upstream https://github.com/neuhai/UXAgent`). Rebase the fork's commits onto it when upstream moves.

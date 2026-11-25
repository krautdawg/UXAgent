"""Persona audit driver: many simulated users, each on their own device, walk the flows of one site.

Run from the repository root (config paths are relative), with a project folder that holds audit.yaml:

  uv run python uxaudit.py personas <project>/audit.yaml       # demographics -> personas.json
  uv run python uxaudit.py plan     <project>/audit.yaml       # personas x flows -> plan.json (or write plan.json yourself)
  uv run python uxaudit.py run      <project>/audit.yaml [flow|run_id ...]   # sessions + survey, resumable
  uv run python uxaudit.py survey   <project>/audit.yaml       # questionnaire for finished sessions that have none
  uv run python uxaudit.py report   <project>/audit.yaml       # runs/* -> results.json + tables
  uv run python uxaudit.py quota    <project>/audit.yaml       # Codex subscription windows

Everything a run writes stays in the project folder (personas.json, plan.json, runs/, results.json).
<project>/hooks.py, if present, is loaded as the project hooks (examples/hooks.example.py).
audit.yaml is documented in examples/audit.example.yaml.
"""
import asyncio
import collections
import json
import os
import pathlib
import random
import re
import statistics
import sys

import yaml

LIMITS = {
    "codex_weekly_stop": 90,   # percent of the weekly Codex window at which the run leaves Codex for good
    "codex_window_pause": 80,  # percent of the 5-hour Codex window at which chunks go to Claude until it resets
    "cost_cap_usd": 80,        # ledger cost (API cost, or the API-equivalent `claude -p` reports) at which the run stops
}
FRICTION = re.compile(r"confus|unclear|not sure|unsure|don't understand|do not understand|can't find|cannot find|no obvious|"
                      r"expected|unexpected|stuck|hesitat|worr|uneasy|uncomfortable|nervous|frustrat|annoy|didn't work|did not work|"
                      r"nothing happened|failed|where is|what does .* mean|jargon", re.I)


def flows_of(spec: dict) -> list[dict]:
    return spec.get("flows") or [{"name": "main", "intent": spec["general_intent"], "start_url": spec["start_url"],
                                  "max_steps": spec.get("max_steps", 15)}]


def spent(runs: pathlib.Path) -> float:
    return sum(json.loads(line).get("cost") or 0 for f in runs.glob("*/usage.jsonl") for line in f.read_text().splitlines())


async def cmd_personas(spec, here):
    from src.simulated_web_agent.main.persona import generate_personas
    people = await generate_personas(spec["demographics"], general_intent="", n=spec["total_personas"],
                                     example_text=spec.get("example_persona"))
    slots = [name for name, n in spec.get("devices", []) for _ in range(n)]  # an exact device mix, if the spec gives one
    random.Random(7).shuffle(slots)
    for i, p in enumerate(people):
        p["pid"] = f"P{i + 1:03d}"
        p.pop("intent", None)
        device = slots[i % len(slots)] if slots else p.get("Device")
        if device:
            p["device"] = device
            p["persona"] += f"\nDevice:\nRight now they are using: {device}."
    (here / "personas.json").write_text(json.dumps(people, indent=2, ensure_ascii=False))
    print("personas:", len(people))


def cmd_plan(spec, here):
    people = json.loads((here / "personas.json").read_text())
    plan = [{**p, "run_id": f"{p['pid']}-{f['name']}", "flow": f["name"], "intent": f["intent"], "start_url": f["start_url"],
             "max_steps": f.get("max_steps", spec.get("max_steps", 15))} for p in people for f in flows_of(spec)]
    (here / "plan.json").write_text(json.dumps(plan, indent=1, ensure_ascii=False))
    print(len(plan), "sessions:", dict(collections.Counter(e["flow"] for e in plan)))


async def cmd_survey(spec, here):
    from src.simulated_web_agent.main.survey import run_survey
    dirs = [f.parent for f in (here / "runs").glob("*/memory_trace.json")
            if not (f.parent / "survey_result.json").exists() and not (f.parent / "error.txt").exists()]
    await run_survey(trace_dirs=dirs, questionnaire=spec["questionnaire"], concurrency=8)


async def cmd_run(spec, here, only: list[str]):
    from src.simulated_web_agent.agent import cli_llm, gpt
    from src.simulated_web_agent.main.experiment import experiment_async
    from src.simulated_web_agent.main.survey import run_survey
    runs, lim, want = here / "runs", {**LIMITS, **spec.get("limits", {})}, gpt.provider
    tried = [json.loads(f.read_text()).get("run_id") for f in runs.glob("*/basic_info.json")]
    # A session that crashed (error.txt: a killed browser, a harness fault) is run again, three attempts at most.
    done = {json.loads(f.read_text()).get("run_id") for f in runs.glob("*/basic_info.json")
            if (f.parent / "survey_result.json").exists() and not (f.parent / "error.txt").exists()}
    todo = [e for e in json.loads((here / "plan.json").read_text())
            if e["run_id"] not in done and tried.count(e["run_id"]) < 3 and (not only or e["flow"] in only or e["run_id"] in only)]
    print(f"{len(todo)} sessions to run ({len(done)} already done)")
    for e in todo:  # a repeated attempt may need fresh data (e.g. an email address that is not taken yet)
        e["attempt"] = tried.count(e["run_id"])
        e["intent"] = e["intent"].replace("{attempt}", str(e["attempt"]))
    chunk, codex_over = spec.get("chunk", 8), False
    for i in range(0, len(todo), chunk):  # small chunks: surveys land early, a stop loses little, the backend can change
        provider, five, week = want, None, None
        if want in ("auto", "codex"):
            if not codex_over and not cli_llm.codex_spent:
                try:
                    q = cli_llm.codex_quota()
                    five, week = q["primary"]["used_percent"], q["secondary"]["used_percent"]
                except Exception:  # a spent window fails the probe itself
                    five = week = 100
                else:
                    codex_over = week >= lim["codex_weekly_stop"]
            if codex_over or cli_llm.codex_spent or five >= lim["codex_window_pause"]:
                if want == "codex":
                    print("Codex usage window spent, stopping", flush=True)
                    break
                provider = "claude"
        cost = spent(runs)
        print(f"chunk {i // chunk}: {i} sessions run, provider {provider}, codex 5h {five}% weekly {week}%, ledger ${cost:.2f}", flush=True)
        if cost > lim["cost_cap_usd"]:
            print("cost cap reached, stopping", flush=True)
            break
        os.environ["UXAGENT_PROVIDER"] = provider  # experiment_async reads the provider from the config
        dirs = await experiment_async(agents=todo[i:i + chunk], start_url=spec.get("start_url", ""), max_steps=spec.get("max_steps", 15),
                                      concurrency=spec.get("concurrency", 8), headless=True)
        dirs = [d for d in dirs if isinstance(d, pathlib.Path) and (d / "memory_trace.json").exists() and not (d / "error.txt").exists()]
        await run_survey(trace_dirs=dirs, questionnaire=spec["questionnaire"], concurrency=8)


# ---- report -------------------------------------------------------------------------------------

def device_class(d: str) -> str:
    if not d:
        return "?"
    if d[0].isdigit() or d.startswith(("Desktop", "Laptop")):
        return "desktop"
    if "iPad" in d or "Tab" in d:
        return "tablet"
    return "phone-landscape" if "landscape" in d else "phone"


def read_objs(path: pathlib.Path) -> list:
    """observation_trace.jsonl is concatenated JSON without newlines."""
    out, dec, s, i = [], json.JSONDecoder(), path.read_text() if path.exists() else "", 0
    while i < len(s):
        try:
            o, i = dec.raw_decode(s, i)
            out.append(o)
        except json.JSONDecodeError:
            break
    return out


def jsonl(path: pathlib.Path) -> list:
    return [json.loads(x) for x in path.read_text().splitlines() if x.strip()] if path.exists() else []


def sus(ans: dict) -> float | None:
    """System Usability Scale from questions sus1..sus10 (or q1..q10), each answered 1-5."""
    try:
        v = [int(ans.get(f"sus{i}") or ans[f"q{i}"]) for i in range(1, 11)]
    except (KeyError, ValueError, TypeError):
        return None
    return 2.5 * sum((x - 1) if i % 2 == 0 else (5 - x) for i, x in enumerate(v))


def load_run(d: pathlib.Path, milestones: dict, pages: dict) -> dict:
    info = json.loads((d / "basic_info.json").read_text())
    actions = [json.loads(a) for a in json.loads((d / "action_trace.json").read_text())] if (d / "action_trace.json").exists() else []
    obs = read_objs(d / "observation_trace.jsonl")
    mem = json.loads((d / "memory_trace.json").read_text()) if (d / "memory_trace.json").exists() else []
    survey = json.loads((d / "survey_result.json").read_text()) if (d / "survey_result.json").exists() else {}
    ans = {a["question_id"]: a.get("value") for a in survey.get("answers", [])} if isinstance(survey, dict) else {}
    usage, events = jsonl(d / "usage.jsonl"), jsonl(d / "events.jsonl")
    probes = [json.loads(p.read_text()) for p in sorted((d / "probe").glob("*.json"))] if (d / "probe").exists() else []
    reached, first = [], {}
    for n in range(len(actions) + 1):
        f = d / "simp_html" / f"simp_html_{n}.html"
        if not f.exists():
            continue
        tabs = obs[n].get("tabs") or [] if n < len(obs) else []
        url = next((t["url"] for t in tabs if t.get("is_active")), tabs[-1]["url"] if tabs else "")
        # Server pages often quote UI strings, so a page listed under `pages` is matched by URL only.
        seen = [name for name, part in pages.items() if part in url.split("?")[0]] or \
               [name for name, rx in milestones.items() if re.search(rx, f.read_text().lower(), re.S)]
        for name in seen:
            if name not in first:
                first[name] = n
                reached.append(name)
    keys = [(a.get("action"), a.get("target") or a.get("url")) for a in actions]
    device = info.get("device") or info.get("Device") or ""
    return {
        "run": d.name, "run_id": info.get("run_id", d.name), "pid": info.get("pid"), "flow": info.get("flow", "main"),
        "device": device, "device_class": device_class(device),
        "viewport": {"w": probes[0]["vw"], "h": probes[0]["vh"]} if probes else None,
        "demographics": {k: v for k, v in info.items() if isinstance(v, str) and k not in ("persona", "intent", "run_id", "pid", "flow", "start_url", "device", "upload_file")},
        "steps": len(actions), "max_steps": info.get("max_steps"),
        "terminated": bool(actions) and actions[-1].get("action") == "terminate",
        "crashed": (d / "error.txt").exists() or not actions,
        "milestones": reached, "milestone_step": first,
        "actions": [f"{a.get('action')}:{a.get('target') or a.get('url') or ''}" for a in actions],
        "repeats": sum(1 for i in range(1, len(keys)) if keys[i] == keys[i - 1] and keys[i][0] != "terminate"),
        "action_errors": [{"step": i, "error": o["error"][:200]} for i, o in enumerate(obs) if o.get("error")],
        "survey": ans, "sus": sus(ans),
        "cost": round(sum(u.get("cost") or 0 for u in usage), 4), "llm_calls": len(usage), "models": sorted({u.get("model") or "?" for u in usage}),
        "tokens_in": sum(u.get("in") or 0 for u in usage), "tokens_out": sum(u.get("out") or 0 for u in usage),
        "events": dict(collections.Counter(e["kind"] if e["kind"] != "http" else f"http{e['status']}" for e in events)),
        "event_log": [e for e in events if e["kind"] not in ("http", "console_error", "pageerror")],
        "http_errors": sorted({f"{e['status']} {e['url']}" for e in events if e["kind"] == "http"}),
        "console": sorted({e["text"] for e in events if e["kind"] in ("console_error", "pageerror") and "ERR_NAME_NOT_RESOLVED" not in e["text"]})[:8],
        "h_overflow_steps": sum(1 for p in probes if p["hOverflow"]),
        "small_targets": sorted({f"{b['label']} {b['w']}x{b['h']}" for p in probes for b in p["small"]}),
        "clipped": sorted({b["label"] for p in probes for b in p["clippedX"]}),
        "truncated": sorted({t for p in probes for t in p["truncated"]}),
        "friction": [f"[{m['timestamp']}] {m['content']}" for m in mem if m["kind"] in ("thought", "reflection") and FRICTION.search(m["content"])],
    }


def cmd_report(spec, here):
    rows = [load_run(d, spec.get("milestones", {}), spec.get("pages", {}))
            for d in sorted((here / "runs").iterdir()) if (d / "basic_info.json").exists()]
    latest = {}
    for r in rows:  # a re-run replaces the earlier attempt, unless the re-run is the one that crashed
        if r["run_id"] not in latest or not r["crashed"] or latest[r["run_id"]]["crashed"]:
            latest[r["run_id"]] = r
    rows = list(latest.values())
    (here / "results.json").write_text(json.dumps(rows, indent=1, ensure_ascii=False))
    by = collections.defaultdict(list)
    for r in rows:
        by[r["flow"]].append(r)
    print(f"{len(rows)} sessions, ledger ${sum(r['cost'] for r in rows):.2f}, steps {sum(r['steps'] for r in rows)}")
    print(f"{'flow':20}{'n':>4}{'SUS':>6}{'steps':>7}{'ended':>7}{'maxed':>6}{'errs':>6}{'crash':>6}")
    for flow, rs in sorted(by.items()):
        s = [r["sus"] for r in rs if r["sus"] is not None]
        print(f"{flow:20}{len(rs):>4}{statistics.mean(s) if s else 0:>6.0f}{statistics.mean(r['steps'] for r in rs):>7.1f}"
              f"{sum(r['terminated'] for r in rs):>7}{sum(bool(r['max_steps']) and r['steps'] >= r['max_steps'] for r in rs):>6}"
              f"{sum(bool(r['action_errors']) for r in rs):>6}{sum(r['crashed'] for r in rs):>6}")
    for flow, rs in sorted(by.items()):
        m = collections.Counter(x for r in rs for x in r["milestones"])
        if m:
            print(f"\n{flow}: " + ", ".join(f"{k} {v}/{len(rs)}" for k, v in m.most_common()))


if __name__ == "__main__":
    cmd, spec_path, args = sys.argv[1], pathlib.Path(sys.argv[2]).resolve(), sys.argv[3:]
    spec, here = yaml.safe_load(spec_path.read_text()), spec_path.parent
    os.environ.setdefault("UXAGENT_PROVIDER", spec.get("provider", "auto"))
    os.environ.setdefault("UXAGENT_RUNS_DIR", str(here / "runs"))
    os.environ.setdefault("UXAGENT_LIMIT_FLAG", str(here / "LIMIT"))
    if (here / "hooks.py").exists():
        os.environ.setdefault("UXAGENT_HOOKS", str(here / "hooks.py"))
    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
    from src.simulated_web_agent.agent import cli_llm, gpt

    gpt.provider = os.environ["UXAGENT_PROVIDER"]  # persona generation and the survey do not go through experiment_async
    if cmd == "personas":
        asyncio.run(cmd_personas(spec, here))
    elif cmd == "plan":
        cmd_plan(spec, here)
    elif cmd == "run":
        asyncio.run(cmd_run(spec, here, args))
    elif cmd == "survey":
        asyncio.run(cmd_survey(spec, here))
    elif cmd == "report":
        cmd_report(spec, here)
    elif cmd == "quota":
        try:
            q = cli_llm.codex_quota()
            print(f"codex 5h {q['primary']['used_percent']}% week {q['secondary']['used_percent']}% plan {q.get('plan_type')}")
        except Exception:  # a spent window fails the probe call itself
            print("no reading: the Codex usage window is spent, or codex is not signed in")
    else:
        sys.exit(__doc__)

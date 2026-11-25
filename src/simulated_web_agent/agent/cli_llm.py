"""LLM calls through headless CLI sessions on a subscription instead of an API key.

Providers (conf/base.yaml `llm_provider`, or env UXAGENT_PROVIDER):
  codex   `codex exec` on the ChatGPT subscription
  claude  `claude -p` on the Claude subscription
  auto    codex until its usage window is spent, then claude for the rest of the process

Every call is a fresh, tool-less session. The flags below strip what the CLIs normally prepend
(tools, skills, project docs, sub-agents): for Codex that is ~23k tokens per call down to ~5.6k.
"""
import asyncio
import json
import os
import re
import signal
import subprocess
import tempfile
import zlib
from pathlib import Path

import numpy as np

from . import context

PROVIDERS = ("codex", "claude", "auto")
CODEX_MODELS = {"small": os.environ.get("UXAGENT_CODEX_SMALL", "gpt-6-luna"), "large": os.environ.get("UXAGENT_CODEX_LARGE", "gpt-6.2-sol")}
CLAUDE_MODELS = {"small": os.environ.get("UXAGENT_CLAUDE_SMALL", "haiku"), "large": os.environ.get("UXAGENT_CLAUDE_LARGE", "sonnet")}
# Lean loop (default): no wonder() call and memory importance by kind, 5 calls per step instead of 7+.
LEAN = os.environ.get("UXAGENT_LEAN", "1") != "0"
INSTRUCTIONS = Path(__file__).parent / "codex_instructions.md"
JSON_RULE = " Reply with one JSON object and nothing else. Inside JSON strings write quoted page text with single quotes or escape it as \\\"."
CODEX_OFF = ("plugins skill_search multi_agent shell_tool unified_exec image_generation goals hooks tool_suggest sleep_tool "
             "computer_use browser_use in_app_browser remote_plugin collaboration_modes").split()
CODEX_FLAGS = ["--ignore-user-config", "--ignore-rules", *[a for f in CODEX_OFF for a in ("--disable", f)],
               "--enable", "skip_host_skill_discovery", "--enable", "omit_skill_descriptions",
               "-c", "agents.enabled=false", "-c", f'model_instructions_file="{INSTRUCTIONS}"',
               "-c", "project_doc_max_bytes=0", "-c", 'web_search="disabled"', "-c", "model_reasoning_effort=low",
               "--sandbox", "read-only", "--skip-git-repo-check"]
CODEX_LIMIT_RE = re.compile(r"usage limit|rate limit|quota|too many requests|plan limit|out of credits|limit (reached|exceeded)", re.I)
CLAUDE_LIMIT_RE = re.compile(r"usage limit|rate limit|limit reached|out of extra usage|credit balance", re.I)

codex_spent = False  # set when a Codex call reports its limit; "auto" then stays on Claude
_slots = None


def _text(m) -> str:
    return m["content"] if isinstance(m["content"], str) else json.dumps(m["content"])


def _ledger(model, tokens_in, cached, out, cost):
    """One line per call in <run>/usage.jsonl, so a run can be costed afterwards."""
    try:
        with open(context.run_path.get() / "usage.jsonl", "a") as f:
            f.write(json.dumps({"model": model, "in": tokens_in, "cached": cached, "out": out, "cost": cost}) + "\n")
    except Exception:
        pass  # calls outside a session (persona generation, survey) have no run directory


async def _run(args, stdin: str, cwd: str):
    global _slots
    if _slots is None:
        _slots = asyncio.Semaphore(int(os.environ.get("UXAGENT_CLI_CONCURRENCY", "12")))
    async with _slots:
        proc = await asyncio.create_subprocess_exec(*args, cwd=cwd, stdin=asyncio.subprocess.PIPE,
                                                    stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        try:
            out, err = await asyncio.wait_for(proc.communicate(stdin.encode()), 300)
        except asyncio.TimeoutError:
            proc.kill()
            raise
    return proc.returncode, out.decode(errors="replace"), err.decode(errors="replace")


async def codex_chat(messages, size, json_mode) -> str:
    prompt = (JSON_RULE.strip() + "\n\n" if json_mode else "") + "\n\n".join(f"<{m['role']}>\n{_text(m)}\n</{m['role']}>" for m in messages)
    with tempfile.TemporaryDirectory() as d:  # an empty working directory: nothing for the agent to read
        code, out, err = await _run(["codex", "exec", "-m", CODEX_MODELS[size], *CODEX_FLAGS, "--ephemeral", "--json",
                                     "-o", f"{d}/out.txt", "-"], prompt, d)
        text = Path(d, "out.txt").read_text() if Path(d, "out.txt").exists() else ""
    log = out + err
    if (not text.strip() and CODEX_LIMIT_RE.search(log)) or (len(text) < 400 and not text.lstrip().startswith("{") and CODEX_LIMIT_RE.search(text)):
        global codex_spent
        codex_spent = True
        print("CODEX LIMIT reached, switching to headless Claude", flush=True)
        return await claude_chat(messages, size, json_mode)
    if code or not text.strip():
        raise RuntimeError(f"codex exec failed ({code}): {log[-400:]}")
    usage = re.search(r'"usage":(\{[^}]*\})', log)
    u = json.loads(usage.group(1)) if usage else {}
    _ledger(CODEX_MODELS[size], u.get("input_tokens"), u.get("cached_input_tokens"), u.get("output_tokens"), 0)
    return text


async def claude_chat(messages, size, json_mode) -> str:
    system = "\n\n".join(_text(m) for m in messages if m["role"] == "system") + (JSON_RULE if json_mode else "")
    user = "\n\n".join(_text(m) for m in messages if m["role"] != "system")
    with tempfile.TemporaryDirectory() as d:
        code, out, err = await _run(
            ["claude", "-p", "--model", CLAUDE_MODELS[size], "--safe-mode", "--strict-mcp-config", "--tools", "", "--disable-slash-commands",
             "--no-session-persistence", "--system-prompt", system or INSTRUCTIONS.read_text(), "--output-format", "json"], user, d)
    try:
        r = json.loads(out)
    except json.JSONDecodeError:
        r = {"is_error": True, "result": out[-400:] + err[-400:]}
    text = r.get("result") or ""
    if r.get("is_error") or code or not text.strip():
        if CLAUDE_LIMIT_RE.search(text) and len(text) < 600:
            # Nothing left to fall back to, and a session in flight cannot wait hours on a live page: end the run. It is resumable.
            Path(os.environ.get("UXAGENT_LIMIT_FLAG", "CLAUDE_LIMIT")).write_text(text)
            os.kill(os.getpid(), signal.SIGTERM)
        raise RuntimeError(f"claude -p failed ({code}): {text[-400:]}")
    u = r.get("usage") or {}
    _ledger("claude-" + CLAUDE_MODELS[size], sum(u.get(k) or 0 for k in ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens")),
            u.get("cache_read_input_tokens"), u.get("output_tokens"), r.get("total_cost_usd"))  # cost = API-equivalent, not billed
    return text


async def chat(provider, messages, model, json_mode) -> str:
    size = "small" if model == "small" else "large"
    use_claude = provider == "claude" or (provider == "auto" and codex_spent)
    return await (claude_chat if use_claude else codex_chat)(messages, size, json_mode)


def embed(texts: list[str]) -> list[list[float]]:
    # shortcut: hashed bag-of-words, not an embedding model (the CLIs have none); use a local model if memory retrieval quality starts to matter
    out = []
    for t in texts:
        v = np.zeros(512)
        for w in re.findall(r"[a-zäöüß0-9']{3,}", t.lower()):
            v[zlib.crc32(w.encode()) % 512] += 1
        v = np.sqrt(v)
        n = np.linalg.norm(v)
        out.append((v / n if n else v).tolist())
    return out


def codex_quota() -> dict:
    """Usage of the Codex subscription windows ({'primary': 5-hour, 'secondary': weekly}, each with used_percent and resets_at).

    Read from the session file of one tiny persisted call; that file is removed again.
    """
    with tempfile.TemporaryDirectory() as d:
        r = subprocess.run(["codex", "exec", "-m", CODEX_MODELS["small"], *CODEX_FLAGS, "-C", d, "--json", "Reply with only: ok"],
                           capture_output=True, text=True, timeout=180, stdin=subprocess.DEVNULL)
    thread = re.search(r'"thread_id":"([^"]+)"', r.stdout).group(1)
    f = next((Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex") / "sessions").rglob(f"*{thread}*"))

    def find(o):
        if isinstance(o, dict):
            if o.get("rate_limits"):
                return o["rate_limits"]
            o = list(o.values())
        return next((x for x in map(find, o) if x), None) if isinstance(o, list) else None

    limits = [x for x in (find(json.loads(line)) for line in f.read_text().splitlines() if "rate_limits" in line) if x][-1]
    f.unlink()
    return limits



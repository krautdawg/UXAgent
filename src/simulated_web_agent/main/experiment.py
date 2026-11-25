import asyncio
import copy
import importlib.util
import json
import logging
import os
import pathlib
import shutil
import traceback
import uuid
from datetime import datetime
from typing import Callable, Dict, List, Optional

from dotenv import load_dotenv
from hydra import compose, initialize, initialize_config_dir
from omegaconf import DictConfig, OmegaConf
from playwright.async_api import async_playwright

from ..agent import context, gpt
from ..executor.env import WebAgentEnv  # Playwright env
from .model import AgentPolicy  # noqa

log = logging.getLogger("simulated_web_agent.main.experiment")

# Desktop sizes Playwright has no descriptor for. Any other device value is a Playwright device name or "WIDTHxHEIGHT".
DESKTOP_VIEWPORTS = {
    "Desktop 1920x1080": (1920, 1080),
    "Laptop 1440x900": (1440, 900),
    "Laptop 1366x768": (1366, 768),
}


def device_context_options(device, pw_devices) -> dict:
    """Browser context options for a persona's device: a Playwright device name, "1366x768", or a DESKTOP_VIEWPORTS key."""
    if not device:
        return {}
    size = DESKTOP_VIEWPORTS.get(device) or (tuple(map(int, device.split("x"))) if device[0].isdigit() and "x" in device else None)
    if size:
        return {"viewport": {"width": size[0], "height": size[1]}, "device_scale_factor": 1}
    # shortcut: every device runs in Chrome emulation (no WebKit); upgrade if Safari-only bugs matter
    return {k: v for k, v in pw_devices[device].items() if k != "default_browser_type"}


def load_hooks():
    """Project hooks: the Python file named by UXAGENT_HOOKS. Everything in it is optional (examples/hooks.example.py):
    LAUNCH_ARGS, CONTEXT_OPTIONS, before_session(env, entry, note, tell), before_observation(page, entry, note, tell),
    after_step(env, entry, note, tell) -> True ends the session."""
    path = os.environ.get("UXAGENT_HOOKS")
    if not path:
        return None
    spec = importlib.util.spec_from_file_location("uxagent_hooks", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# Layout facts the HTML-reading agent cannot see, measured per step at the persona's viewport.
PROBE_JS = """() => {
  const vw = innerWidth, vh = innerHeight, de = document.documentElement;
  const vis = (el) => { const r = el.getBoundingClientRect(), s = getComputedStyle(el);
    return r.width > 0 && r.height > 0 && s.visibility !== 'hidden' && s.display !== 'none' && parseFloat(s.opacity) > 0; };
  const label = (el) => (el.getAttribute('aria-label') || el.innerText || el.getAttribute('placeholder') || el.tagName).trim().slice(0, 40);
  const els = [...document.querySelectorAll('a,button,input:not([type=hidden]),select,textarea,[role=button],[role=switch],[role=link]')].filter(vis);
  const box = (el) => { const r = el.getBoundingClientRect(); return { label: label(el), w: Math.round(r.width), h: Math.round(r.height), x: Math.round(r.left), y: Math.round(r.top) }; };
  const inView = els.filter((el) => { const r = el.getBoundingClientRect(); return r.bottom > 0 && r.top < vh; });
  return {
    vw, vh, scrollH: de.scrollHeight, hOverflow: de.scrollWidth > vw + 1,
    controls: els.length,
    belowFold: els.filter((el) => el.getBoundingClientRect().top >= vh).length,
    small: inView.map(box).filter((b) => b.w < 38 || b.h < 38),
    clippedX: inView.map(box).filter((b) => b.x < -1 || b.x + b.w > vw + 1),
    truncated: [...document.querySelectorAll('button,a,h1,h2,h3,label,p:not([data-audit-note]),span')].filter((el) => vis(el) && el.children.length === 0 && el.scrollWidth > el.clientWidth + 1 && getComputedStyle(el).overflow !== 'visible').map(label).slice(0, 10),
  };
}"""
logging.basicConfig(level=logging.INFO)


async def _run_for_persona_and_intent(
    cfg: DictConfig,
    persona_info: Dict,
    start_url: str,
    max_steps: int,
    wait_for_login: bool = False,
    env_setup_hook: Callable = None,
    env_wait_hook: Callable = None,
    pw_devices: dict = None,
    hooks=None,
):
    persona = persona_info["persona"]
    intent = persona_info["intent"]
    # Each entry may carry its own start URL, step budget, device and browser context (e.g. a storage_state that is signed in).
    start_url = persona_info.get("start_url", start_url)
    max_steps = persona_info.get("max_steps", max_steps)
    context_options = {
        **device_context_options(persona_info.get("device") or persona_info.get("Device"), pw_devices or {}),
        **getattr(hooks, "CONTEXT_OPTIONS", {}),
        **(persona_info.get("context_options") or {}),
    }
    cfg = copy.deepcopy(cfg)
    OmegaConf.set_struct(cfg, False)  # device options add keys base.yaml lacks
    cfg.environment.browser.context_options = OmegaConf.merge(cfg.environment.browser.context_options, context_options)
    cfg.environment.browser.launch_options.args = [*cfg.environment.browser.launch_options.args, *getattr(hooks, "LAUNCH_ARGS", [])]
    log.info(
        f"\n=== persona (first 200 chars) ===\n{persona[:200]}...\n=== intent ===\n{intent}"
    )
    run_uid = uuid.uuid4().hex[:8]

    task_to_use = {
        "sites": ["shopping"],
        "task_id": 1,
        "require_login": False,
        "start_url": start_url,
        "intent": intent or "Interactive testing session",
    }

    run_name = f"{datetime.now().strftime('%Y-%m-%d_%H-%M-%S')}_{uuid.uuid4().hex[:4]}"
    trace_dir = pathlib.Path(os.environ.get("UXAGENT_RUNS_DIR") or "runs").resolve() / run_name
    # trace_dir.mkdir(parents=True, exist_ok=True)
    (trace_dir / "simp_html").mkdir(parents=True, exist_ok=True)
    (trace_dir / "raw_html").mkdir(parents=True, exist_ok=True)
    (trace_dir / "api_trace").mkdir(parents=True, exist_ok=True)
    (trace_dir / "screenshot").mkdir(parents=True, exist_ok=True)
    (trace_dir / "observation_trace").mkdir(
        parents=True, exist_ok=True
    )
    # save persona and intent
    (trace_dir / "basic_info.json").write_text(
        json.dumps(persona_info)
    )
    context.run_path.set(trace_dir)
    steps_taken = 0

    async def before_action_hook():
        if cfg.environment.recording.enabled:
            return
        # input()
        # save screenshot
        await env.page.screenshot(
            path=trace_dir / "screenshot" / f"screenshot_{steps_taken}.jpg",
            type="jpeg", quality=55, scale="css",
        )
        try:
            await project_hook("before_observation", env.page)
        except Exception:
            pass
        try:
            probe = await env.page.evaluate(PROBE_JS)
            probe["url"] = env.page.url
            (trace_dir / "probe").mkdir(exist_ok=True)
            (trace_dir / "probe" / f"probe_{steps_taken}.json").write_text(json.dumps(probe))
        except Exception as e:
            log.warning(f"probe failed: {e}")
        # get scroll top position
        scroll_top = await env.page.evaluate("window.scrollY")
        with open(trace_dir / "screenshot" / f"scroll_top_{steps_taken}.txt", "w") as f:
            f.write(
                str(
                    scroll_top
                    * cfg.environment.browser.context_options.device_scale_factor
                )
            )

    def note(kind, **data):
        with open(trace_dir / "events.jsonl", "a") as f:
            f.write(json.dumps({"step": steps_taken, "kind": kind, **data}) + "\n")

    pending = []  # [text, observations left]: things a person would know that the page HTML does not show

    def tell(text):
        pending.append([text, 2])  # each step observes twice (after the action, then before the next decision)

    async def project_hook(name, target):
        fn = getattr(hooks, name, None)
        return await fn(target, persona_info, note, tell) if fn else None

    async def wake_hook(page):
        if page.url.startswith("chrome-error://"):  # a page that did not load, e.g. an outside site blocked by --host-resolver-rules
            note("blocked_external")
            tell("(note: that link leads to a page that cannot be opened here; you are back where you were.)")
            if len(env.context.pages) > 1:
                await page.close()
                env.page = page = env.context.pages[-1]
            else:
                await page.go_back(wait_until="domcontentloaded")
        await project_hook("before_observation", page)
        texts = [t for t, _ in pending]
        for item in pending:
            item[1] -= 1
        pending[:] = [i for i in pending if i[1] > 0]
        await page.evaluate(
            """(texts) => {
              document.querySelectorAll('[data-audit-note]').forEach((n) => n.remove());
              if (!texts.length) return;
              const host = [...document.querySelectorAll('[role="dialog"][aria-modal="true"]')].pop() || document.body;
              const p = document.createElement('p');
              p.setAttribute('data-audit-note', '');
              p.style.cssText = 'position:fixed;left:0;top:0;width:2px;height:2px;overflow:hidden;font-size:1px;pointer-events:none;';
              p.textContent = texts.join(' ');
              host.prepend(p);
            }""",
            texts,
        )

    env = WebAgentEnv(
        cfg.environment, before_action_hook=before_action_hook, wait_hook=env_wait_hook or wake_hook
    )

    async def clear_cart(env):
        page = await env.context.new_page()

        # Initial navigation
        await page.goto(
            "https://www.amazon.com/fmc/ssd-storefront?ref_=nav_cs_SSD_nav_storefront",
            wait_until="networkidle",
        )

        while True:
            # Find all delete buttons currently visible
            delete_buttons = page.locator('button[data-action="a-stepper-decrement"]')
            count = await delete_buttons.count()

            print(f"Found {count} delete buttons")

            if count == 0:
                print("No more items to delete.")
                break

            # Always click the FIRST button, then reload the page
            btn = delete_buttons.nth(0)
            await btn.click()
            await env.observation()
        await page.close()

    log.info(f"[{run_uid}] env created")
    try:
        policy = AgentPolicy(persona, intent)
        print("setting up env with headless = " + str(cfg.environment.browser.launch_options.headless))
        await env.setup(task_to_use, headless=cfg.environment.browser.launch_options.headless)

        def wire(page):
            page.on("filechooser", lambda fc: asyncio.ensure_future(_pick(fc)))
            page.on("pageerror", lambda e: note("pageerror", text=str(e)[:300]))
            page.on("console", lambda m: m.type == "error" and note("console_error", text=m.text[:300]))
            page.on("response", lambda r: r.status >= 400 and note("http", status=r.status, url=r.url.split("?")[0][-120:]))

        async def _pick(fc):
            note("file_chooser")
            if persona_info.get("upload_file"):
                await fc.set_files(persona_info["upload_file"])
                tell(f"(note: the device's file picker opened and you chose {persona_info.get('upload_label', 'your file')} from it.)")
            else:
                tell("(note: the device's file picker opened; you have no suitable file and closed it again.)")

        def on_new_tab(page):
            wire(page)
            note("new_tab")
            env.page = page

        env.context.on("page", on_new_tab)
        # Answer these requests locally ("glob=status,...") so simulated users cannot trigger
        # real side effects on the tested site, such as sign-up mails. GETs pass through.
        for stub in filter(None, os.environ.get("UXAGENT_STUB_URLS", "").split(",")):
            glob, status = stub.rsplit("=", 1)

            async def answer(route, status=int(status)):
                if route.request.method == "GET":
                    await route.continue_()
                else:
                    await route.fulfill(status=status, json={})

            await env.context.route(glob, answer)
        await project_hook("before_session", env)  # sign-in, seeded state, route stand-ins: whatever this site needs
        wire(env.page)

        if wait_for_login:
            env.debug_pause()

        # await clear_cart(env)

        # Execute any custom setup actions if specified
        if env_setup_hook:
            await env_setup_hook(env)
        obs = await env.observation()

        log.info("Initial observation ready")

        action_trace = []
        while steps_taken < max_steps:
            with open(trace_dir / "observation_trace.jsonl", "a") as f:
                json.dump(obs, f)
            if obs.get("tabs"):
                current_url = obs["tabs"][0].get("url")
                print("Current url:", current_url)
            with open(
                trace_dir / "simp_html" / f"simp_html_{steps_taken}.html", "w"
            ) as f:
                f.write(obs["html"])
            with open(
                trace_dir / "raw_html" / f"raw_html_{steps_taken}.html", "w"
            ) as f:
                f.write(await env.page.content())

            # Use our policy to determine the action for this step from the environment
            action = await asyncio.wait_for(policy.forward(env), 1200)  # a stuck step ends the session, not the run
            action_trace.append(action)
            with open(trace_dir / "action_trace.json", "w") as f:
                json.dump(action_trace, f, indent=2)
            with open(
                trace_dir
                / "observation_trace"
                / f"observation_trace_{steps_taken}.txt",
                "w",
            ) as f:
                f.write(policy.agent.observation)
            # save memory trace
            with open(trace_dir / "memory_trace.json", "w") as f:
                json.dump(policy.agent.memory.memories, f)
            print(f"Taking action {action}")
            print(f"Action: {steps_taken + 1} out of {max_steps}")
            obs = await asyncio.wait_for(env.step(action), 300)
            steps_taken += 1
            # A person does not repeat the same one or two taps six times; end the session as stuck
            recent = [(json.loads(a).get("action"), json.loads(a).get("target")) for a in action_trace[-6:]]
            if len(recent) == 6 and len(set(recent)) <= 2:
                note("loop_abort", actions=[f"{a}:{t}" for a, t in recent[-2:]])
                break

            if obs.get("terminated"):
                break
            if await project_hook("after_step", env):
                break

        log.info(
            f"Finished persona run: terminated={obs.get('terminated')}, "
            f"score={obs.get('score')}, steps={steps_taken}"
        )

        # ---- save final memory trace ----
        final_memories_str = policy.get_formatted_memories()

        trace_file = trace_dir / f"{run_name}.txt"
        trace_file.write_text(final_memories_str, encoding="utf-8")

        log.info(f"Saved memory trace to {trace_file}")

    except Exception:
        err = traceback.format_exc()
        print(err)
        try:
            (trace_dir / "error.txt").write_text(err)
        except Exception:
            pass
    finally:
        try:
            await policy.close()
        except Exception:
            pass
        try:
            log.info(f"[{run_uid}] closing env...")
            await asyncio.wait_for(asyncio.shield(env.close()), timeout=10)
            log.info(f"[{run_uid}] env.close() completed")
        except Exception as e:
            log.exception(f"[{run_uid}] env.close() raised: {e!r}")
    return trace_dir

def _load_cfg(config_name: str = "base"):
    here = pathlib.Path(__file__).resolve().parent
    conf_dir = here.parents[2] / "conf"
    with initialize_config_dir(version_base=None, config_dir=str(conf_dir)):
        cfg = compose(config_name=config_name)
    return cfg

async def experiment_async(
    agents: List[Dict[str, str]],
    start_url: str,
    max_steps: int,
    *,
    headless=False,
    config_name: str = "base",
    config_path: str = ".",
    concurrency: int = 4,
    on_progress: Optional[Callable[[int, int], None]] = None,
) -> None:
    cfg = _load_cfg(config_name=config_name)
    if concurrency:
        cfg.environment.browser.user_data_dir = None
    gpt.provider = cfg.llm_provider
    print("llm provider: " + cfg.llm_provider)

    sem = asyncio.Semaphore(concurrency)
    hooks = load_hooks()
    async with async_playwright() as pw:
        pw_devices = dict(pw.devices)

    total = len(agents)
    done = 0
    lock = asyncio.Lock()  # protect shared counter in async context

    async def run_one(entry: Dict[str, str]):
        nonlocal done
        # persona = (entry.get("persona") or "").strip()
        # intent = (entry.get("intent") or "").strip()
        # if not persona or not intent:
        #     log.warning("Skipping agent: missing persona or intent")
        #     return
        async with sem:
            trace_dir = await _run_for_persona_and_intent(
                cfg=cfg,
                persona_info=entry,
                start_url=start_url,
                max_steps=max_steps,
                pw_devices=pw_devices,
                hooks=hooks,
            )

        # --- progress tick ---
        async with lock:
            done += 1
            if on_progress:
                try:
                    on_progress(done, total)
                except Exception:
                    pass
        return trace_dir

    tasks = [asyncio.create_task(run_one(e)) for e in agents]
    # This await is the "barrier": it doesn't return until ALL experiments finish
    results = await asyncio.gather(*tasks, return_exceptions=True)
    for r in results:
        if isinstance(r, Exception):
            log.exception("A session failed", exc_info=r)
    return results

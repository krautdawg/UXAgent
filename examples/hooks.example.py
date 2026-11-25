"""Project hooks: what one site needs that the generic harness cannot know. Copy to <project>/hooks.py.

uxaudit.py loads <project>/hooks.py on its own; other entry points take the path from UXAGENT_HOOKS.
Everything here is optional. In the functions:
  entry  the plan entry of this session (persona fields, run_id, flow, and any key your plan script added)
  note   note("kind", key=value) appends a line to <run>/events.jsonl, for the report
  tell   tell("(note: ...)") shows the persona a sentence for the next step: something a person would know
         that the page's HTML does not say (a native dialog, a download, a link that left the site)
"""
import asyncio

# Extra Chrome flags for every session.
LAUNCH_ARGS = [
    # Keep the simulated users on the server under test: every other host fails to resolve.
    "--host-resolver-rules=MAP * ~NOTFOUND, EXCLUDE 127.0.0.1, EXCLUDE localhost",
    # A fake camera and microphone, with the permission prompt answered.
    "--use-fake-device-for-media-stream",
    "--use-fake-ui-for-media-stream",
]

# Merged into every session's browser context (Playwright new_context options).
CONTEXT_OPTIONS = {"permissions": ["camera", "microphone"]}


async def before_session(env, entry, note, tell):
    """Once per session, after the start URL has opened and before the persona first looks at the page."""
    # Links to the production domain stay on the local copy of the same site.
    await env.context.route("https://www.example.com/**", lambda r: r.fulfill(status=302, headers={"location": "http://127.0.0.1:3000/"}))
    if entry.get("stub_payment"):  # a key your plan script sets on the sessions that need it
        # A stand-in for a hosted payment page: the site's own part runs for real, nothing can be paid.
        await env.context.route("https://checkout.example-pay.com/**",
                                lambda r: r.fulfill(content_type="text/html", body="<h1>Payment</h1><p>Stand-in for the payment page.</p>"))
        await env.page.reload()  # the first load happened before the routes were in place


async def before_observation(page, entry, note, tell):
    """Before every look at the page and before every action: bring the page into the state a person would see."""
    # Example: controls that fade out when idle. A person's first tap only wakes them; log that, then wake them.
    if await page.evaluate("!!document.querySelector('.controls.is-idle')"):
        note("controls_asleep", url=page.url)
        await page.mouse.move(20, 20)
        await asyncio.sleep(0.3)


async def after_step(env, entry, note, tell):
    """After every step. Return True to end the session here."""
    if "checkout.example-pay.com" in env.page.url:
        note("payment_page")
        return True

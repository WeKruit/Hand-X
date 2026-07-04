"""Toy proof: intercept a NATIVE file chooser (button -> detached <input type=file>.click(),
the React pattern hibob/bamboohr use) and inject the file via DOM.setFileInputFiles.

Serves a local page over http (file:// is blocked by SecurityWatchdog), clicks the button,
expects Page.fileChooserOpened to surface through cdp_use's EventRegistry, then sets the file
on the chooser's backendNodeId and asserts the page saw the change event.
"""

import asyncio
import http.server
import json
import sys
import threading
from pathlib import Path

sys.path.insert(0, ".")

PAGE = """<!doctype html><html><body>
<button id="add">Add file</button><div id="out">none</div>
<script>
document.getElementById('add').onclick = () => {
  const inp = document.createElement('input');   // DETACHED - never enters the DOM
  inp.type = 'file';
  inp.addEventListener('change', () => {
    document.getElementById('out').textContent = inp.files[0] ? inp.files[0].name : 'none';
  });
  inp.click();                                    // native OS picker (intercepted in test)
};
</script></body></html>""".encode()


class H(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.end_headers()
        self.wfile.write(PAGE)

    def log_message(self, *a):
        pass


async def main() -> None:
    srv = http.server.HTTPServer(("127.0.0.1", 0), H)
    port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()

    from browser_use import BrowserProfile, BrowserSession

    s = BrowserSession(browser_profile=BrowserProfile(headless=True, args=["--no-sandbox"]))
    await s.start()
    try:
        await s.navigate_to(f"http://127.0.0.1:{port}/")
        await asyncio.sleep(1.0)
        page = await s.must_get_current_page()

        cdp = await s.get_or_create_cdp_session()
        chooser: asyncio.Future = asyncio.get_event_loop().create_future()

        def on_chooser(event, session_id=None):
            if not chooser.done():
                chooser.set_result((event, session_id))

        cdp.cdp_client._event_registry.register("Page.fileChooserOpened", on_chooser)
        await cdp.cdp_client.send.Page.enable(session_id=cdp.session_id)
        await cdp.cdp_client.send.Page.setInterceptFileChooserDialog(
            params={"enabled": True}, session_id=cdp.session_id
        )

        # a synthetic .click() has NO user activation -> Chrome silently refuses to open the
        # chooser. Must be a TRUSTED click (Input.dispatchMouseEvent), same as the engine's.
        rect = json.loads(
            await page.evaluate(
                "() => JSON.stringify(document.getElementById('add').getBoundingClientRect())"
            )
        )
        cx, cy = rect["x"] + rect["width"] / 2, rect["y"] + rect["height"] / 2
        for t, extra in (("mousePressed", {"button": "left", "clickCount": 1}),
                         ("mouseReleased", {"button": "left", "clickCount": 1})):
            await cdp.cdp_client.send.Input.dispatchMouseEvent(
                params={"type": t, "x": cx, "y": cy, **extra}, session_id=cdp.session_id
            )
        event, sid = await asyncio.wait_for(chooser, timeout=8.0)
        print("CHOOSER EVENT:", json.dumps(event), "sid:", sid)

        node_id = event.get("backendNodeId")
        assert node_id, f"no backendNodeId in chooser event: {event}"
        resume = str(Path("fixtures/test_resume.pdf").resolve())
        await cdp.cdp_client.send.DOM.setFileInputFiles(
            params={"files": [resume], "backendNodeId": node_id}, session_id=sid or cdp.session_id
        )
        await asyncio.sleep(0.8)
        out = await page.evaluate("() => document.getElementById('out').textContent")
        print("PAGE SAW:", out)
        assert "test_resume.pdf" in str(out), f"page did not see the file: {out}"
        print(">>> TOY PASS — native chooser intercepted + file injected")
    finally:
        await s.kill()
        srv.shutdown()


if __name__ == "__main__":
    asyncio.run(main())

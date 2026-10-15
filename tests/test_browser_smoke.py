"""End-to-end smoke tests: drive the real app in headless Chrome.

This is the TDD hook for the frontend. Each test boots the actual FastAPI app
on a free port, opens it in headless Chrome via the Chrome DevTools Protocol,
deep-links the original sample songs in ``samples/``, and asserts real behavior — no
mocked DOM, no stubbed player.

Skipped automatically when Chrome or the vendored alphaTab assets are absent,
so the fast unit loop (``pytest -m "not browser"``) never depends on them.
"""

import asyncio
import contextlib
import functools
import http.server
import json
import os
import shutil
import socket
import subprocess
import threading
import time
import urllib.parse
import urllib.request
from pathlib import Path

import pytest
import uvicorn

from guitarz.__main__ import default_root
from guitarz.server import create_app
from guitarz.static_build import build_static

# Defined here rather than imported from conftest: conftest is pytest-loaded,
# and importing it as a module can resolve to an unrelated `tests` package
# elsewhere on sys.path (e.g. via PYTHONPATH leaking from another project).
REPO_ROOT = Path(__file__).resolve().parent.parent

# The sample songs are original tunes committed in samples/ (written by
# scripts/make_samples.py), so the suite runs on any clone and in CI without
# touching anyone's private collection.
SAMPLES = REPO_ROOT / "samples"
SAMPLE_GP = SAMPLES / "guitarz-demo.gp5"
LYRIC_GP = SAMPLES / "guitarz-lyrics.gp5"
DROPD_GP = SAMPLES / "guitarz-dropd.gp5"
EB_GP = SAMPLES / "guitarz-eb.gp5"

#: Preferred real song for the Guitar Pro 7 smoke test, when the collection has it.
PREFERRED_GP7 = "Joe Walsh-Funk*.gp"


def find_collection_gp7(root: Path) -> Path | None:
    """Find one real Guitar Pro 7/8 song in the private collection, if any.

    The committed samples are Guitar Pro 5 files (PyGuitarPro cannot write
    the newer format), but the user's real songs are GP7 ``.gp`` files — a
    zip container alphaTab parses with a different reader. This finder lets
    one test keep that reader covered on the dev machine. The song is only
    copied into a temp folder at test time and never committed; on CI and
    fresh clones there is no collection, so the test skips. Funk #49 is
    preferred (7 tracks, long-known to load) so runs are stable; otherwise
    the first ``.gp`` file by name whose bytes start with the zip signature.

    Args:
        root: the music collection folder (``$GUITARZ_ROOT`` or ``~/gps``).

    Returns:
        Path to a GP7 song, or None when the folder or such a song is missing.
    """
    if not root.is_dir():
        return None
    candidates = sorted(root.glob("**/" + PREFERRED_GP7)) + sorted(root.glob("**/*.gp"))
    for path in candidates:
        with path.open("rb") as f:
            if f.read(4) == b"PK\x03\x04":
                return path
    return None


COLLECTION_GP7 = find_collection_gp7(default_root())

#: Browser executables tried when nothing is on PATH: the standard install
#: locations on macOS and Windows, where Chrome is an app, not a command.
CHROME_APP_PATHS = [
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "/Applications/Chromium.app/Contents/MacOS/Chromium",
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
]


def find_chrome() -> str | None:
    """Locate a Chrome or Chromium executable for the headless smoke tests.

    ``$CHROME`` wins when set, then the usual command names on PATH (Linux,
    CI runners), then the app install locations. Looking only on PATH used
    to skip every browser test on macOS without a word, because Chrome there
    lives inside an ``.app`` bundle and is never on PATH.

    Returns:
        Path to the browser executable, or None when none is installed.
    """
    env = os.environ.get("CHROME")
    if env and Path(env).exists():
        return env
    for name in ("google-chrome", "google-chrome-stable", "chromium", "chromium-browser"):
        found = shutil.which(name)
        if found:
            return found
    return next((p for p in CHROME_APP_PATHS if Path(p).exists()), None)


CHROME = find_chrome()
VENDOR_JS = REPO_ROOT / "static" / "vendor" / "alphatab" / "alphaTab.min.js"

pytestmark = [
    pytest.mark.browser,
    pytest.mark.skipif(CHROME is None, reason="no Chrome/Chromium found (set $CHROME)"),
    pytest.mark.skipif(
        not VENDOR_JS.exists(),
        reason="alphaTab not vendored (run scripts/fetch_vendor.sh)",
    ),
]


def _free_port() -> int:
    """Ask the OS for an unused TCP port.

    Binding to port 0 lets the kernel pick, avoiding collisions with a dev
    server that may already be running on the default port 8008.

    Returns:
        An available port number on 127.0.0.1.
    """
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="module")
def live_server(tmp_path_factory):
    """Serve the real app (rooted at a copy of the sample song) in a thread.

    Runs uvicorn's event loop in a daemon thread rather than a subprocess so
    the fixture can signal a clean shutdown via ``server.should_exit`` and the
    test process owns the port for its whole lifetime.

    Yields:
        The server's base URL, e.g. ``http://127.0.0.1:54321``.
    """
    root = tmp_path_factory.mktemp("music")
    for sample in (SAMPLE_GP, LYRIC_GP, DROPD_GP, EB_GP):
        shutil.copy(sample, root / sample.name)
    if COLLECTION_GP7 is not None:
        shutil.copy(COLLECTION_GP7, root / "collection-gp7.gp")
    port = _free_port()
    server = uvicorn.Server(
        uvicorn.Config(
            create_app(root), host="127.0.0.1", port=port, log_level="warning"
        )
    )
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.time() + 15
    while not server.started:
        assert time.time() < deadline, "uvicorn did not start in time"
        time.sleep(0.05)
    yield f"http://127.0.0.1:{port}"
    server.should_exit = True
    thread.join(timeout=10)


class Cdp:
    """Minimal Chrome DevTools Protocol driver for one page.

    Just enough protocol to navigate, evaluate JavaScript (optionally with a
    synthetic user gesture, which WebAudio playback requires), and let event
    noise drain — deliberately tiny instead of pulling in Playwright for a
    couple of smoke tests.
    """

    def __init__(self, ws):
        """Wrap an open websocket to a page target.

        Args:
            ws: connected ``websockets`` client for the page's debugger URL.
        """
        self.ws = ws
        self.mid = 0

    async def send(self, method: str, params: dict | None = None) -> dict:
        """Issue one CDP command and wait for its matching response.

        Interleaved event notifications are discarded; only the reply whose id
        matches the request is returned, which keeps call sites synchronous.

        Args:
            method: CDP method name, e.g. ``"Runtime.evaluate"``.
            params: method parameters, if any.

        Returns:
            The ``result`` object of the CDP response.
        """
        self.mid += 1
        await self.ws.send(
            json.dumps({"id": self.mid, "method": method, "params": params or {}})
        )
        while True:
            msg = json.loads(await self.ws.recv())
            if msg.get("id") == self.mid:
                return msg.get("result", msg)

    async def js(self, expression: str, gesture: bool = False):
        """Evaluate a JavaScript expression in the page and return its value.

        Args:
            expression: JS source; may return a promise (awaited).
            gesture: mark the evaluation as user-initiated so gated APIs
                (AudioContext playback) are allowed to start.

        Returns:
            The JSON-serializable result value, or None.
        """
        res = await self.send(
            "Runtime.evaluate",
            {
                "expression": expression,
                "returnByValue": True,
                "awaitPromise": True,
                "userGesture": gesture,
            },
        )
        return res.get("result", {}).get("value")


@contextlib.asynccontextmanager
async def chrome_page(url: str, profile: Path | None = None):
    """Launch headless Chrome, attach CDP to its page, navigate, and clean up.

    A context manager so every test gets a fresh browser (no state bleed
    between tests) and Chrome is terminated even when assertions fail.

    Args:
        url: the address to navigate the page to after attaching.
        profile: optional persistent browser profile for cross-session tests.

    Yields:
        A connected :class:`Cdp` driver with the navigation already issued.
    """
    import websockets

    debug_port = _free_port()
    chrome = subprocess.Popen(
        [
            CHROME,
            "--headless=new",
            "--no-sandbox",
            "--disable-gpu",
            f"--remote-debugging-port={debug_port}",
            "--window-size=1400,1200",
            *([f"--user-data-dir={profile}"] if profile else []),
            "about:blank",
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        ws_url = None
        for _ in range(60):
            try:
                with urllib.request.urlopen(
                    f"http://127.0.0.1:{debug_port}/json/list"
                ) as r:
                    ws_url = next(t for t in json.load(r) if t["type"] == "page")[
                        "webSocketDebuggerUrl"
                    ]
                break
            except Exception:
                await asyncio.sleep(0.5)
        assert ws_url, "Chrome debugger never came up"
        async with websockets.connect(ws_url, max_size=50 * 1024 * 1024) as ws:
            cdp = Cdp(ws)
            await cdp.send("Page.enable")
            await cdp.send("Runtime.enable")
            await cdp.send("Page.navigate", {"url": url})
            yield cdp
    finally:
        chrome.terminate()
        chrome.wait(timeout=10)


async def wait_ready(cdp: Cdp) -> dict:
    """Poll until the score is rendered and the player is ready.

    Bounded at ~90 s to cover cold-cache SoundFont loading on slow disks;
    returns early the moment the app is usable.

    Args:
        cdp: driver attached to a page that is loading a song.

    Returns:
        The final status dict (overlayHidden/svg/rows/playEnabled) for
        follow-up assertions.
    """
    status = None
    for _ in range(30):
        await asyncio.sleep(3)
        raw = await cdp.js("""JSON.stringify({
            overlayHidden: document.getElementById('overlay').classList.contains('hidden'),
            svg: document.querySelectorAll('#alphatab svg').length,
            rows: document.querySelectorAll('.track-row').length,
            playEnabled: !document.getElementById('btn-play').disabled})""")
        status = json.loads(raw)
        if status["overlayHidden"] and status["svg"] > 0 and status["playEnabled"]:
            break
    assert status and status["overlayHidden"], (
        f"score never finished rendering: {status}"
    )
    assert status["svg"] > 0, f"no score SVG rendered: {status}"
    return status


def _song_url(live_server: str) -> str:
    """Build the deep-link URL that auto-opens the bundled sample song.

    Args:
        live_server: base URL from the ``live_server`` fixture.

    Returns:
        The app URL with the ``?open=`` query for the sample file.
    """
    return f"{live_server}/?open={urllib.parse.quote(SAMPLE_GP.name)}"


@pytest.fixture(scope="module")
def static_server(tmp_path_factory):
    """Serve the export with a plain HTTP server, without FastAPI or API routes.

    This verifies deployment behavior against the generated artifact rather
    than accidentally letting the development backend fulfill API requests.

    Yields:
        Base URL of the static-only server.
    """
    output = build_static(tmp_path_factory.mktemp("static-export"))
    handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=str(output))
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


#: Sub-path the static site is published under on GitHub Pages (#11).
PAGES_PREFIX = "/guitarz/"


@pytest.fixture(scope="module")
def static_subpath_server(tmp_path_factory):
    """Serve the export under ``/guitarz/``, the way GitHub Pages will host it.

    GitHub project Pages serve a repo's site at ``<user>.github.io/<repo>/``,
    so every asset and data path must resolve below that folder, not at the
    domain root. A plain HTTP server rooted one level up reproduces that.

    Yields:
        Base URL of the server root (the site itself is at ``+ PAGES_PREFIX``).
    """
    root = tmp_path_factory.mktemp("pages-root")
    build_static(root / PAGES_PREFIX.strip("/"))
    handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=str(root))
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_static_site_works_under_a_sub_path(static_subpath_server):
    """The static site loads, plays and links correctly from ``/guitarz/`` (#11).

    Spec: opened at ``/guitarz/?open=guitarz-demo.gp5`` the demo renders all
    7 tracks, the sample list loads, the SoundFont makes the player ready,
    and playback advances; every request the page makes stays inside
    ``/guitarz/`` (nothing leaks to the domain root, where it would 404 or
    hit another site), including the music font and the SoundFont; and
    every visible license link resolves inside ``/guitarz/`` and loads.
    """
    site = static_subpath_server + PAGES_PREFIX

    async def scenario():
        async with chrome_page(site + "?open=" + urllib.parse.quote(SAMPLE_GP.name)) as cdp:
            status = await wait_ready(cdp)
            assert status["rows"] == 7 and status["playEnabled"], status
            assert await cdp.js("document.querySelectorAll('#filelist li.file').length") == 5
            await cdp.js("document.getElementById('btn-play').click()", gesture=True)
            await asyncio.sleep(8)
            assert await cdp.js("Guitarz.conductor.state.api.playerState") == 1
            await cdp.js("document.getElementById('btn-stop').click()")

            paths = json.loads(await cdp.js(
                "JSON.stringify(performance.getEntriesByType('resource')"
                ".map(e => new URL(e.name)).filter(u => u.origin === location.origin)"
                ".map(u => u.pathname))"))
            outside = [p for p in paths if not p.startswith(PAGES_PREFIX)]
            assert not outside, f"requests escaped {PAGES_PREFIX}: {outside}"
            for needed in ("vendor/alphatab/font/Bravura.woff2",
                           "vendor/alphatab/soundfont/sonivox.sf2",
                           "catalog.json", "songs/" + SAMPLE_GP.name):
                assert PAGES_PREFIX + needed in paths, (needed, paths)

            links = json.loads(await cdp.js("""(async () => {
                const out = [];
                for (const a of document.querySelectorAll('#asset-info a')) {
                    if (a.closest('[hidden]') || new URL(a.href).origin !== location.origin) continue;
                    const res = await fetch(a.href);
                    out.push({path: new URL(a.href).pathname, ok: res.ok});
                }
                return JSON.stringify(out);
            })()"""))
            assert len(links) >= 5, links
            bad = [link for link in links if not (link["ok"] and link["path"].startswith(PAGES_PREFIX))]
            assert not bad, bad

    asyncio.run(scenario())


#: The real optional SoundFonts; the all-fonts browser test needs them on disk.
BIG_FONTS = [
    REPO_ROOT / "static" / "vendor" / "alphatab" / "soundfont" / name
    for name in ("MuseScore_General.sf3", "GeneralUser-GS.sf2")
]


@pytest.mark.skipif(
    not all(p.is_file() for p in BIG_FONTS),
    reason="optional SoundFonts not fetched (run scripts/fetch_vendor.sh without --core)",
)
def test_static_site_with_all_soundfonts(tmp_path):
    """The GitHub Pages build offers all three SoundFonts and plays a big one.

    Spec, on a real ``all_soundfonts`` export served under ``/guitarz/``:
    (1) the menu lists GeneralUser GS, MuseScore General and Sonivox, with
    Sonivox selected (big fonts download only on demand); (2) the licenses
    dialog shows the MuseScore and GeneralUser credits and their license
    links load from inside ``/guitarz/``; (3) choosing GeneralUser GS loads
    it and playback advances.
    """
    root = tmp_path / "pages-root"
    build_static(root / PAGES_PREFIX.strip("/"), all_soundfonts=True)
    handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=str(root))
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    site = f"http://127.0.0.1:{server.server_port}{PAGES_PREFIX}"

    async def scenario():
        async with chrome_page(site + "?open=" + urllib.parse.quote(SAMPLE_GP.name)) as cdp:
            await wait_ready(cdp)
            menu = json.loads(await cdp.js("""JSON.stringify({
                options: [...document.getElementById('soundfont').options].map(o => o.textContent),
                selected: document.getElementById('soundfont').value})"""))
            assert menu == {"options": ["GeneralUser GS", "MuseScore General", "Sonivox (light)"],
                            "selected": "sonivox.sf2"}, menu

            credits = json.loads(await cdp.js("""(async () => {
                const shown = id => !document.getElementById(id).closest('[hidden]');
                const links = [];
                for (const id of ['musescore-credit', 'generaluser-credit'])
                    for (const a of document.querySelectorAll('#' + id + ' a')) {
                        const res = await fetch(a.href);
                        links.push({path: new URL(a.href).pathname, ok: res.ok});
                    }
                return JSON.stringify({musescore: shown('musescore-credit'),
                                       generaluser: shown('generaluser-credit'), links});
            })()"""))
            assert credits["musescore"] and credits["generaluser"], credits
            assert credits["links"] and all(
                link["ok"] and link["path"].startswith(PAGES_PREFIX) for link in credits["links"]
            ), credits

            await cdp.js("""(() => {
                window.__sfLoaded = false;
                Guitarz.conductor.state.api.soundFontLoaded.on(() => { window.__sfLoaded = true; });
                const sel = document.getElementById('soundfont');
                sel.value = 'GeneralUser-GS.sf2';
                sel.dispatchEvent(new Event('change'));
            })()""")
            for _ in range(60):
                await asyncio.sleep(1)
                if await cdp.js("window.__sfLoaded && !document.getElementById('btn-play').disabled"):
                    break
            assert await cdp.js("window.__sfLoaded"), "GeneralUser GS never finished loading"
            await cdp.js("document.getElementById('btn-play').click()", gesture=True)
            await asyncio.sleep(8)
            assert await cdp.js("Guitarz.conductor.state.api.playerState") == 1
            assert not (await cdp.js("document.getElementById('pos-time').textContent")).startswith("0:00 /")
            await cdp.js("document.getElementById('btn-stop').click()")

    try:
        asyncio.run(scenario())
    finally:
        server.shutdown()
        server.server_close()


def test_static_catalog_render_and_play(static_server):
    """Static catalogs, explorer selection, deep links and playback need no API.

    Inspect actual browser requests to ensure the static branch does not call
    the Python endpoints, then exercise both deep-link and explorer loading.
    """

    async def scenario():
        """Drive the exported site and check real rendering and audio progress."""
        async with chrome_page(_song_url(static_server)) as cdp:
            status = await wait_ready(cdp)
            assert status["rows"] == 7
            assert status["playEnabled"]
            assert await cdp.js("document.getElementById('soundfont').value") == "sonivox.sf2"
            assert await cdp.js("document.querySelectorAll('#filelist li.file').length") == 5
            await cdp.js("document.getElementById('btn-play').click()", gesture=True)
            await asyncio.sleep(8)
            assert await cdp.js("Guitarz.conductor.state.api.playerState") == 1
            assert not (await cdp.js("document.getElementById('pos-time').textContent")).startswith("0:00 /")
            await cdp.js("document.getElementById('btn-stop').click()", gesture=True)
            await cdp.js("Array.from(document.querySelectorAll('#filelist li.file')).find(el => el.textContent.startsWith('guitarz-eb.gp5')).click()")
            await wait_ready(cdp)
            assert await cdp.js("Guitarz.conductor.state.transpose") == 1
            paths = json.loads(await cdp.js("JSON.stringify(performance.getEntriesByType('resource').map(e => new URL(e.name).pathname))"))
            assert not any(path.startswith("/api/") for path in paths)
            assert "/catalog.json" in paths
            assert "/soundfonts.json" in paths
            assert "/songs/guitarz-demo.gp5" in paths
            assert "/songs/guitarz-eb.gp5" in paths

    asyncio.run(scenario())


@pytest.mark.parametrize("server_fixture", ["static_server", "live_server"])
def test_asset_license_dialog(request, server_fixture):
    """Licenses are accessible before loading a song in both deployment modes.

    Check that the dialog lists the core assets and integrated libraries,
    serves every local license link, and returns focus when dismissed.
    """
    base = request.getfixturevalue(server_fixture)

    async def scenario():
        """Open the info dialog and follow its bundled license links."""
        async with chrome_page(base + "/") as cdp:
            for _ in range(50):
                if await cdp.js("Boolean(document.getElementById('btn-info')?.onclick)"):
                    break
                await asyncio.sleep(0.1)
            await cdp.js("document.getElementById('btn-info').click()", gesture=True)
            assert await cdp.js("document.getElementById('asset-info').open")
            text = await cdp.js("document.getElementById('asset-info').textContent")
            for name in ("alphaTab", "MPL-2.0", "Bravura", "SIL Open Font License", "Sonivox", "Apache-2.0", "TinySoundFont", "libvorbis"):
                assert name in text
            assert await cdp.js("Array.from(document.querySelectorAll('#asset-info a')).filter(a => new URL(a.href).origin === location.origin).length") >= 5
            assert await cdp.js("(async () => { const links = Array.from(document.querySelectorAll('#asset-info a')).filter(a => new URL(a.href).origin === location.origin); for (const link of links) { if (link.closest('[hidden]')) continue; const res = await fetch(link.href); if (!res.ok || !(await res.text()).length) return false; } return true; })()")
            await cdp.js("document.getElementById('asset-info-close').click()", gesture=True)
            assert not await cdp.js("document.getElementById('asset-info').open")
            assert await cdp.js("document.activeElement.id") == "btn-info"

    asyncio.run(scenario())


def test_local_library_survives_sessions(static_server, tmp_path):
    """Import a folder, play its song, restart Chrome, then remove local copies.

    Real native file inputs exercise directory paths and unsupported-file
    filtering. A dedicated profile verifies persistence across browser exits,
    while request inspection ensures imported files never travel to a server.
    """
    profile = tmp_path / "profile"
    folder = tmp_path / "My Tabs"
    nested = folder / "Set #1"
    nested.mkdir(parents=True)
    song = nested / "riff #1.GP5"
    shutil.copy(SAMPLE_GP, song)
    (folder / "notes.txt").write_text("not a song")
    (folder / ".hidden.gp5").write_bytes(b"private")

    async def choose(cdp, selector, paths):
        """Select real disk files through Chrome's native file input API."""
        document = await cdp.send("DOM.getDocument")
        node = await cdp.send("DOM.querySelector", {"nodeId": document["root"]["nodeId"], "selector": selector})
        await cdp.js("document.getElementById('library-status').textContent = ''")
        await cdp.send("DOM.setFileInputFiles", {"nodeId": node["nodeId"], "files": [str(p) for p in paths]})
        for _ in range(100):
            if await cdp.js("!document.getElementById('btn-import-files').disabled && document.getElementById('library-status').textContent.length > 0"):
                break
            await asyncio.sleep(0.1)

    async def ready(cdp):
        """Wait until library restoration enables the import controls."""
        for _ in range(100):
            if await cdp.js("document.getElementById('btn-import-files') && !document.getElementById('btn-import-files').disabled"):
                return
            await asyncio.sleep(0.1)
        pytest.fail("library UI did not initialize")

    async def scenario():
        """Exercise import, restoration, replacement, removal and clear controls."""
        async with chrome_page(static_server + "/", profile) as cdp:
            await ready(cdp)
            await choose(cdp, "#import-folder", [folder])
            assert await cdp.js("(async () => (await Guitarz.library.list()).length)()") == 1
            assert await cdp.js("(async () => (await Guitarz.library.list())[0].path)()") == "My Tabs/Set #1/riff #1.GP5"
            await cdp.js("document.querySelector('#filelist .dir').click()")
            await cdp.js("document.querySelector('#filelist .dir:not(:first-child)').click()")
            await cdp.js("document.querySelector('#filelist .file').click()")
            status = await wait_ready(cdp)
            assert status["rows"] == 7
            await cdp.js("document.getElementById('btn-play').click()", gesture=True)
            await asyncio.sleep(8)
            assert await cdp.js("Guitarz.conductor.state.api.playerState") == 1
            paths = json.loads(await cdp.js("JSON.stringify(performance.getEntriesByType('resource').map(e => new URL(e.name).pathname))"))
            assert not any(path.startswith("/api/") or path.startswith("/songs/") for path in paths)

        async with chrome_page(static_server + "/", profile) as cdp:
            await ready(cdp)
            assert await cdp.js("document.getElementById('library-source').value") == "local"
            assert await cdp.js("(async () => (await Guitarz.library.list()).length)()") == 1
            assert await cdp.js("(async () => (await Guitarz.library.read('My Tabs/Set #1/riff #1.GP5')).size)()") == song.stat().st_size
            # A mid-batch write failure rolls back all new songs and reports it.
            await cdp.js("""(() => {
                const original = IDBObjectStore.prototype.put;
                let writes = 0;
                IDBObjectStore.prototype.put = function(...args) {
                    if (++writes === 2) {
                        IDBObjectStore.prototype.put = original;
                        throw new DOMException('Simulated storage exhaustion', 'QuotaExceededError');
                    }
                    return original.apply(this, args);
                };
            })()""")
            await choose(cdp, "#import-files", [SAMPLE_GP, LYRIC_GP])
            assert "storage is full" in await cdp.js("document.getElementById('library-status').textContent")
            assert await cdp.js("(async () => (await Guitarz.library.list()).length)()") == 1
            # File selection adds a root-level entry, then reimport replaces it.
            await choose(cdp, "#import-files", [SAMPLE_GP])
            await choose(cdp, "#import-files", [SAMPLE_GP])
            assert await cdp.js("(async () => (await Guitarz.library.list()).length)()") == 2
            await cdp.js("document.querySelector('#filelist .remove-song').click()")
            for _ in range(50):
                if await cdp.js("(async () => (await Guitarz.library.list()).length)()") == 1:
                    break
                await asyncio.sleep(0.1)
            assert await cdp.js("(async () => (await Guitarz.library.list()).length)()") == 1
            await cdp.js("document.getElementById('btn-clear-library').click()")
            await cdp.js("document.getElementById('confirm-clear-library').click()")
            for _ in range(50):
                if await cdp.js("(async () => (await Guitarz.library.list()).length)()") == 0:
                    break
                await asyncio.sleep(0.1)
            assert await cdp.js("(async () => (await Guitarz.library.list()).length)()") == 0
            assert song.exists()
        async with chrome_page(static_server + "/", profile) as cdp:
            await ready(cdp)
            assert await cdp.js("(async () => (await Guitarz.library.list()).length)()") == 0

    asyncio.run(scenario())


def test_load_render_and_play(live_server):
    """The whole app works end-to-end: render all tracks, then really play.

    Asserts the three pillars the UI promises: (1) the conductor view renders
    every track of the sample (7 track rows, SVG score present, overlay gone),
    (2) the SoundFont player becomes ready (play button enabled), and (3) a
    user-gesture click on play starts the synth — verified by the player state
    flag and the position readout advancing past the count-in bar.
    """

    async def scenario():
        async with chrome_page(_song_url(live_server)) as cdp:
            status = await wait_ready(cdp)
            assert status["rows"] == 7, f"expected 7 track rows: {status}"
            assert status["playEnabled"], f"player never became ready: {status}"

            await cdp.js("document.getElementById('btn-play').click()", gesture=True)
            await asyncio.sleep(8)  # count-in bar + a couple seconds of song
            state = await cdp.js("Guitarz.conductor.state.api.playerState")
            pos = await cdp.js("document.getElementById('pos-time').textContent")
            assert state == 1, f"player not in Playing state: {state}"
            assert not pos.startswith("0:00 /"), f"position never advanced: {pos}"

    asyncio.run(scenario())


async def _center(cdp, selector: str) -> dict:
    """Return the viewport center of the first element matching ``selector``.

    Args:
        cdp: driver attached to the page.
        selector: CSS selector of the element.

    Returns:
        ``{"x": float, "y": float}`` in CSS pixels.
    """
    return json.loads(await cdp.js(f"""(() => {{
        const r = document.querySelector({json.dumps(selector)}).getBoundingClientRect();
        return JSON.stringify({{x: r.x + r.width / 2, y: r.y + r.height / 2}});
    }})()"""))


async def _mouse(cdp, kind: str, x: float, y: float, **extra) -> None:
    """Send one real mouse event through Chrome's input pipeline.

    Real input (not synthetic DOM events) is what pointer capture, hover and
    double-click detection respond to, so layout tests use it.

    Args:
        cdp: driver attached to the page.
        kind: CDP type — ``mouseMoved``, ``mousePressed`` or ``mouseReleased``.
        x: viewport x in CSS pixels.
        y: viewport y in CSS pixels.
        **extra: extra CDP fields (``button``, ``clickCount``, ``buttons``).
    """
    await cdp.send("Input.dispatchMouseEvent", {"type": kind, "x": x, "y": y, **extra})


async def _sidebar_layout(cdp) -> dict:
    """Snapshot the left column's state and the room left for the score.

    Returns:
        Dict with sidebar visibility/width/position, rail visibility, the
        collapsed/peek/pinned flags, the viewport width, and the width of
        the rendered score (to prove alphaTab re-laid out, not just shifted).
    """
    return json.loads(await cdp.js("""(() => {
        const main = document.getElementById('main');
        const side = document.getElementById('sidebar');
        const rail = document.getElementById('sidebar-rail');
        const svgs = [...document.querySelectorAll('#alphatab svg')];
        return JSON.stringify({
            sidebarShown: side.offsetWidth > 0,
            sidebarWidth: side.getBoundingClientRect().width,
            sidebarFloats: getComputedStyle(side).position === 'absolute',
            railShown: rail.offsetWidth > 0,
            collapsed: main.classList.contains('sidebar-collapsed'),
            peek: main.classList.contains('sidebar-peek'),
            pinned: document.getElementById('btn-sidebar-pin').classList.contains('on'),
            viewport: document.getElementById('viewport').getBoundingClientRect().width,
            scoreWidth: svgs.length
                ? Math.max(...svgs.map(s => s.getBoundingClientRect().right))
                  - Math.min(...svgs.map(s => s.getBoundingClientRect().left))
                : 0,
        });
    })()"""))


@pytest.mark.parametrize("server_fixture", ["live_server", "static_server"])
def test_sidebar_collapse_expand_and_persist(request, server_fixture):
    """The left column collapses to a rail for more tab room, and comes back (#9).

    Spec, in both the server app and the static site: (1) by default the
    column is open and pinned; (2) the collapse button hides it behind a thin
    rail, the score viewport gains the column's width, and alphaTab re-lays
    the score out wider; (3) the rail's expand button restores the column
    and the original viewport width; (4) collapsed and pinned choices are
    saved and survive a reload.
    """
    base = request.getfixturevalue(server_fixture)

    async def scenario():
        async with chrome_page(_song_url(base)) as cdp:
            await wait_ready(cdp)
            start = await _sidebar_layout(cdp)
            assert start["sidebarShown"] and not start["railShown"], start
            assert not start["collapsed"] and start["pinned"], start

            await cdp.js("document.getElementById('btn-sidebar-collapse').click()")
            await asyncio.sleep(3)  # alphaTab re-layout
            shut = await _sidebar_layout(cdp)
            assert shut["collapsed"] and not shut["sidebarShown"] and shut["railShown"], shut
            assert shut["viewport"] > start["viewport"] + 200, (start, shut)
            assert shut["scoreWidth"] > start["scoreWidth"] + 150, (start, shut)

            await cdp.js("document.getElementById('btn-sidebar-expand').click()")
            await asyncio.sleep(1)
            back = await _sidebar_layout(cdp)
            assert back["sidebarShown"] and not back["collapsed"], back
            assert abs(back["viewport"] - start["viewport"]) < 2, (start, back)

            await cdp.js("document.getElementById('btn-sidebar-pin').click()")
            await cdp.js("document.getElementById('btn-sidebar-collapse').click()")
            await cdp.send("Page.reload")
            await wait_ready(cdp)
            again = await _sidebar_layout(cdp)
            assert again["collapsed"] and not again["pinned"], again

    asyncio.run(scenario())


def test_sidebar_unpinned_hides_on_play_and_peeks_on_hover(live_server):
    """Unpinned, the column gets out of the way when the song starts (#9).

    Spec: (1) pinned (default), pressing play leaves the column open;
    (2) unpinned, pressing play collapses it; (3) hovering the rail slides
    the column out *over* the score — it floats, so the viewport width does
    not change and the score does not re-lay out under the pointer — and it
    sits above alphaTab's cursor, which otherwise draws over it; (4) moving
    the pointer back onto the score hides it again.
    """

    async def scenario():
        async with chrome_page(_song_url(live_server)) as cdp:
            await wait_ready(cdp)
            await cdp.js("document.getElementById('btn-play').click()", gesture=True)
            await asyncio.sleep(1.5)
            assert not (await _sidebar_layout(cdp))["collapsed"], "pinned must stay open"
            await cdp.js("document.getElementById('btn-stop').click()")

            await cdp.js("document.getElementById('btn-sidebar-pin').click()")
            await cdp.js("document.getElementById('btn-play').click()", gesture=True)
            await asyncio.sleep(1.5)
            playing = await _sidebar_layout(cdp)
            assert playing["collapsed"] and playing["railShown"], playing

            rail = await _center(cdp, "#sidebar-rail")
            await _mouse(cdp, "mouseMoved", rail["x"], rail["y"])
            await asyncio.sleep(0.5)
            peek = await _sidebar_layout(cdp)
            assert peek["peek"] and peek["sidebarShown"] and peek["sidebarFloats"], peek
            assert abs(peek["viewport"] - playing["viewport"]) < 2, (playing, peek)
            # alphaTab's cursor uses a high z-index; the score area must form its
            # own stacking context or the cursor draws on top of the peek column.
            assert await cdp.js("getComputedStyle(document.getElementById('stage')).isolation") == "isolate"

            await _mouse(cdp, "mouseMoved", playing["viewport"] * 0.8 + 300, 400)
            await asyncio.sleep(0.5)
            gone = await _sidebar_layout(cdp)
            assert not gone["peek"] and not gone["sidebarShown"], gone
            await cdp.js("document.getElementById('btn-stop').click()")

    asyncio.run(scenario())


def test_sidebar_resize_handle(live_server):
    """A drag handle on the column's edge resizes it, clamped and saved (#10).

    Spec, with real mouse and key input: (1) the handle is a focusable
    ``separator`` reporting the width in ``aria-valuenow``; (2) dragging it
    150px right widens the column by 150px; (3) widths clamp to 200px and to
    half the window; (4) arrow keys on the focused handle change the width in
    16px steps; (5) collapse and expand keep the chosen width; (6) the width
    survives a reload; (7) a double-click resets the default 290px.
    """

    async def width(cdp):
        """Read the column width and the handle's reported value."""
        return json.loads(await cdp.js("""JSON.stringify({
            w: Math.round(document.getElementById('sidebar').getBoundingClientRect().width),
            aria: Number(document.getElementById('sidebar-handle').getAttribute('aria-valuenow')),
            role: document.getElementById('sidebar-handle').getAttribute('role')})"""))

    async def drag_to(cdp, x):
        """Drag the handle from its center to viewport x."""
        h = await _center(cdp, "#sidebar-handle")
        await _mouse(cdp, "mouseMoved", h["x"], h["y"])
        await _mouse(cdp, "mousePressed", h["x"], h["y"], button="left", clickCount=1, buttons=1)
        for step in range(1, 6):
            await _mouse(cdp, "mouseMoved", h["x"] + (x - h["x"]) * step / 5, h["y"],
                         button="left", buttons=1)
        await _mouse(cdp, "mouseReleased", x, h["y"], button="left", clickCount=1)
        await asyncio.sleep(0.3)

    async def key(cdp, code, vk):
        """Press one key on the focused element."""
        for kind in ("rawKeyDown", "keyUp"):
            await cdp.send("Input.dispatchKeyEvent", {
                "type": kind, "code": code, "key": code, "windowsVirtualKeyCode": vk})

    async def scenario():
        async with chrome_page(_song_url(live_server)) as cdp:
            await wait_ready(cdp)
            start = await width(cdp)
            assert start == {"w": 290, "aria": 290, "role": "separator"}, start
            window_w = await cdp.js("window.innerWidth")

            h = await _center(cdp, "#sidebar-handle")
            await drag_to(cdp, h["x"] + 150)
            assert (await width(cdp))["w"] == 440

            await drag_to(cdp, window_w - 10)
            assert (await width(cdp))["w"] == window_w // 2
            await drag_to(cdp, 20)
            assert (await width(cdp))["w"] == 200

            await cdp.js("document.getElementById('sidebar-handle').focus()")
            await key(cdp, "ArrowRight", 39)
            await key(cdp, "ArrowRight", 39)
            assert await width(cdp) == {"w": 232, "aria": 232, "role": "separator"}

            await cdp.js("document.getElementById('btn-sidebar-collapse').click()")
            await cdp.js("document.getElementById('btn-sidebar-expand').click()")
            assert (await width(cdp))["w"] == 232

            await cdp.send("Page.reload")
            await wait_ready(cdp)
            assert (await width(cdp))["w"] == 232

            h = await _center(cdp, "#sidebar-handle")
            await _mouse(cdp, "mouseMoved", h["x"], h["y"])
            for count in (1, 2):
                await _mouse(cdp, "mousePressed", h["x"], h["y"], button="left", clickCount=count)
                await _mouse(cdp, "mouseReleased", h["x"], h["y"], button="left", clickCount=count)
            await asyncio.sleep(0.3)
            assert (await width(cdp))["w"] == 290

    asyncio.run(scenario())


def test_space_bar_plays_after_mouse_click_but_presses_keyboard_focused_buttons(live_server):
    """Space plays/pauses after a mouse click; it presses a button reached by keyboard.

    Regression: making Space press any focused button (so keyboard users can
    operate buttons) broke the everyday flow of clicking a toggle such as
    ``loop`` with the mouse and then pressing Space to play — Space toggled
    the button off again instead. The app tells the two cases apart by the
    last way the user moved around (Tab vs. a pointer press; Chrome's
    ``:focus-visible`` turns true on the key press itself, so it cannot).
    Spec, driven with real CDP mouse and key input: (1) mouse-click
    ``loop`` (it turns on), press Space — play/pause fires and ``loop`` stays
    on; (2) press Tab to move keyboard focus to the next button (``✕ loop``),
    press Space — that button is pressed and play/pause does not fire;
    (3) with a dialog open, Space never plays.
    """

    async def key(cdp, code, key_name, vk):
        """Send one real key press (down + up) through Chrome's input pipeline."""
        for kind in ("rawKeyDown", "keyUp"):
            await cdp.send("Input.dispatchKeyEvent", {
                "type": kind, "code": code, "key": key_name,
                "windowsVirtualKeyCode": vk, "nativeVirtualKeyCode": vk,
            })
        if code == "Space":
            await cdp.send("Input.dispatchKeyEvent", {"type": "char", "text": " "})

    async def mouse_click(cdp, element_id):
        """Click an element's center with a real mouse press and release."""
        box = json.loads(await cdp.js(f"""(() => {{
            const r = document.getElementById('{element_id}').getBoundingClientRect();
            return JSON.stringify({{x: r.x + r.width / 2, y: r.y + r.height / 2}});
        }})()"""))
        for kind in ("mousePressed", "mouseReleased"):
            await cdp.send("Input.dispatchMouseEvent", {
                "type": kind, "x": box["x"], "y": box["y"], "button": "left", "clickCount": 1,
            })

    async def scenario():
        async with chrome_page(_song_url(live_server)) as cdp:
            await wait_ready(cdp)
            await cdp.js("""(() => {
                const api = Guitarz.conductor.state.api;
                window.__playPause = 0;
                api.playPause = () => { window.__playPause++; };
                window.__clearLoop = 0;
                document.getElementById('btn-clearloop')
                    .addEventListener('click', () => { window.__clearLoop++; });
            })()""")

            async def snap():
                """Read the counters, loop state and the focused element."""
                return json.loads(await cdp.js("""JSON.stringify({
                    playPause: window.__playPause, clearLoop: window.__clearLoop,
                    loopOn: document.getElementById('btn-loop').classList.contains('on'),
                    focused: document.activeElement.id})"""))

            await mouse_click(cdp, "btn-loop")
            await key(cdp, "Space", " ", 32)
            after_mouse = await snap()
            assert after_mouse == {"playPause": 1, "clearLoop": 0, "loopOn": True,
                                   "focused": "btn-loop"}, after_mouse

            await key(cdp, "Tab", "Tab", 9)
            await key(cdp, "Space", " ", 32)
            after_keyboard = await snap()
            assert after_keyboard == {"playPause": 1, "clearLoop": 1, "loopOn": True,
                                      "focused": "btn-clearloop"}, after_keyboard

            await mouse_click(cdp, "btn-info")
            await key(cdp, "Space", " ", 32)
            assert (await snap())["playPause"] == 1, "Space must not play while a dialog is open"

    asyncio.run(scenario())


@pytest.mark.skipif(
    COLLECTION_GP7 is None, reason="no Guitar Pro 7 song in the private collection (dev machine only)"
)
def test_real_guitar_pro_7_song_loads_and_plays(live_server):
    """A real Guitar Pro 7 song from the collection renders and plays.

    The committed samples are all Guitar Pro 5, so without this test nothing
    would exercise alphaTab's GP7 (zip) reader that every real ``.gp`` song
    goes through. Spec: the song renders with one rail row per track (and
    at least one track), the player becomes ready, and playback really
    advances past the count-in. Runs only where the private collection is
    present; the file is copied to a temp folder and never committed.
    """

    async def scenario():
        url = f"{live_server}/?open={urllib.parse.quote('collection-gp7.gp')}"
        async with chrome_page(url) as cdp:
            status = await wait_ready(cdp)
            tracks = await cdp.js("Guitarz.conductor.state.score.tracks.length")
            assert tracks > 0 and status["rows"] == tracks, (status, tracks)
            assert status["playEnabled"], f"player never became ready: {status}"

            await cdp.js("document.getElementById('btn-play').click()", gesture=True)
            await asyncio.sleep(8)  # count-in bar + a couple seconds of song
            state = await cdp.js("Guitarz.conductor.state.api.playerState")
            pos = await cdp.js("document.getElementById('pos-time').textContent")
            assert state == 1, f"player not in Playing state: {state}"
            assert not pos.startswith("0:00 /"), f"position never advanced: {pos}"

    asyncio.run(scenario())


def test_string_tracks_are_color_coded(live_server):
    """Every string-instrument track gets one distinct color in rail AND score.

    The point of the feature is visual correspondence: the chip next to the
    track name in the rail must let the eye jump to the matching tinted staff.
    So the spec is (1) exactly the string tracks (tuned staves, not drums)
    carry a chip, (2) all chip colors are distinct, and (3) the score model
    carries a per-track style whose color equals that track's chip color —
    alphaTab renders staves from exactly these style objects.
    """

    async def scenario():
        async with chrome_page(_song_url(live_server)) as cdp:
            await wait_ready(cdp)
            raw = await cdp.js("""(() => {
                const tracks = Guitarz.conductor.state.score.tracks;
                const stringTracks = tracks.filter(
                    t => t.staves.some(s => !s.isPercussion && s.tuning.length > 0));
                const rows = [...document.querySelectorAll('.track-row')];
                const chips = rows.map(r => {
                    const c = r.querySelector('.chip');
                    return c ? c.style.backgroundColor : null;
                });
                const styleColors = tracks.map(t => {
                    if (!t.style || t.style.colors.size === 0) return null;
                    const col = [...t.style.colors.values()].find(c => c);
                    return col ? col.rgba : null;
                });
                return JSON.stringify({
                    total: tracks.length,
                    stringCount: stringTracks.length,
                    chips, styleColors});
            })()""")
            data = json.loads(raw)

            chips = [c for c in data["chips"] if c]
            assert len(chips) == data["stringCount"], (
                f"every string track (and only those) needs a chip: {data}"
            )
            assert len(set(chips)) == len(chips), (
                f"chip colors must be distinct: {data}"
            )

            styled = [c for c in data["styleColors"] if c]
            assert len(styled) == data["stringCount"], (
                f"every string track needs a score-side style color: {data}"
            )

    asyncio.run(scenario())


def test_tab_tails_hidden_by_default_with_toggle(live_server):
    """Rhythm tails under tab staves default OFF and toggle back on.

    The user tracks position by the cursor and fret numbers; stems/beams under
    the tab add clutter, so ``notation.rhythmMode`` must start at ``Hidden``
    (0). Clicking the transport's tails button must flip it to
    ``ShowWithBeams`` (1) and trigger a re-render that completes (overlay
    hidden again, SVG still present); a second click restores Hidden.
    """

    async def scenario():
        async with chrome_page(_song_url(live_server)) as cdp:
            await wait_ready(cdp)

            mode = await cdp.js(
                "Guitarz.conductor.state.api.settings.notation.rhythmMode"
            )
            assert mode == 0, f"tails must default to TabRhythmMode.Hidden: {mode}"

            await cdp.js("document.getElementById('btn-tails').click()")
            await asyncio.sleep(4)  # re-render
            mode = await cdp.js(
                "Guitarz.conductor.state.api.settings.notation.rhythmMode"
            )
            assert mode == 1, f"toggle on must set ShowWithBeams: {mode}"
            status = await wait_ready(cdp)
            assert status["svg"] > 0, "score must re-render with tails shown"

            await cdp.js("document.getElementById('btn-tails').click()")
            await asyncio.sleep(4)
            mode = await cdp.js(
                "Guitarz.conductor.state.api.settings.notation.rhythmMode"
            )
            assert mode == 0, f"toggle off must restore Hidden: {mode}"

    asyncio.run(scenario())


def test_global_transpose_stepper(live_server):
    """The transpose stepper shifts playback pitch for pitched tracks only.

    Spec (audio-only, global): the transport stepper starts at +0; each click
    steps one semitone within a clamp of +/-12; clicking the readout resets to
    0; the pitch change is delivered via alphaTab's live
    ``changeTrackTranspositionPitch`` API to every pitched track and to no
    percussion track (transposing MIDI drums would remap sounds, not pitch);
    and a persisted non-zero transpose is automatically re-applied when a new
    score loads. Audio itself can't be asserted headlessly, so the contract is
    pinned at the API boundary with an in-page spy.
    """

    async def scenario():
        async with chrome_page(_song_url(live_server)) as cdp:
            await wait_ready(cdp)

            # Baseline: neutral stepper, neutral state.
            assert (
                await cdp.js("document.getElementById('transpose-val').textContent")
                == "+0"
            )
            assert await cdp.js("Guitarz.conductor.state.transpose") == 0

            # Spy on the alphaTab call so semitones + track choice are observable.
            await cdp.js("""(() => {
                const api = Guitarz.conductor.state.api;
                window.__transposeCalls = [];
                const orig = api.changeTrackTranspositionPitch.bind(api);
                api.changeTrackTranspositionPitch = (tracks, semis) => {
                    window.__transposeCalls.push({idx: tracks.map(t => t.index), semis});
                    return orig(tracks, semis);
                };
            })()""")

            # Two clicks up -> +2, applied to exactly the pitched tracks.
            await cdp.js("document.getElementById('btn-transpose-up').click()")
            await cdp.js("document.getElementById('btn-transpose-up').click()")
            raw = await cdp.js("""JSON.stringify({
                label: document.getElementById('transpose-val').textContent,
                state: Guitarz.conductor.state.transpose,
                calls: window.__transposeCalls,
                pitched: Guitarz.conductor.state.score.tracks
                    .filter(t => t.staves.some(s => !s.isPercussion && s.tuning.length > 0))
                    .map(t => t.index),
                drums: Guitarz.conductor.state.score.tracks
                    .filter(t => t.staves.every(s => s.isPercussion))
                    .map(t => t.index)})""")
            data = json.loads(raw)
            assert data["label"] == "+2", f"readout should show +2: {data}"
            assert data["state"] == 2, f"state should be 2: {data}"
            last = data["calls"][-1]
            assert last["semis"] == 2, f"last API call must carry 2 semitones: {data}"
            assert sorted(last["idx"]) == sorted(data["pitched"]), (
                f"transpose must hit exactly the pitched tracks: {data}"
            )
            assert not set(last["idx"]) & set(data["drums"]), (
                f"drums must be excluded: {data}"
            )

            # Readout click resets to 0.
            await cdp.js("document.getElementById('transpose-val').click()")
            assert (
                await cdp.js("document.getElementById('transpose-val').textContent")
                == "+0"
            )
            assert await cdp.js("window.__transposeCalls.at(-1).semis") == 0

            # Clamp: 13 clicks down stop at -12.
            await cdp.js("""(() => {
                const btn = document.getElementById('btn-transpose-down');
                for (let i = 0; i < 13; i++) btn.click();
            })()""")
            assert (
                await cdp.js("document.getElementById('transpose-val').textContent")
                == "−12"
            )
            assert await cdp.js("Guitarz.conductor.state.transpose") == -12

            # Persistence: with auto-from-tuning off, reload the song; the -12
            # must be re-applied automatically. (With auto on, each song sets
            # its own value instead — see test_auto_transpose_from_tuning.)
            await cdp.js("document.getElementById('btn-transpose-auto').click()")
            assert await cdp.js("Guitarz.conductor.state.autoTranspose") is False
            await cdp.js("window.__transposeCalls = []")
            await cdp.js(f"""Guitarz.conductor.loadFile({{
                path: {json.dumps(SAMPLE_GP.name)}, name: {json.dumps(SAMPLE_GP.name)}}})""")
            await wait_ready(cdp)
            reapplied = await cdp.js(
                "window.__transposeCalls.some(c => c.semis === -12)"
            )
            assert reapplied, "transpose must be re-applied after loading a score"

    asyncio.run(scenario())


def test_soundfont_switch_unlocks_only_after_the_real_load(live_server):
    """Over a slow network, play is not unlocked until the new font has arrived.

    Regression, seen on GitHub Pages with the 38 MB MuseScore General: the
    switch called alphaTab's ``resetSoundFonts()``, which itself fires
    ``soundFontLoaded`` at once. The menu took that as "done", unlocked play,
    and the real load landing seconds later stopped the song again. Locally
    the font comes off disk instantly, so the two events coincided and no
    test noticed. Spec, with the download throttled to ~0.5 MB/s so Sonivox
    (1.3 MB) takes a few seconds: (1) the play button stays disabled for at
    least a second after the switch starts; (2) once it unlocks and play is
    pressed, playback keeps going — it is still Playing three seconds later
    and was never stopped in between.
    """

    async def scenario():
        async with chrome_page(_song_url(live_server)) as cdp:
            await wait_ready(cdp)
            await cdp.send("Network.enable")
            await cdp.send("Network.emulateNetworkConditions", {
                "offline": False, "latency": 50,
                "downloadThroughput": 500 * 1024, "uploadThroughput": 500 * 1024,
            })
            await cdp.js("""(() => {
                const api = Guitarz.conductor.state.api;
                window.__stops = 0;
                api.playerStateChanged.on(e => { if (window.__armed && e.state !== 1) window.__stops++; });
                window.__t0 = performance.now();
                const sel = document.getElementById('soundfont');
                sel.value = 'sonivox.sf2';
                sel.dispatchEvent(new Event('change'));
            })()""")
            unlocked_after = None
            for _ in range(120):
                await asyncio.sleep(0.25)
                if await cdp.js("!document.getElementById('btn-play').disabled"):
                    unlocked_after = await cdp.js("(performance.now() - window.__t0) / 1000")
                    break
            assert unlocked_after is not None, "play never unlocked after the switch"
            assert unlocked_after >= 1.0, f"play unlocked after {unlocked_after:.2f}s, before the font arrived"

            await cdp.js("window.__armed = true; document.getElementById('btn-play').click()", gesture=True)
            await asyncio.sleep(3)
            state = await cdp.js("Guitarz.conductor.state.api.playerState")
            stops = await cdp.js("window.__stops")
            await cdp.js("document.getElementById('btn-stop').click()")
            assert state == 1 and stops == 0, f"playback was interrupted: state={state} stops={stops}"

    asyncio.run(scenario())


def test_soundfont_menu_switches_and_persists(live_server):
    """The transport's SoundFont menu mirrors the catalog, switches live, and persists.

    Spec: (1) the menu's options are exactly the ``/api/soundfonts`` catalog,
    in order; (2) a fresh browser profile starts on the server's default font
    and that font's URL is what alphaTab was configured with; (3) choosing
    another font reloads the synth in place — ``soundFontLoaded`` fires, play
    re-enables, and playback really advances; (4) the choice is saved and
    survives a page reload. The switch targets ``sonivox.sf2`` because
    fetch_vendor.sh always installs it, so the test holds on machines that
    have not downloaded the large optional fonts.
    """

    async def scenario():
        async with chrome_page(_song_url(live_server)) as cdp:
            await wait_ready(cdp)
            raw = await cdp.js("""(async () => {
                const cat = await (await fetch('/api/soundfonts')).json();
                const sel = document.getElementById('soundfont');
                return JSON.stringify({
                    cat,
                    options: [...sel.options].map(o => o.value),
                    selected: sel.value,
                    configured: Guitarz.conductor.state.api.settings.player.soundFont});
            })()""")
            data = json.loads(raw)
            cat = data["cat"]
            assert data["options"] == [f["file"] for f in cat["fonts"]], (
                f"menu must mirror the catalog: {data}"
            )
            assert "sonivox.sf2" in data["options"], f"sonivox must be offered: {data}"
            assert data["selected"] == cat["default"], (
                f"fresh profile must start on the server default: {data}"
            )
            default_url = next(f["url"] for f in cat["fonts"] if f["file"] == cat["default"])
            # alphaTab gets the catalog URL resolved against the page (siteUrl).
            default_url = await cdp.js(f"new URL({json.dumps(default_url)}, document.baseURI).href")
            assert data["configured"] == default_url, (
                f"alphaTab must be configured with the default font: {data}"
            )

            # Switch to sonivox and wait for the synth to finish reloading.
            await cdp.js("""(() => {
                window.__sfLoaded = false;
                Guitarz.conductor.state.api.soundFontLoaded.on(() => { window.__sfLoaded = true; });
                const sel = document.getElementById('soundfont');
                sel.value = 'sonivox.sf2';
                sel.dispatchEvent(new Event('change'));
            })()""")
            ready = None
            for _ in range(40):
                await asyncio.sleep(1.5)
                ready = json.loads(await cdp.js("""JSON.stringify({
                    loaded: window.__sfLoaded,
                    playEnabled: !document.getElementById('btn-play').disabled,
                    menuEnabled: !document.getElementById('soundfont').disabled})"""))
                if all(ready.values()):
                    break
            assert ready and all(ready.values()), f"font switch never completed: {ready}"
            saved = await cdp.js("localStorage.getItem('guitarz.soundfont')")
            assert saved == "sonivox.sf2", f"choice must be saved: {saved}"

            await cdp.js("document.getElementById('btn-play').click()", gesture=True)
            await asyncio.sleep(8)  # count-in bar + a couple seconds of song
            state = await cdp.js("Guitarz.conductor.state.api.playerState")
            pos = await cdp.js("document.getElementById('pos-time').textContent")
            assert state == 1, f"player not Playing after font switch: {state}"
            assert not pos.startswith("0:00 /"), f"position never advanced: {pos}"
            await cdp.js("document.getElementById('btn-stop').click()")

            # Reload: the saved choice wins over the server default.
            await cdp.send("Page.reload")
            await wait_ready(cdp)
            after = json.loads(await cdp.js("""JSON.stringify({
                selected: document.getElementById('soundfont').value,
                configured: Guitarz.conductor.state.api.settings.player.soundFont})"""))
            assert after["selected"] == "sonivox.sf2", f"choice must persist: {after}"
            assert after["configured"].endswith("/sonivox.sf2"), (
                f"alphaTab must boot with the saved font: {after}"
            )

    asyncio.run(scenario())


def test_fretboard_geometry(live_server):
    """The fretboard's pure geometry helpers follow real guitar math.

    Pinned without loading a song (fast): (1) fret positions follow the
    equal-temperament rule ``scale * (1 - 2^(-n/12))`` — fret 12 sits at half
    the scale length and fret 24 at three quarters; (2) inlay markers sit on
    frets 3/5/7/9/15/17/19/21 with double markers on 12 and 24; (3) alphaTab
    numbers strings from the lowest (string 1), while the drawing puts the
    highest string on top, so ``stringRow`` must flip the order; (4) MIDI
    numbers are labeled in scientific pitch (64 = E4); (5) the theme catalog
    offers the decorative inlay styles the feature exists for.
    """

    async def scenario():
        async with chrome_page(live_server + "/") as cdp:
            for _ in range(40):
                if await cdp.js("!!(window.Guitarz && Guitarz.fretboard)"):
                    break
                await asyncio.sleep(0.25)
            raw = await cdp.js("""(() => {
                const fb = Guitarz.fretboard;
                return JSON.stringify({
                    f0: fb.fretPosition(0, 1000),
                    f12: fb.fretPosition(12, 1000),
                    f24: fb.fretPosition(24, 1000),
                    inlays: fb.inlayFrets(24),
                    inlays15: fb.inlayFrets(15),
                    rows: [1, 2, 3, 4, 5, 6].map(s => fb.stringRow(s, 6)),
                    names: [40, 64, 61, 45].map(fb.noteName),
                    inlayStyles: Object.values(fb.THEMES).map(t => t.inlay)});
            })()""")
            data = json.loads(raw)
            assert data["f0"] == 0
            assert abs(data["f12"] - 500) < 1e-9, data
            assert abs(data["f24"] - 750) < 1e-9, data
            assert data["inlays"] == [
                {"fret": f, "double": f in (12, 24)}
                for f in (3, 5, 7, 9, 12, 15, 17, 19, 21, 24)
            ], data
            assert [i["fret"] for i in data["inlays15"]] == [3, 5, 7, 9, 12, 15], data
            assert data["rows"] == [5, 4, 3, 2, 1, 0], data
            assert data["names"] == ["E2", "E4", "C#4", "A2"], data
            for style in ("dots", "diamonds", "blocks", "tree-of-life"):
                assert style in data["inlayStyles"], data

    asyncio.run(scenario())


def test_fretboard_pane_toggles_and_follows_playback(live_server):
    """The fretboard pane hides by default, toggles, follows play, and persists.

    Spec: (1) the pane starts hidden and the transport's ``fretboard`` button
    is unlit; (2) clicking it shows the pane with one SVG string line per
    string of the selected track, inlay markers, and a track menu listing
    exactly the string tracks (first one selected); (3) the row mapping is
    pinned against alphaTab's own pitch model — tuning of the drawn row plus
    fret equals ``note.realValue`` — so dots can never land on the mirrored
    string; (4) during playback, note dots appear for the beats being played;
    (5) switching the theme re-skins the board (``data-theme`` attribute and
    diamond inlays as polygons); (6) the show/hide and theme choices survive
    a page reload; (7) a second click hides the pane again.
    """

    async def scenario():
        async with chrome_page(_song_url(live_server)) as cdp:
            await wait_ready(cdp)
            start = json.loads(await cdp.js("""JSON.stringify({
                hidden: document.getElementById('fretpane').hidden,
                lit: document.getElementById('btn-fretboard').classList.contains('on')})"""))
            assert start == {"hidden": True, "lit": False}, start

            await cdp.js("document.getElementById('btn-fretboard').click()")
            await asyncio.sleep(0.5)
            raw = await cdp.js("""(() => {
                const tracks = Guitarz.conductor.state.score.tracks;
                const stringTracks = tracks.filter(
                    t => t.staves.some(s => !s.isPercussion && s.tuning.length > 0));
                const sel = document.getElementById('fret-track');
                const track = tracks[Number(sel.value)];
                const staff = track.staves.find(s => !s.isPercussion && s.tuning.length > 0);
                let match = 0, total = 0;
                for (const bar of staff.bars) for (const v of bar.voices)
                    for (const b of v.beats) for (const n of b.notes) {
                        if (n.isDead || n.harmonicType) continue;
                        total++;
                        const row = Guitarz.fretboard.stringRow(n.string, staff.tuning.length);
                        if (staff.tuning[row] + n.fret === n.realValue) match++;
                    }
                return JSON.stringify({
                    hidden: document.getElementById('fretpane').hidden,
                    lit: document.getElementById('btn-fretboard').classList.contains('on'),
                    strings: document.querySelectorAll('#fretboard .fb-string').length,
                    tuningLen: staff.tuning.length,
                    inlays: document.querySelectorAll('#fretboard .fb-inlay').length,
                    options: [...sel.options].map(o => Number(o.value)),
                    stringIdx: stringTracks.map(t => t.index),
                    selected: Number(sel.value),
                    match, total});
            })()""")
            shown = json.loads(raw)
            assert shown["hidden"] is False and shown["lit"] is True, shown
            assert shown["strings"] == shown["tuningLen"], shown
            assert shown["inlays"] > 0, shown
            assert shown["options"] == shown["stringIdx"], shown
            assert shown["selected"] == shown["stringIdx"][0], shown
            assert shown["total"] > 0 and shown["match"] / shown["total"] > 0.95, shown

            # Playback: dots must appear on the board for the played beats.
            await cdp.js("document.getElementById('btn-play').click()", gesture=True)
            seen = 0
            for _ in range(40):
                await asyncio.sleep(0.5)
                seen = max(seen, await cdp.js(
                    "document.querySelectorAll('#fretboard .fb-note').length"))
                if seen:
                    break
            await cdp.js("document.getElementById('btn-stop').click()")
            assert seen > 0, "no note dots appeared during playback"

            # Theme switch re-skins the board.
            await cdp.js("""(() => {
                const sel = document.getElementById('fret-theme');
                sel.value = 'ebony-diamonds';
                sel.dispatchEvent(new Event('change'));
            })()""")
            themed = json.loads(await cdp.js("""JSON.stringify({
                theme: document.querySelector('#fretboard svg').dataset.theme,
                polys: document.querySelectorAll('#fretboard polygon.fb-inlay').length})"""))
            assert themed["theme"] == "ebony-diamonds" and themed["polys"] > 0, themed

            # Choices persist across a reload.
            await cdp.send("Page.reload")
            await wait_ready(cdp)
            after = json.loads(await cdp.js("""JSON.stringify({
                hidden: document.getElementById('fretpane').hidden,
                theme: document.getElementById('fret-theme').value})"""))
            assert after == {"hidden": False, "theme": "ebony-diamonds"}, after

            await cdp.js("document.getElementById('btn-fretboard').click()")
            assert await cdp.js("document.getElementById('fretpane').hidden") is True

    asyncio.run(scenario())


def test_fretboard_jem_theme_has_24_frets_and_white_vine(live_server):
    """The JEM-style theme draws a 24-fret neck with a white vine inlay.

    Modeled on the user's own guitar: rosewood board, white tree-of-life
    vine, 24 frets. Spec: (1) a default theme on this song draws the
    minimum 22 frets (the song never climbs higher), recorded on the SVG as
    ``data-frets``; (2) the ``rosewood-jem`` theme forces 24 frets even
    though the song does not need them — the neck should look like the
    instrument; (3) its inlay is drawn white; and (4) the vine runs the full
    neck, so some leaf sits past fret 22.
    """

    async def scenario():
        async with chrome_page(_song_url(live_server)) as cdp:
            await wait_ready(cdp)
            await cdp.js("""(() => {
                localStorage.removeItem('guitarz.fretTheme');
                const sel = document.getElementById('fret-theme');
                sel.value = 'rosewood-dots';
                sel.dispatchEvent(new Event('change'));
                if (document.getElementById('fretpane').hidden)
                    document.getElementById('btn-fretboard').click();
            })()""")
            base = await cdp.js("document.querySelector('#fretboard svg').dataset.frets")
            assert base == "22", base

            await cdp.js("""(() => {
                const sel = document.getElementById('fret-theme');
                sel.value = 'rosewood-jem';
                sel.dispatchEvent(new Event('change'));
            })()""")
            raw = await cdp.js("""(() => {
                const svg = document.querySelector('#fretboard svg');
                const leaves = [...svg.querySelectorAll('.fb-inlay')];
                const fret22 = [...svg.querySelectorAll('.fb-frets line')][21];
                return JSON.stringify({
                    frets: svg.dataset.frets,
                    fills: [...new Set(leaves.map(l => l.getAttribute('fill').toLowerCase()))],
                    leaves: leaves.length,
                    pastFret22: leaves.some(l => {
                        const b = l.getBBox();
                        return b.x > Number(fret22.getAttribute('x1'));
                    })});
            })()""")
            data = json.loads(raw)
            assert data["frets"] == "24", data
            assert data["leaves"] > 0, data
            assert data["fills"] == ["#ffffff"] or data["fills"] == ["#fff"], data
            assert data["pastFret22"], data

    asyncio.run(scenario())


def test_fretboard_grey_strings_and_quiet_inlays_on_dark_boards(live_server):
    """Every look uses Maple's grey strings and frets; dark boards dim inlays.

    Musician feedback: the neutral greys of the Maple · blocks look read best
    for strings and frets on every board, and on dark woods the bright pearl
    dots / diamonds / vine compete with the note numbers and names. Spec,
    checked on the rendered SVG of every theme: (1) fret wires stroke
    ``#8c8c8c`` and strings ``#5e5e5e``; (2) on a dark board (wood luminance
    below 0.25) every inlay shape — dots, diamonds, vine stem and leaves —
    shows at an effective opacity of at most 0.4; (3) on the light Maple
    board the dark block inlays stay clearly visible (at least 0.8).
    """

    async def scenario():
        async with chrome_page(_song_url(live_server)) as cdp:
            await wait_ready(cdp)
            raw = await cdp.js("""(() => {
                if (document.getElementById('fretpane').hidden)
                    document.getElementById('btn-fretboard').click();
                const lum = hex => {
                    const c = [1, 3, 5].map(i => parseInt(hex.slice(i, i + 2), 16) / 255)
                        .map(v => v <= 0.03928 ? v / 12.92 : ((v + 0.055) / 1.055) ** 2.4);
                    return 0.2126 * c[0] + 0.7152 * c[1] + 0.0722 * c[2];
                };
                const effective = el => {
                    let o = 1;
                    for (let n = el; n && n.tagName !== 'svg'; n = n.parentNode)
                        o *= Number(getComputedStyle(n).opacity);
                    return o;
                };
                const sel = document.getElementById('fret-theme');
                const out = {};
                for (const [id, theme] of Object.entries(Guitarz.fretboard.THEMES)) {
                    sel.value = id;
                    sel.dispatchEvent(new Event('change'));
                    const svg = document.querySelector('#fretboard svg');
                    const inlays = [...svg.querySelectorAll('.fb-inlay, .fb-inlay-vine')];
                    out[id] = {
                        dark: lum(theme.wood[0]) < 0.25,
                        frets: [...new Set([...svg.querySelectorAll('.fb-frets line')]
                            .map(l => l.getAttribute('stroke')))],
                        strings: [...new Set([...svg.querySelectorAll('.fb-string')]
                            .map(l => l.getAttribute('stroke')))],
                        inlays: inlays.length,
                        maxOpacity: Math.max(...inlays.map(effective)),
                        minOpacity: Math.min(...inlays.map(effective)),
                    };
                }
                return JSON.stringify(out);
            })()""")
            themes = json.loads(raw)
            assert any(t["dark"] for t in themes.values()), themes
            for name, t in themes.items():
                assert t["frets"] == ["#8c8c8c"], (name, t)
                assert t["strings"] == ["#5e5e5e"], (name, t)
                assert t["inlays"] > 0, (name, t)
                if t["dark"]:
                    assert t["maxOpacity"] <= 0.4, (name, t)
                else:
                    assert t["minOpacity"] >= 0.8, (name, t)

    asyncio.run(scenario())


def test_fretboard_previews_next_notes_faded(live_server):
    """The board shows the next notes to play as faded dots.

    So the fretting hand can move early, the board previews the next *new*
    attack on the selected track. Spec: (1) ``upcomingBeat(beat)`` returns
    the first later beat in the same voice — across bar lines — that has at
    least one note which is not a tie continuation (rests and held notes
    are skipped, since there is nothing new to fret); it is checked against
    an independent bar-by-bar flattening of the voice for every beat of the
    first 16 bars; (2) during playback, preview dots (``.fb-note.next``)
    appear and are drawn faded (opacity well below the live dots).
    """

    async def scenario():
        async with chrome_page(_song_url(live_server)) as cdp:
            await wait_ready(cdp)
            raw = await cdp.js("""(() => {
                const fb = Guitarz.fretboard;
                const track = Guitarz.conductor.state.score.tracks.find(
                    t => t.staves.some(s => !s.isPercussion && s.tuning.length > 0));
                const staff = track.staves.find(s => !s.isPercussion && s.tuning.length > 0);
                const flat = [];
                for (const bar of staff.bars) for (const b of bar.voices[0].beats) flat.push(b);
                const fresh = b => b.notes.some(n => !n.isTieDestination);
                let checked = 0, bad = [];
                for (let i = 0; i < flat.length && flat[i].voice.bar.index < 16; i++) {
                    let want = null;
                    for (let j = i + 1; j < flat.length; j++)
                        if (fresh(flat[j])) { want = flat[j]; break; }
                    const got = fb.upcomingBeat(flat[i]);
                    checked++;
                    if (got !== want) bad.push(i);
                }
                return JSON.stringify({checked, bad});
            })()""")
            data = json.loads(raw)
            assert data["checked"] > 16 and data["bad"] == [], data

            await cdp.js("""(() => {
                if (document.getElementById('fretpane').hidden)
                    document.getElementById('btn-fretboard').click();
            })()""")
            await cdp.js("document.getElementById('btn-play').click()", gesture=True)
            found = None
            for _ in range(40):
                await asyncio.sleep(0.5)
                found = json.loads(await cdp.js("""(() => {
                    const next = document.querySelector('#fretboard .fb-note.next');
                    const live = document.querySelector('#fretboard .fb-note:not(.next)');
                    if (!next) return 'null';
                    return JSON.stringify({
                        next: Number(getComputedStyle(next).opacity),
                        live: live ? Number(getComputedStyle(live).opacity) : 1});
                })()"""))
                if found:
                    break
            await cdp.js("document.getElementById('btn-stop').click()")
            assert found, "no faded preview dots appeared during playback"
            assert found["next"] < 0.6 and found["live"] == 1, found

    asyncio.run(scenario())


def test_fretboard_whole_bar_shape_toggle(live_server):
    """A "bar" toggle outlines every position the current bar uses.

    Seeing the whole bar at once shows the chord shape or scale box the
    hand should settle into. Spec: (1) ``barShape(bar)`` returns each
    distinct string/fret pair played in the bar, across all voices, sorted
    by string then fret — checked against an independent computation for
    the first 16 bars; (2) the ``#fret-bar`` toggle starts off and shows no
    shape dots; (3) switched on during playback, the board draws exactly
    one ``.fb-note.bar`` dot per position of the bar being played
    (``state.shownBar``); (4) the toggle persists across a reload.
    """

    async def scenario():
        async with chrome_page(_song_url(live_server)) as cdp:
            await wait_ready(cdp)
            raw = await cdp.js("""(() => {
                const fb = Guitarz.fretboard;
                const track = Guitarz.conductor.state.score.tracks.find(
                    t => t.staves.some(s => !s.isPercussion && s.tuning.length > 0));
                const staff = track.staves.find(s => !s.isPercussion && s.tuning.length > 0);
                const bad = [];
                for (const bar of staff.bars.slice(0, 16)) {
                    const seen = new Map();
                    for (const v of bar.voices) for (const b of v.beats) for (const n of b.notes)
                        seen.set(n.string + ':' + n.fret, {string: n.string, fret: n.fret});
                    const want = [...seen.values()].sort(
                        (a, c) => a.string - c.string || a.fret - c.fret);
                    if (JSON.stringify(fb.barShape(bar)) !== JSON.stringify(want))
                        bad.push(bar.index);
                }
                return JSON.stringify({bad});
            })()""")
            assert json.loads(raw)["bad"] == [], raw

            await cdp.js("""(() => {
                if (document.getElementById('fretpane').hidden)
                    document.getElementById('btn-fretboard').click();
            })()""")
            assert await cdp.js(
                "document.getElementById('fret-bar').classList.contains('on')") is False
            await cdp.js("document.getElementById('fret-bar').click()")
            await cdp.js("document.getElementById('btn-play').click()", gesture=True)
            got = None
            for _ in range(40):
                await asyncio.sleep(0.5)
                got = json.loads(await cdp.js("""(() => {
                    const fb = Guitarz.fretboard;
                    const i = fb.state.shownBar;
                    if (i === null || i === undefined) return 'null';
                    const track = Guitarz.conductor.state.score.tracks[fb.state.trackIndex];
                    const staff = track.staves.find(s => !s.isPercussion && s.tuning.length > 0);
                    const want = fb.barShape(staff.bars[i]).length;
                    const drawn = document.querySelectorAll('#fretboard .fb-note.bar').length;
                    return JSON.stringify({i, want, drawn});
                })()"""))
                if got and got["want"] > 0:
                    break
            await cdp.js("Guitarz.conductor.state.api.pause()")
            assert got and got["want"] > 0, got
            assert got["drawn"] == got["want"], got

            await cdp.send("Page.reload")
            await wait_ready(cdp)
            assert await cdp.js(
                "document.getElementById('fret-bar').classList.contains('on')") is True

    asyncio.run(scenario())


def test_fretboard_click_plays_note(live_server):
    """Clicking a spot on the neck plays that note with the track's sound.

    Spec: (1) the board has one click zone (``.fb-hit``) per string per
    fret, open strings included; (2) clicking the zone for string 6 (the
    highest, top row) fret 5 calls alphaTab's ``playNote`` with a note at
    that string/fret whose pitch is the top row's open tuning + 5 — the
    same mapping the dots use; (3) the click flashes a ``.fb-note.tap`` dot
    so the hand sees what it hit; (4) the throwaway note never enters the
    score (the borrowed voice keeps its beat count).
    """

    async def scenario():
        async with chrome_page(_song_url(live_server)) as cdp:
            await wait_ready(cdp)
            await cdp.js("""(() => {
                if (document.getElementById('fretpane').hidden)
                    document.getElementById('btn-fretboard').click();
                const api = Guitarz.conductor.state.api;
                window.__played = [];
                const orig = api.playNote.bind(api);
                api.playNote = n => {
                    window.__played.push({string: n.string, fret: n.fret, real: n.realValue});
                    return orig(n);
                };
            })()""")
            raw = await cdp.js("""(() => {
                const fb = Guitarz.fretboard;
                const track = Guitarz.conductor.state.score.tracks[fb.state.trackIndex];
                const staff = track.staves.find(s => !s.isPercussion && s.tuning.length > 0);
                const before = staff.bars.map(b => b.voices.map(v => v.beats.length).join()).join();
                const hits = document.querySelectorAll('#fretboard .fb-hit').length;
                const zone = document.querySelector(
                    '#fretboard .fb-hit[data-string="6"][data-fret="5"]');
                zone.dispatchEvent(new MouseEvent('click', {bubbles: true}));
                const after = staff.bars.map(b => b.voices.map(v => v.beats.length).join()).join();
                return JSON.stringify({
                    hits, want: (fb.state.geom.frets + 1) * staff.tuning.length,
                    played: window.__played, top: staff.tuning[0],
                    tap: document.querySelectorAll('#fretboard .fb-note.tap').length,
                    unchanged: before === after});
            })()""", gesture=True)
            data = json.loads(raw)
            assert data["hits"] == data["want"], data
            assert data["played"] == [
                {"string": 6, "fret": 5, "real": data["top"] + 5}
            ], data
            assert data["tap"] == 1, data
            assert data["unchanged"], data

    asyncio.run(scenario())


def test_fretboard_bass_neck_and_left_handed_mirror(live_server):
    """Bass tracks get their own neck; the lefty toggle mirrors the board.

    Spec: (1) choosing the Bass track draws one string line per bass
    string, labeled from the bass's own tuning (high string on top);
    (2) the ``#fret-lefty`` toggle starts off; switched on, the neck is
    mirrored — the nut moves to the right, so the fret-1 click zone lies
    right of the fret-12 zone — while every text label stays readable (its
    net screen transform is not mirrored); (3) the choice persists across
    a reload.
    """

    async def scenario():
        async with chrome_page(_song_url(live_server)) as cdp:
            await wait_ready(cdp)
            raw = await cdp.js("""(() => {
                if (document.getElementById('fretpane').hidden)
                    document.getElementById('btn-fretboard').click();
                const tracks = Guitarz.conductor.state.score.tracks;
                const bass = tracks.find(t => /bass/i.test(t.name));
                const sel = document.getElementById('fret-track');
                sel.value = bass.index;
                sel.dispatchEvent(new Event('change'));
                const staff = bass.staves.find(s => !s.isPercussion && s.tuning.length > 0);
                return JSON.stringify({
                    strings: document.querySelectorAll('#fretboard .fb-string').length,
                    labels: [...document.querySelectorAll('#fretboard .fb-label')]
                        .map(l => l.textContent),
                    want: staff.tuning.map(Guitarz.fretboard.noteName)});
            })()""")
            bass = json.loads(raw)
            assert bass["strings"] == len(bass["want"]) and bass["strings"] in (4, 5), bass
            assert bass["labels"] == bass["want"], bass

            async def layout():
                """Measure zone order, label readability and the lefty flags."""
                return json.loads(await cdp.js("""(() => {
                    const r = q => document.querySelector(q).getBoundingClientRect();
                    const f1 = r('#fretboard .fb-hit[data-fret="1"]');
                    const f12 = r('#fretboard .fb-hit[data-fret="12"]');
                    const texts = [...document.querySelectorAll('#fretboard text')];
                    return JSON.stringify({
                        lit: document.getElementById('fret-lefty').classList.contains('on'),
                        lefty: document.querySelector('#fretboard svg').dataset.lefty === '1',
                        fret1RightOf12: f1.left > f12.left,
                        mirroredText: texts.filter(t => t.getCTM().a < 0).length});
                })()"""))

            right = await layout()
            assert right == {"lit": False, "lefty": False, "fret1RightOf12": False,
                             "mirroredText": 0}, right
            await cdp.js("document.getElementById('fret-lefty').click()")
            left = await layout()
            assert left == {"lit": True, "lefty": True, "fret1RightOf12": True,
                            "mirroredText": 0}, left

            await cdp.send("Page.reload")
            await wait_ready(cdp)
            again = await layout()
            assert again["lefty"] and again["lit"], again

    asyncio.run(scenario())


def test_fretboard_dot_labels_toggle_fret_or_note_name(live_server):
    """The ``names`` toggle switches dot labels between fret numbers and notes.

    Fret numbers say where the finger goes; note names teach the neck. Spec:
    (1) ``pitchName(midi)`` names a pitch without its octave (69 = "A",
    61 = "C#"); (2) the ``#fret-names`` toggle starts off, so a clicked dot
    at string 6 (top row) fret 5 reads "5"; (3) switched on, the same dot
    reads the note it sounds — the top row's open pitch + 5 — and the
    board redraws at once; (4) the choice persists across a reload.
    """

    async def scenario():
        async with chrome_page(_song_url(live_server)) as cdp:
            await wait_ready(cdp)
            names = await cdp.js("[69, 61, 64, 40].map(Guitarz.fretboard.pitchName)")
            assert names == ["A", "C#", "E", "E"], names

            async def tap_label():
                """Click string 6 fret 5 and return what the flashed dot reads."""
                return await cdp.js("""(() => {
                    document.querySelector('#fretboard .fb-hit[data-string="6"][data-fret="5"]')
                        .dispatchEvent(new MouseEvent('click', {bubbles: true}));
                    const dots = document.querySelectorAll('#fretboard .fb-note.tap');
                    return dots[dots.length - 1].textContent;
                })()""")

            await cdp.js("""(() => {
                if (document.getElementById('fretpane').hidden)
                    document.getElementById('btn-fretboard').click();
            })()""")
            assert await cdp.js(
                "document.getElementById('fret-names').classList.contains('on')") is False
            assert await tap_label() == "5"

            await cdp.js("document.getElementById('fret-names').click()")
            want = await cdp.js("""(() => {
                const fb = Guitarz.fretboard;
                const track = Guitarz.conductor.state.score.tracks[fb.state.trackIndex];
                const staff = track.staves.find(s => !s.isPercussion && s.tuning.length > 0);
                return fb.pitchName(staff.tuning[0] + 5);
            })()""")
            assert await tap_label() == want

            await cdp.send("Page.reload")
            await wait_ready(cdp)
            assert await cdp.js(
                "document.getElementById('fret-names').classList.contains('on')") is True

    asyncio.run(scenario())


def test_fretboard_drop_d_tuning(live_server):
    """A Drop D song gets a neck labeled and played in Drop D.

    The neck must follow each track's own tuning, not assume standard.
    Spec, on the Drop D sample: (1) some string
    track is in Drop D — low string D2 (MIDI 38), the rest standard
    (A2 D3 G3 B3 E4); (2) choosing it labels the bottom string "D2";
    (3) the dot mapping holds for that tuning (top-row tuning + fret
    equals alphaTab's own ``realValue`` for the track's notes); and
    (4) with note names on, the open low string reads "D".
    """

    async def scenario():
        async with chrome_page(
            f"{live_server}/?open={urllib.parse.quote(DROPD_GP.name)}"
        ) as cdp:
            await wait_ready(cdp)
            raw = await cdp.js("""(() => {
                const fb = Guitarz.fretboard;
                const DROP_D = [64, 59, 55, 50, 45, 38];
                const track = Guitarz.conductor.state.score.tracks.find(t => t.staves.some(
                    s => !s.isPercussion && s.tuning.join() === DROP_D.join()));
                if (!track) return JSON.stringify({track: null});
                if (document.getElementById('fretpane').hidden)
                    document.getElementById('btn-fretboard').click();
                if (!document.getElementById('fret-names').classList.contains('on'))
                    document.getElementById('fret-names').click();
                const sel = document.getElementById('fret-track');
                sel.value = track.index;
                sel.dispatchEvent(new Event('change'));
                const staff = track.staves.find(s => s.tuning.join() === DROP_D.join());
                let match = 0, total = 0;
                for (const bar of staff.bars) for (const v of bar.voices)
                    for (const b of v.beats) for (const n of b.notes) {
                        if (n.isDead || n.harmonicType) continue;
                        total++;
                        if (staff.tuning[fb.stringRow(n.string, 6)] + n.fret === n.realValue) match++;
                    }
                document.querySelector('#fretboard .fb-hit[data-string="1"][data-fret="0"]')
                    .dispatchEvent(new MouseEvent('click', {bubbles: true}));
                const labels = [...document.querySelectorAll('#fretboard .fb-label')];
                return JSON.stringify({
                    track: track.name,
                    bottom: labels[labels.length - 1].textContent,
                    open: [...document.querySelectorAll('#fretboard .fb-note.tap')].pop().textContent,
                    match, total});
            })()""", gesture=True)
            data = json.loads(raw)
            assert data["track"], f"no Drop D track in {DROPD_GP.name}: {data}"
            assert data["bottom"] == "D2", data
            assert data["total"] > 0 and data["match"] / data["total"] > 0.95, data
            assert data["open"] == "D", data

    asyncio.run(scenario())


def test_tuning_offset_rule(live_server):
    """``tuningOffset`` finds the transpose that makes a song sound in standard.

    Pure rule, checked on synthetic scores so every case is explicit: every
    tuned, pitched staff is compared string by string with standard tuning
    for its string count (6/7-string guitar, 4/5-string bass). The song
    gets an offset only when every string of every such staff is off by the
    same amount, allowing whole octaves (a 12-string's octave course).
    Then the smallest such shift is returned: Eb → +1, D standard → +2,
    standard → 0, A standard → +7. Mixed tunings (Drop D, or one track Eb
    and one standard) and songs with no tuned track return null — no guess.
    """

    async def scenario():
        async with chrome_page(live_server + "/") as cdp:
            for _ in range(40):
                if await cdp.js("!!(window.Guitarz && Guitarz.conductor)"):
                    break
                await asyncio.sleep(0.25)
            raw = await cdp.js("""(() => {
                const st = t => ({isPercussion: false, tuning: t});
                const drums = {isPercussion: true, tuning: []};
                const song = (...tunings) => ({tracks: tunings.map(
                    t => ({staves: [t === null ? drums : st(t)]}))});
                const off = Guitarz.conductor.tuningOffset;
                const STD = [64, 59, 55, 50, 45, 40], BASS = [43, 38, 33, 28];
                const down = (t, n) => t.map(p => p - n);
                return JSON.stringify({
                    eb: off(song(down(STD, 1), down(BASS, 1), down(STD.map(p => p + 12), 1), null)),
                    d: off(song(down(STD, 2))),
                    std: off(song(STD, BASS, null)),
                    a: off(song(down(STD, 7))),
                    bass5: off(song(down([43, 38, 33, 28, 23], 1))),
                    seven: off(song(down([64, 59, 55, 50, 45, 40, 35], 1))),
                    dropD: off(song([64, 59, 55, 50, 45, 38])),
                    mixed: off(song(down(STD, 1), STD)),
                    none: off(song(null)),
                });
            })()""")
            assert json.loads(raw) == {
                "eb": 1, "d": 2, "std": 0, "a": 7, "bass5": 1, "seven": 1,
                "dropD": None, "mixed": None, "none": None,
            }, raw

    asyncio.run(scenario())


def test_auto_transpose_from_tuning(live_server):
    """Loading a half-step-down song sets transpose to +1 by itself.

    The user plays Eb-tuned songs on a standard-tuned guitar by raising
    playback a semitone. Spec: (1) auto is on by default (``auto`` button
    lit); (2) loading the half-step-down sample sets transpose to +1 and
    the readout shows "+1"; (3) with note names on, the fretboard names the
    notes the standard guitar plays — the open low string reads "E", and
    the bottom string label "E2"; (4) loading a standard song sets 0 again;
    (5) turning auto off leaves transpose alone on the next load, and the
    off choice survives a reload.
    """

    eb_url = f"{live_server}/?open={urllib.parse.quote(EB_GP.name)}"

    async def scenario():
        async with chrome_page(eb_url) as cdp:
            await wait_ready(cdp)
            raw = await cdp.js("""(() => {
                if (document.getElementById('fretpane').hidden)
                    document.getElementById('btn-fretboard').click();
                if (!document.getElementById('fret-names').classList.contains('on'))
                    document.getElementById('fret-names').click();
                document.querySelector('#fretboard .fb-hit[data-string="1"][data-fret="0"]')
                    .dispatchEvent(new MouseEvent('click', {bubbles: true}));
                const labels = [...document.querySelectorAll('#fretboard .fb-label')];
                return JSON.stringify({
                    auto: document.getElementById('btn-transpose-auto').classList.contains('on'),
                    transpose: Guitarz.conductor.state.transpose,
                    readout: document.getElementById('transpose-val').textContent,
                    open: [...document.querySelectorAll('#fretboard .fb-note.tap')].pop().textContent,
                    bottom: labels[labels.length - 1].textContent});
            })()""", gesture=True)
            assert json.loads(raw) == {
                "auto": True, "transpose": 1, "readout": "+1", "open": "E", "bottom": "E2",
            }, raw

            await cdp.js(f"""Guitarz.conductor.loadFile({{
                path: {json.dumps(SAMPLE_GP.name)}, name: {json.dumps(SAMPLE_GP.name)}}})""")
            await wait_ready(cdp)
            assert await cdp.js("Guitarz.conductor.state.transpose") == 0
            assert await cdp.js("document.getElementById('transpose-val').textContent") == "+0"

            await cdp.js("document.getElementById('btn-transpose-auto').click()")
            await cdp.js(f"""Guitarz.conductor.loadFile({{
                path: {json.dumps(EB_GP.name)}, name: {json.dumps(EB_GP.name)}}})""")
            await wait_ready(cdp)
            assert await cdp.js("Guitarz.conductor.state.transpose") == 0

            await cdp.send("Page.reload")
            await wait_ready(cdp)
            off = json.loads(await cdp.js("""JSON.stringify({
                lit: document.getElementById('btn-transpose-auto').classList.contains('on'),
                transpose: Guitarz.conductor.state.transpose})"""))
            assert off == {"lit": False, "transpose": 0}, off

    asyncio.run(scenario())


def test_hiding_vocal_track_keeps_lyrics_visible(live_server):
    """Hiding a lyric-bearing track moves its lyrics onto a visible track.

    A vocal track's tab staff is noise to a guitarist, but its lyric line is
    the roadmap through the song. Collapsing the vocal staff in place was
    tried first and does NOT work — alphaTab skips a track's effect bands
    (including lyrics) when no staff type is displayed. So the contract is
    transplantation: hiding a lyric track removes its staff entirely and
    re-attaches its lyric chunks, bar-aligned, to the topmost visible pitched
    track; hiding that carrier moves them again; re-showing the vocal track
    restores the original state with no lyric residue left on other tracks.

    Verified against the "Edge of the Water" sample with needle word "edge"
    (from "standing at the edge of the water"). The sample has no beat text
    at all, so the needle can only come from real lyrics. Lazy partial
    rendering is disabled so the whole score's text is present in the DOM.
    """

    async def scenario():
        async with chrome_page(
            f"{live_server}/?open={urllib.parse.quote(LYRIC_GP.name)}"
        ) as cdp:
            await wait_ready(cdp)

            await cdp.js("""(async () => {
                const api = Guitarz.conductor.state.api;
                api.settings.core.enableLazyLoading = false;
                api.updateSettings();
                const done = new Promise(res => api.renderFinished.on(res));
                api.render();
                await done;
            })()""")
            await asyncio.sleep(4)

            async def snapshot():
                """Collect the render set, lyric word presence, and per-track lyric counts."""
                raw = await cdp.js("""JSON.stringify({
                    rendered: Guitarz.conductor.state.api.tracks.map(t => t.index),
                    hasEdge: document.getElementById('alphatab').textContent
                        .toLowerCase().includes('edge'),
                    lyricCounts: Guitarz.conductor.state.score.tracks.map(t => {
                        let n = 0;
                        for (const s of t.staves) for (const b of s.bars)
                            for (const v of b.voices) for (const beat of v.beats)
                                if (beat.lyrics && beat.lyrics.length) n++;
                        return n;
                    })})""")
                return json.loads(raw)

            base = await snapshot()
            assert base["hasEdge"], f"baseline: lyrics must be visible: {base}"
            assert base["lyricCounts"][0] > 0, f"vocals must own the lyrics: {base}"

            # Hide the vocals (row 0): staff gone, lyrics carried by track 1.
            await cdp.js("document.querySelectorAll('.track-row .btn-eye')[0].click()")
            await asyncio.sleep(8)
            hid = await snapshot()
            assert 0 not in hid["rendered"], f"vocal staff must be gone: {hid}"
            assert hid["hasEdge"], f"lyrics must survive hiding the vocals: {hid}"
            assert hid["lyricCounts"][1] > 0, (
                f"topmost visible track must carry them: {hid}"
            )

            # Hide the current carrier too: lyrics must move to the next track.
            await cdp.js("document.querySelectorAll('.track-row .btn-eye')[1].click()")
            await asyncio.sleep(8)
            moved = await snapshot()
            assert 1 not in moved["rendered"], f"carrier must be hidden: {moved}"
            assert moved["hasEdge"], f"lyrics must survive hiding the carrier: {moved}"
            assert moved["lyricCounts"][1] == 0, f"old carrier must be cleaned: {moved}"
            assert moved["lyricCounts"][2] > 0, (
                f"next visible track must carry them: {moved}"
            )

            # Re-show the vocals: originals back, zero residue anywhere else.
            await cdp.js("document.querySelectorAll('.track-row .btn-eye')[0].click()")
            await asyncio.sleep(8)
            restored = await snapshot()
            assert 0 in restored["rendered"], f"vocal staff must return: {restored}"
            assert restored["hasEdge"], f"lyrics must be visible again: {restored}"
            assert restored["lyricCounts"][0] > 0, (
                f"vocals must own lyrics again: {restored}"
            )
            assert all(n == 0 for n in restored["lyricCounts"][1:]), (
                f"no lyric residue may remain on other tracks: {restored}"
            )

    asyncio.run(scenario())

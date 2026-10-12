"""Browser integration tests for the real userscript with mocked GM APIs.

No test contacts YouTube or the local download server. Chromium executes the
userscript against representative watch/Shorts pages, while deterministic GM
responses exercise the interface and actual image decoding. This does not
replace a manual smoke test in Tampermonkey against YouTube's current DOM.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import struct
import unittest
import zlib

try:
    from playwright.sync_api import sync_playwright
except ImportError:
    sync_playwright = None


PROJECT_ROOT = Path(__file__).resolve().parents[1]
USERSCRIPT = PROJECT_ROOT / "userscript" / "youtube-downloader.user.js"
VIDEO_ID = "dQw4w9WgXcQ"
SECOND_ID = "aqz-KE-bpKQ"
VIDEO_TITLE = "Видео / Fixture: Test?"
TEST_TOKEN = "browser-test-token-value-1234567890"


def image_png(width: int, height: int) -> bytes:
    """Create a valid image using stdlib only; dimensions matter to the tests."""
    def chunk(kind: bytes, content: bytes) -> bytes:
        return (
            struct.pack("!I", len(content))
            + kind
            + content
            + struct.pack("!I", zlib.crc32(kind + content) & 0xFFFFFFFF)
        )

    row = b"\x00" + bytes((30, 110, 180)) * width
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack("!IIBBBBB", width, height, 8, 2, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(row * height))
        + chunk(b"IEND", b"")
    )


WATCH_HTML = """<!doctype html><html lang="ru"><head><meta charset="utf-8">
<title>Видео / Fixture: Test? - YouTube</title></head><body>
<ytd-app><div id="content"><ytd-watch-flexy video-id="dQw4w9WgXcQ">
<div id="primary"><div id="below"><ytd-watch-metadata>
<div id="title"><h1><yt-formatted-string>Видео / Fixture: Test?</yt-formatted-string></h1></div>
<div id="top-row"><div id="owner"></div><div id="actions">
<ytd-menu-renderer><div id="top-level-buttons-computed"><button id="native-like">Like</button></div></ytd-menu-renderer>
</div></div></ytd-watch-metadata></div></div></ytd-watch-flexy></div></ytd-app>
</body></html>"""

SHORTS_HTML = """<ytd-app><div id="content"><ytd-shorts>
<ytd-reel-video-renderer is-active video-id="aqz-KE-bpKQ">
<h2 class="title">Shorts fixture</h2><div id="actions"><button id="native-like">Like</button></div>
</ytd-reel-video-renderer></ytd-shorts></div></ytd-app>"""


GM_MOCK = r"""
(() => {
  const state = window.__gmState = {
    values: {}, calls: [], downloads: [], apiOffline: false, downloadFailure: false,
    taskStatuses: [], taskIndex: 0, formatDelay: 0, canceled: [], formatsById: {},
    health: {ok: true, ffmpeg_available: true, download_dir: '/fixture/downloads'},
    formats: {
      title: 'Видео / Fixture: Test?', video_id: 'dQw4w9WgXcQ',
      thumbnail: 'https://i.ytimg.com/vi/dQw4w9WgXcQ/hqdefault.jpg',
      ffmpeg_available: true,
      qualities: [
        {value: 'best', label: 'Лучшее доступное', height: 1080, container: 'mp4', requires_ffmpeg: true},
        {value: '360', label: '360p', height: 360, container: 'mp4', requires_ffmpeg: false},
        {value: '720', label: '720p', height: 720, container: 'mp4', requires_ffmpeg: true},
        {value: '1080', label: '1080p', height: 1080, container: 'mp4', requires_ffmpeg: true}
      ]
    }
  };
  // The shipped script requests a closed root. This fixture alone exposes its
  // returned root to Playwright; production pages receive no such accessor.
  const originalAttachShadow = Element.prototype.attachShadow;
  Element.prototype.attachShadow = function(options) {
    const root = originalAttachShadow.call(this, options);
    if (this.id === 'ytdl-tm-host') {
      state.shadowMode = options.mode;
      state.nativeShadowHidden = this.shadowRoot === null;
      window.__ytdlTestShadow = root;
      Object.defineProperty(this, 'shadowRoot', {get: () => root, configurable: true});
    }
    return root;
  };
  window.GM_getValue = (key, fallback) => state.values[key] ?? fallback;
  window.GM_setValue = (key, value) => { state.values[key] = value; };
  window.GM_registerMenuCommand = () => {};
  window.GM_download = options => {
    state.downloads.push({url: options.url, name: options.name, saveAs: options.saveAs});
    const timer = setTimeout(() => state.downloadFailure
      ? options.onerror?.({error: 'Browser fixture blocked GM_download'})
      : options.onload?.({}), 5);
    return {abort: () => clearTimeout(timer)};
  };
  window.GM_xmlhttpRequest = options => {
    const record = {url: options.url, method: options.method || 'GET',
      headers: options.headers || {}, data: options.data || null};
    state.calls.push(record);
    const controller = new AbortController();
    let canceled = false, timer;
    const finish = (status, value) => {
      if (canceled) return;
      const raw = typeof value === 'string' ? value : JSON.stringify(value);
      options.onload?.({status, responseText: raw,
        response: options.responseType === 'json' ? value : raw,
        finalUrl: options.url, responseHeaders: 'Content-Type: application/json\r\n'});
    };
    const url = new URL(options.url);
    if (url.hostname === 'i.ytimg.com') {
      fetch(options.url, {signal: controller.signal}).then(async response => {
        const blob = await response.blob();
        if (!canceled) options.onload?.({status: response.status,
          response: blob, responseText: '', finalUrl: options.url,
          responseHeaders: 'Content-Type: image/png\r\n'});
      }).catch(error => { if (!canceled) options.onerror?.(error); });
    } else {
      const delay = url.pathname === '/api/formats' ? state.formatDelay : 5;
      const requestedId = url.pathname === '/api/formats'
        ? new URL(url.searchParams.get('url')).searchParams.get('v') : null;
      const requestedFormats = state.formatsById[requestedId] || state.formats;
      timer = setTimeout(() => {
        if (canceled) return;
        if (state.apiOffline) { options.onerror?.({error: 'Connection refused'}); return; }
        if (url.pathname === '/api/health') { finish(200, state.health); return; }
        if (url.pathname === '/api/formats') { finish(200, requestedFormats); return; }
        if (url.pathname === '/api/download') { finish(202, {task_id: 'task-test-1'}); return; }
        if (url.pathname.startsWith('/api/tasks/')) {
          const tasks = state.taskStatuses.length ? state.taskStatuses : [
            {status: 'downloading', progress: 25},
            {status: 'processing', progress: null},
            {status: 'completed', progress: 100, filename: 'Fixture.mp4'}
          ];
          const task = tasks[Math.min(state.taskIndex++, tasks.length - 1)];
          finish(200, {task_id: 'task-test-1', ...task}); return;
        }
        finish(404, {error: 'Unknown fixture route'});
      }, delay);
    }
    return {abort: () => {
      canceled = true; state.canceled.push(options.url);
      clearTimeout(timer); controller.abort();
    }};
  };
})();
"""


@unittest.skipUnless(sync_playwright is not None, "Install tests/requirements.txt for browser tests")
class UserscriptBrowserTests(unittest.TestCase):
    """Exercise the shipped script in a real browser without external services."""

    @classmethod
    def setUpClass(cls) -> None:
        executable = os.environ.get("YTDL_CHROMIUM_PATH") or shutil.which("chromium") or shutil.which("chromium-browser")
        cls.playwright = sync_playwright().start()
        cls.playwright.selectors.register("userscript", """({
          query(root, selector) { return window.__ytdlTestShadow?.querySelector(selector) || null; },
          queryAll(root, selector) { return [...(window.__ytdlTestShadow?.querySelectorAll(selector) || [])]; }
        })""", content_script=False)
        try:
            options = {"headless": True, "args": ["--no-sandbox"]}
            if executable:
                options["executable_path"] = executable
            cls.browser = cls.playwright.chromium.launch(**options)
        except Exception as exc:
            cls.playwright.stop()
            raise unittest.SkipTest(f"Chromium is unavailable: {exc}") from exc

    @classmethod
    def tearDownClass(cls) -> None:
        cls.browser.close()
        cls.playwright.stop()

    def setUp(self) -> None:
        self.context = self.browser.new_context(viewport={"width": 1280, "height": 800})
        self.page = self.context.new_page()
        self.page.set_default_timeout(5000)
        self.errors: list[str] = []
        self.page.on("pageerror", lambda error: self.errors.append(str(error)))
        self.image_sizes = {
            "maxresdefault": (1280, 720), "sddefault": (640, 480),
            "hqdefault": (480, 360), "mqdefault": (320, 180),
        }
        self.context.route("**/*", self.route_request)

    def tearDown(self) -> None:
        self.context.close()
        self.assertEqual(self.errors, [], "Userscript emitted uncaught browser exceptions")

    def route_request(self, route) -> None:
        url = route.request.url
        if url.startswith("https://i.ytimg.com/vi/"):
            name = url.rsplit("/", 1)[-1].split(".", 1)[0]
            width, height = self.image_sizes.get(name, (120, 90))
            route.fulfill(status=200, body=image_png(width, height), content_type="image/png",
                          headers={"Access-Control-Allow-Origin": "*"})
        elif url.startswith("https://www.youtube.com/"):
            html = WATCH_HTML if "/watch?" in url else "<!doctype html><html><body><ytd-app><div id='content'>Home fixture</div></ytd-app></body></html>"
            route.fulfill(status=200, body=html, content_type="text/html")
        else:
            route.fulfill(status=404, body="Fixture blocked external request")

    def launch(self, *, token: bool = True, offline: bool = False, path: str | None = None) -> None:
        self.page.goto("https://www.youtube.com/" + (path if path is not None else f"watch?v={VIDEO_ID}"))
        self.page.add_script_tag(content=GM_MOCK)
        self.page.evaluate("([token, offline]) => {window.__gmState.apiOffline = offline; if (token) window.__gmState.values.apiToken = token;}", [TEST_TOKEN if token else "", offline])
        self.page.add_script_tag(path=str(USERSCRIPT))

    def button(self):
        return self.ui("#download-button")

    def ui(self, selector: str):
        return self.page.locator(f"userscript={selector}")

    def panel(self):
        return self.ui("#download-panel")

    def open_panel(self) -> None:
        if not self.panel().is_visible():
            self.button().click()
        self.panel().wait_for(state="visible")

    def show_cover(self) -> None:
        self.open_panel()
        self.ui("#cover-tab").click()
        self.page.wait_for_function("!document.querySelector('#ytdl-tm-host')?.shadowRoot.querySelector('#cover-quality')?.disabled")

    def calls(self, endpoint: str) -> list[dict]:
        return self.page.evaluate("endpoint => window.__gmState.calls.filter(call => new URL(call.url).pathname === endpoint)", endpoint)

    def test_watch_button_is_unique_after_dom_changes(self) -> None:
        self.launch()
        self.button().wait_for()
        self.page.evaluate("""() => {
          for (let i = 0; i < 40; i++) {
            const marker = document.createElement('span'); marker.textContent = `change ${i}`;
            document.querySelector('#actions').append(marker); marker.remove();
          }
          document.dispatchEvent(new Event('yt-navigate-finish'));
        }""")
        self.page.wait_for_timeout(400)
        self.assertEqual(self.page.locator("#ytdl-tm-host").count(), 1)
        self.assertEqual(self.ui("#download-button").count(), 1)
        self.assertTrue(self.page.locator("#native-like").is_visible())

    def test_only_reported_video_qualities_are_displayed(self) -> None:
        self.launch()
        self.open_panel()
        self.page.wait_for_function("document.querySelector('#ytdl-tm-host')?.shadowRoot.querySelector('#video-quality')?.options.length >= 3")
        options = self.ui("#video-quality option").all_text_contents()
        joined = " ".join(options)
        for quality in ["360", "720", "1080"]:
            self.assertIn(quality, joined)
        for quality in ["480p", "1440p", "2160p"]:
            self.assertNotIn(quality, joined)
        self.assertTrue(self.calls("/api/health"))
        formats = self.calls("/api/formats")
        self.assertEqual(len(formats), 1)
        self.assertIn(VIDEO_ID, formats[0]["url"])
        self.assertEqual(formats[0]["headers"].get("X-YTD-Token"), TEST_TOKEN)

    def test_token_is_masked_and_never_rendered_as_plain_text(self) -> None:
        self.launch()
        self.open_panel()
        self.assertEqual(self.page.evaluate("window.__gmState.shadowMode"), "closed")
        self.assertTrue(self.page.evaluate("window.__gmState.nativeShadowHidden"))
        token_field = self.ui("#api-token")
        self.assertEqual(token_field.get_attribute("type"), "password")
        rendered_text = self.page.locator("#ytdl-tm-host").evaluate("host => host.shadowRoot.textContent")
        self.assertNotIn(TEST_TOKEN, rendered_text)

    def test_theme_switch_and_small_viewport_keep_panel_usable(self) -> None:
        self.page.set_viewport_size({"width": 375, "height": 667})
        self.launch(token=False)
        self.open_panel()
        light_background = self.panel().evaluate("panel => getComputedStyle(panel).backgroundColor")
        self.page.evaluate("document.documentElement.setAttribute('dark', '')")
        self.page.wait_for_function("document.querySelector('#ytdl-tm-host').hasAttribute('data-dark')")
        dark_background = self.panel().evaluate("panel => getComputedStyle(panel).backgroundColor")
        self.assertNotEqual(light_background, dark_background)
        bounds = self.panel().bounding_box()
        self.assertIsNotNone(bounds)
        self.assertGreaterEqual(bounds["x"], 0)
        self.assertGreaterEqual(bounds["y"], 0)
        self.assertLessEqual(bounds["x"] + bounds["width"], 376)
        self.assertLessEqual(bounds["y"] + bounds["height"], 668)
        self.ui("#close-panel").click()
        self.assertFalse(self.panel().is_visible())

    def test_saving_token_connects_api_and_clears_input(self) -> None:
        self.launch(token=False)
        self.open_panel()
        field = self.ui("#api-token")
        field.fill(TEST_TOKEN)
        self.ui("#save-token").click()
        self.page.wait_for_function("document.querySelector('#ytdl-tm-host').shadowRoot.querySelector('#video-quality').options.length >= 3")
        self.assertEqual(field.input_value(), "")
        self.assertEqual(self.page.evaluate("window.__gmState.values.apiToken"), TEST_TOKEN)
        self.assertEqual(self.calls("/api/health")[0]["headers"].get("X-YTD-Token"), TEST_TOKEN)

    def test_video_task_progress_and_repeated_clicks(self) -> None:
        self.launch()
        self.open_panel()
        start = self.ui("#start-download")
        self.page.wait_for_function("!document.querySelector('#ytdl-tm-host')?.shadowRoot.querySelector('#start-download')?.disabled")
        start.evaluate("button => { for (let i = 0; i < 6; i++) button.click(); }")
        self.page.wait_for_function("document.querySelector('#ytdl-tm-host').shadowRoot.querySelector('#task-progress')?.value === 25", timeout=8000)
        self.assertTrue(self.ui("#task-progress").is_visible())
        self.page.wait_for_function("window.__gmState.taskIndex >= 3", timeout=10000)
        self.page.wait_for_function("document.querySelector('#ytdl-tm-host').shadowRoot.querySelector('#task-status').textContent.toLowerCase().includes('заверш')", timeout=5000)
        self.assertEqual(len(self.calls("/api/download")), 1)
        payload = json.loads(self.calls("/api/download")[0]["data"])
        self.assertEqual(payload["url"], f"https://www.youtube.com/watch?v={VIDEO_ID}")
        self.assertEqual(payload["quality"], "best")

    def test_offline_api_has_actionable_message_and_cover_still_works(self) -> None:
        self.launch(offline=True)
        self.open_panel()
        self.page.wait_for_function("document.querySelector('#ytdl-tm-host').shadowRoot.querySelector('#video-status').textContent.includes('Нет связи')")
        self.assertIn("server.py", self.ui("#video-view").text_content())
        self.assertTrue(self.ui("#start-download").is_disabled())
        self.ui("#cover-tab").click()
        self.page.wait_for_function("!document.querySelector('#ytdl-tm-host')?.shadowRoot.querySelector('#cover-quality')?.disabled")
        self.ui("#download-cover").click()
        self.page.wait_for_function("window.__gmState.downloads.length === 1")
        self.assertTrue(self.page.evaluate("window.__gmState.downloads[0].url.includes('i.ytimg.com')"))

    def test_download_error_is_displayed(self) -> None:
        self.launch()
        self.page.evaluate("window.__gmState.taskStatuses = [{status: 'error', progress: null, error: 'Network fixture failure'}]")
        self.open_panel()
        self.page.wait_for_function("!document.querySelector('#ytdl-tm-host')?.shadowRoot.querySelector('#start-download')?.disabled")
        self.ui("#start-download").click()
        self.page.wait_for_function("document.querySelector('#ytdl-tm-host').shadowRoot.querySelector('#task-status').textContent.includes('Network fixture failure')", timeout=8000)
        self.assertEqual(len(self.calls("/api/download")), 1)

    def test_thumbnail_placeholder_falls_back_and_filename_is_safe(self) -> None:
        self.image_sizes["maxresdefault"] = (120, 90)
        self.launch(token=False)
        self.show_cover()
        select = self.ui("#cover-quality")
        self.page.wait_for_function("document.querySelector('#ytdl-tm-host').shadowRoot.querySelector('#cover-preview')?.naturalWidth > 0")
        choices = select.locator("option").all_text_contents()
        self.assertIn("640", " ".join(choices))
        self.assertIn("480", " ".join(choices))
        self.assertNotIn("1280", " ".join(choices))
        self.assertEqual(self.ui("#cover-preview").evaluate("image => image.naturalWidth"), 640)
        self.ui("#download-cover").click()
        self.page.wait_for_function("window.__gmState.downloads.length === 1")
        download = self.page.evaluate("window.__gmState.downloads[0]")
        self.assertIn("sddefault.jpg", download["url"])
        self.assertTrue(download["name"].endswith("_thumbnail.jpg"))
        self.assertNotRegex(download["name"], r"[\\/:*?\"<>|]")
        self.assertEqual(self.calls("/api/download"), [])

    def test_blocked_gm_download_uses_verified_blob_fallback(self) -> None:
        self.launch(token=False)
        self.page.evaluate("window.__gmState.downloadFailure = true")
        self.show_cover()
        with self.page.expect_download(timeout=5000) as download_event:
            self.ui("#download-cover").click()
        download = download_event.value
        self.assertTrue(download.url.startswith("blob:https://www.youtube.com/"))
        self.assertTrue(download.suggested_filename.endswith("_thumbnail.jpg"))
        self.page.wait_for_function("!document.querySelector('#ytdl-tm-host').shadowRoot.querySelector('#cover-manual-link').hidden")
        self.assertTrue(self.ui("#cover-manual-link").is_visible())
        self.assertEqual(self.calls("/api/download"), [])

    def test_spa_watch_to_shorts_uses_new_video_id(self) -> None:
        self.launch(token=False)
        self.show_cover()
        self.page.evaluate("html => {history.pushState({}, '', '/shorts/aqz-KE-bpKQ'); document.body.innerHTML = html; document.dispatchEvent(new Event('yt-navigate-finish'));}", SHORTS_HTML)
        self.button().wait_for()
        self.assertEqual(self.page.locator("#ytdl-tm-host").count(), 1)
        self.show_cover()
        self.ui("#download-cover").click()
        self.page.wait_for_function("window.__gmState.downloads.length === 1")
        self.assertIn(SECOND_ID, self.page.evaluate("window.__gmState.downloads[0].url"))

    def test_home_to_watch_spa_inserts_button(self) -> None:
        self.launch(token=False, path="")
        self.page.evaluate("() => { history.replaceState({}, '', '/'); document.dispatchEvent(new Event('yt-navigate-finish')); }")
        self.page.wait_for_function("!document.querySelector('#ytdl-tm-host')")
        self.page.evaluate("html => {history.pushState({}, '', '/watch?v=dQw4w9WgXcQ'); document.body.innerHTML = html; document.dispatchEvent(new Event('yt-navigate-finish'));}", WATCH_HTML)
        self.button().wait_for()
        self.assertEqual(self.page.locator("#ytdl-tm-host").count(), 1)

    def test_late_formats_response_cannot_overwrite_navigated_video(self) -> None:
        self.launch()
        self.page.evaluate("""() => {
          window.__gmState.formatDelay = 600;
          window.__gmState.formats = {...window.__gmState.formats, title: 'Stale watch title'};
          window.__gmState.formatsById['aqz-KE-bpKQ'] = {
            ...window.__gmState.formats, video_id: 'aqz-KE-bpKQ', title: 'New Shorts title'
          };
        }""")
        self.open_panel()
        self.page.wait_for_function("window.__gmState.calls.some(call => new URL(call.url).pathname === '/api/formats')")
        self.page.evaluate("html => {history.pushState({}, '', '/shorts/aqz-KE-bpKQ'); document.body.innerHTML = html; document.dispatchEvent(new Event('yt-navigate-finish'));}", SHORTS_HTML)
        self.button().wait_for()
        self.open_panel()
        self.page.wait_for_function("document.querySelector('#ytdl-tm-host').shadowRoot.querySelector('#video-view').textContent.includes('New Shorts title')")
        self.assertNotIn("Stale watch title", self.ui("#video-view").text_content())


if __name__ == "__main__":
    unittest.main()

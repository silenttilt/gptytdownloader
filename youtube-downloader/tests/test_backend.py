"""Offline backend tests: real loopback HTTP, deterministic yt-dlp fixtures.

No test contacts YouTube, executes FFmpeg, or downloads remote media.
"""

from __future__ import annotations

import copy
from concurrent.futures import ThreadPoolExecutor
import http.client
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest
from urllib.parse import quote

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))
import server  # noqa: E402


VIDEO_ID = "BaW_jenozKc"
VIDEO_URL = f"https://www.youtube.com/watch?v={VIDEO_ID}"
TOKEN = "0123456789abcdef" * 4


def video_format(format_id, height, *, ext="mp4", vcodec="avc1.640028", acodec="none"):
    return {
        "format_id": str(format_id),
        "height": height,
        "width": round(height * 16 / 9),
        "ext": ext,
        "vcodec": vcodec,
        "acodec": acodec,
        "fps": 30,
        "tbr": height * 3,
        "url": "https://rr1.googlevideo.com/videoplayback",
        "protocol": "https",
    }


def audio_format(format_id="140", *, ext="m4a", acodec="mp4a.40.2"):
    return {
        "format_id": str(format_id),
        "ext": ext,
        "vcodec": "none",
        "acodec": acodec,
        "abr": 128,
        "url": "https://rr1.googlevideo.com/videoplayback",
        "protocol": "https",
    }


def fixture_info(formats=None):
    return {
        "id": VIDEO_ID,
        "title": "Example video / безопасное имя",
        "thumbnail": f"https://i.ytimg.com/vi/{VIDEO_ID}/hqdefault.jpg",
        "webpage_url": VIDEO_URL,
        "extractor_key": "Youtube",
        "formats": formats if formats is not None else [
            video_format("18", 360, acodec="mp4a.40.2"),
            video_format("135", 480),
            video_format("22", 720, acodec="mp4a.40.2"),
            video_format("137", 1080),
            video_format("248", 1080, ext="webm", vcodec="vp9"),
            video_format("271", 1440, ext="webm", vcodec="vp9"),
            video_format("313", 2160, ext="webm", vcodec="vp9"),
            audio_format(),
            audio_format("251", ext="webm", acodec="opus"),
        ],
    }


class FakeYoutubeDLFactory:
    """A controllable library boundary, including actual output-file creation."""

    def __init__(self, info=None, *, blocked=False, failure=None):
        self.info = fixture_info() if info is None else info
        self.failure = failure
        self.started = threading.Event()
        self.release = threading.Event()
        self.progress_reported = threading.Event()
        self.processing_started = threading.Event()
        self.release_processing = threading.Event()
        self.release_processing.set()
        self.calls = []
        self.download_count = 0
        self.lock = threading.Lock()
        if not blocked:
            self.release.set()

    def __call__(self, options):
        owner = self

        class FakeYoutubeDL:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def extract_info(self, url, download=False):
                with owner.lock:
                    owner.calls.append((url, download, options))
                result = copy.deepcopy(owner.info)
                if not download:
                    return result
                with owner.lock:
                    owner.download_count += 1
                owner.started.set()
                for hook in options.get("progress_hooks", []):
                    hook({
                        "status": "downloading",
                        "downloaded_bytes": 25,
                        "total_bytes": 100,
                        "speed": 10,
                        "eta": 8,
                        "info_dict": result,
                    })
                owner.progress_reported.set()
                if not owner.release.wait(5):
                    raise RuntimeError("The test failed to release the fake downloader")
                if owner.failure is not None:
                    raise owner.failure
                for hook in options.get("progress_hooks", []):
                    hook({"status": "finished", "downloaded_bytes": 100,
                          "total_bytes": 100, "info_dict": result})
                for hook in options.get("postprocessor_hooks", []):
                    hook({"status": "started", "postprocessor": "Merger", "info_dict": result})
                owner.processing_started.set()
                if not owner.release_processing.wait(5):
                    raise RuntimeError("The test failed to release fake postprocessing")
                template = options["outtmpl"]
                if isinstance(template, dict):
                    template = template["default"]
                extension = options.get("merge_output_format") or "mp4"
                filename = str(template).replace("%(ext)s", extension)
                filename = filename.replace("%(id)s", VIDEO_ID).replace("%(title)s", "Example video")
                output = Path(filename)
                output.parent.mkdir(parents=True, exist_ok=True)
                output.write_bytes(b"offline fixture media")
                result.update({"ext": extension, "filepath": str(output),
                               "requested_downloads": [{"filepath": str(output)}]})
                for hook in options.get("postprocessor_hooks", []):
                    hook({"status": "finished", "postprocessor": "Merger", "info_dict": result})
                return result

        return FakeYoutubeDL()


class UrlAndFilenameTests(unittest.TestCase):
    def test_supported_video_urls_are_canonicalized(self):
        for url in (
            VIDEO_URL,
            f"https://youtube.com/watch?v={VIDEO_ID}&list=PLexample",
            f"https://m.youtube.com/watch?v={VIDEO_ID}",
            f"https://www.youtube.com/shorts/{VIDEO_ID}?feature=share",
            f"https://youtu.be/{VIDEO_ID}?t=10",
        ):
            with self.subTest(url=url):
                self.assertEqual(server.normalize_youtube_url(url), VIDEO_URL)

    def test_unsupported_urls_cannot_reach_an_extractor(self):
        for url in (
            "http://127.0.0.1:8080/private", "http://169.254.169.254/latest/meta-data",
            "file:///etc/passwd", f"ftp://www.youtube.com/watch?v={VIDEO_ID}",
            f"https://www.youtube.com.evil.example/watch?v={VIDEO_ID}",
            f"https://evil.example@www.youtube.com/watch?v={VIDEO_ID}",
            f"https://user:password@www.youtube.com/watch?v={VIDEO_ID}",
            f"https://www.youtube.com:12345/watch?v={VIDEO_ID}",
            f"https://www.youtube.com/embed/{VIDEO_ID}",
            "https://www.youtube.com/watch?v=bad", "https://www.youtube.com/shorts/",
            f"https://youtu.be/{VIDEO_ID}/other", "javascript:alert(1)", "", None,
        ):
            with self.subTest(url=url):
                with self.assertRaises((server.APIError, ValueError, TypeError)):
                    server.normalize_youtube_url(url)

    def test_filename_cannot_escape_directory_or_use_control_characters(self):
        result = server.safe_filename('../bad\\name:\x00\r\n*?"<>|')
        self.assertTrue(result)
        self.assertNotIn(result, (".", ".."))
        self.assertFalse(any(char in result for char in '/\\:*?"<>|'))
        self.assertFalse(any(ord(char) < 32 for char in result))
        self.assertFalse(result.endswith((" ", ".")))

    def test_filename_unicode_length_and_reserved_windows_names(self):
        self.assertIn("Пример", server.safe_filename("Пример видео"))
        self.assertLessEqual(len(server.safe_filename("x" * 1000)), 140)
        self.assertLessEqual(len(server.safe_filename("示例视频😀" * 100).encode("utf-8")), 220)
        for name in ("", "...", "CON", "NUL", "COM1", "LPT1"):
            with self.subTest(name=name):
                result = server.safe_filename(name)
                self.assertTrue(result)
                self.assertNotIn(result.upper(), ("CON", "NUL", "COM1", "LPT1", ".", ".."))


class FormatSelectionTests(unittest.TestCase):
    def test_only_real_qualities_are_advertised(self):
        info = fixture_info([video_format("18", 360, acodec="mp4a.40.2"),
                             video_format("137", 1080), audio_format()])
        qualities = server.list_qualities(info, ffmpeg_available=True)
        self.assertEqual({row["value"] for row in qualities}, {"best", "360", "1080"})

    def test_mp4_with_audio_is_preferred_at_requested_quality(self):
        selection = server.select_format(fixture_info(), "1080", True)
        self.assertEqual(selection.height, 1080)
        self.assertEqual(selection.container, "mp4")
        self.assertTrue(selection.requires_ffmpeg)
        self.assertIn("137", selection.selector)
        self.assertIn("140", selection.selector)

    def test_progressive_video_does_not_require_ffmpeg(self):
        selection = server.select_format(fixture_info(), "720", False)
        self.assertEqual(selection.height, 720)
        self.assertEqual(selection.container, "mp4")
        self.assertFalse(selection.requires_ffmpeg)
        self.assertEqual(selection.selector, "22")

    def test_webm_and_incompatible_codec_fallbacks_are_honest(self):
        webm = fixture_info([video_format("313", 2160, ext="webm", vcodec="vp9"),
                             audio_format("251", ext="webm", acodec="opus")])
        selection = server.select_format(webm, "best", True)
        self.assertEqual(selection.height, 2160)
        self.assertEqual(selection.container, "webm")
        self.assertTrue(selection.requires_ffmpeg)
        incompatible = fixture_info([video_format("100", 1080, ext="webm", vcodec="vp8"),
                                     audio_format()])
        self.assertEqual(server.select_format(incompatible, "1080", True).container, "mkv")

    def test_best_means_highest_available_quality(self):
        self.assertEqual(server.select_format(fixture_info(), "best", True).height, 2160)

    def test_no_ffmpeg_omits_unusable_qualities_and_falls_back_to_muxed(self):
        qualities = server.list_qualities(fixture_info(), ffmpeg_available=False)
        self.assertEqual({row["value"] for row in qualities}, {"best", "360", "720"})
        self.assertEqual(server.select_format(fixture_info(), "best", False).height, 720)
        with self.assertRaises(server.APIError):
            server.select_format(fixture_info(), "1080", False)

    def test_drm_and_video_without_audio_are_not_download_choices(self):
        protected = video_format("313", 2160, acodec="mp4a.40.2")
        protected["has_drm"] = True
        info = fixture_info([protected, video_format("18", 360, acodec="mp4a.40.2")])
        self.assertEqual(server.select_format(info, "best", True).height, 360)
        with self.assertRaises(server.APIError):
            server.select_format(fixture_info([video_format("137", 1080)]), "best", True)

    def test_absent_quality_is_not_silently_substituted(self):
        with self.assertRaises(server.APIError):
            server.select_format(fixture_info(), "9999", True)

    def test_portrait_shorts_offer_1080p_for_1080_by_1920_video(self):
        portrait = video_format("299", 1920, acodec="mp4a.40.2")
        portrait["width"] = 1080
        info = fixture_info([portrait])
        self.assertEqual(server.select_format(info, "best", False).height, 1080)
        self.assertEqual(server.select_format(info, "1080", False).height, 1080)
        self.assertEqual({row["value"] for row in server.list_qualities(info, False)}, {"best", "1080"})


class TokenStorageTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.path = Path(self.tempdir.name) / "private-config" / "token"

    def tearDown(self):
        self.tempdir.cleanup()

    def test_token_is_generated_once_and_reused(self):
        token = server.load_or_create_token(self.path)
        self.assertGreaterEqual(len(token), 32)
        self.assertTrue(token == server.load_or_create_token(self.path))
        self.assertTrue(token != server.load_or_create_token(self.path.with_name("another-token")))

    @unittest.skipIf(os.name == "nt", "POSIX permission bits are not portable to Windows")
    def test_new_token_has_private_posix_permissions(self):
        server.load_or_create_token(self.path)
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.path.parent.stat().st_mode & 0o777, 0o700)

    def test_corrupt_token_is_rejected_and_not_overwritten(self):
        self.path.parent.mkdir()
        self.path.write_text("too-short\n", encoding="utf-8")
        with self.assertRaises(ValueError):
            server.load_or_create_token(self.path)
        self.assertEqual(self.path.read_text(encoding="utf-8"), "too-short\n")

    def test_symlink_token_is_rejected(self):
        self.path.parent.mkdir()
        target = self.path.with_name("target")
        target.write_text(TOKEN, encoding="utf-8")
        try:
            self.path.symlink_to(target)
        except (OSError, NotImplementedError):
            self.skipTest("Symlinks are unavailable in this execution environment")
        with self.assertRaises(ValueError):
            server.load_or_create_token(self.path)
        self.assertTrue(target.read_text(encoding="utf-8") == TOKEN)


class HttpApiTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.fake = FakeYoutubeDLFactory(blocked=True)
        self.service = server.DownloaderService(
            self.tempdir.name, TOKEN, ffmpeg_available=True,
            youtube_dl_factory=self.fake,
        )
        self.httpd = server.make_server(self.service, port=0)
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()
        self.port = self.httpd.server_address[1]

    def tearDown(self):
        self.fake.release.set()
        self.fake.release_processing.set()
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(timeout=5)
        self.service.close(wait=True)
        self.tempdir.cleanup()

    def request(self, method, path, *, data=None, raw=None, authenticated=True, headers=None):
        request_headers = {"X-YTD-Token": TOKEN} if authenticated else {}
        request_headers.update(headers or {})
        body = raw
        if data is not None:
            body = json.dumps(data).encode("utf-8")
            request_headers.setdefault("Content-Type", "application/json")
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        try:
            connection.request(method, path, body=body, headers=request_headers)
            response = connection.getresponse()
            payload = response.read()
            return response.status, dict(response.getheaders()), json.loads(payload) if payload else None
        finally:
            connection.close()

    def create(self, quality="720"):
        status, _headers, result = self.request("POST", "/api/download", data={"url": VIDEO_URL, "quality": quality})
        self.assertIn(status, (200, 202), result)
        return result

    def wait_for_task(self, task_id, expected):
        deadline = time.monotonic() + 5
        last = None
        while time.monotonic() < deadline:
            status, _headers, last = self.request("GET", f"/api/tasks/{task_id}")
            self.assertEqual(status, 200)
            if last["status"] == expected:
                return last
            time.sleep(0.01)
        self.fail(f"Task did not reach {expected!r}; last response: {last}")

    def test_every_api_route_requires_token(self):
        for method, path, data in (
            ("GET", "/api/health", None),
            ("GET", "/api/formats?url=" + quote(VIDEO_URL, safe=""), None),
            ("POST", "/api/download", {"url": VIDEO_URL, "quality": "best"}),
            ("GET", "/api/tasks/example", None),
        ):
            with self.subTest(path=path):
                status, _headers, _result = self.request(method, path, data=data, authenticated=False)
                self.assertEqual(status, 401)
        status, _headers, _result = self.request("GET", "/api/health", headers={"X-YTD-Token": "wrong"})
        self.assertEqual(status, 401)
        self.assertFalse(self.fake.calls)

    def test_valid_auth_with_youtube_origin_and_without_origin(self):
        for headers in ({}, {"Origin": "https://www.youtube.com"}, {"Origin": "https://youtube.com"}):
            with self.subTest(headers=headers):
                status, response_headers, result = self.request("GET", "/api/health", headers=headers)
                self.assertEqual(status, 200, result)
                self.assertNotIn(TOKEN, json.dumps(result))
                if headers:
                    self.assertEqual(response_headers.get("Access-Control-Allow-Origin"), headers["Origin"])

    def test_untrusted_origins_are_rejected_even_with_token(self):
        for origin in ("https://evil.example", "https://www.youtube.com.evil.example", "null", "http://www.youtube.com"):
            with self.subTest(origin=origin):
                status, headers, _result = self.request("POST", "/api/download", data={"url": VIDEO_URL, "quality": "best"}, headers={"Origin": origin})
                self.assertEqual(status, 403)
                self.assertNotEqual(headers.get("Access-Control-Allow-Origin"), origin)
        self.assertFalse(self.fake.calls)

    def test_cors_preflight_allows_only_youtube_origin(self):
        status, headers, _result = self.request("OPTIONS", "/api/download", authenticated=False, headers={
            "Origin": "https://www.youtube.com", "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "content-type,x-ytd-token",
        })
        self.assertIn(status, (200, 204))
        self.assertEqual(headers.get("Access-Control-Allow-Origin"), "https://www.youtube.com")
        self.assertIn("x-ytd-token", headers.get("Access-Control-Allow-Headers", "").lower())
        status, _headers, _result = self.request("OPTIONS", "/api/download", authenticated=False, headers={"Origin": "https://evil.example"})
        self.assertEqual(status, 403)

    def test_formats_are_real_and_include_merge_requirements(self):
        status, _headers, result = self.request("GET", "/api/formats?url=" + quote(VIDEO_URL, safe=""))
        self.assertEqual(status, 200, result)
        self.assertEqual(result["video_id"], VIDEO_ID)
        self.assertEqual({row["value"] for row in result["qualities"]}, {"best", "360", "480", "720", "1080", "1440", "2160"})
        self.assertFalse(self.fake.calls[0][1])

    def test_ssrf_and_non_video_urls_are_rejected_before_yt_dlp(self):
        for url in ("http://127.0.0.1:9/", "https://example.com/watch?v=" + VIDEO_ID):
            with self.subTest(url=url):
                status, _headers, _result = self.request("GET", "/api/formats?url=" + quote(url, safe=""))
                self.assertEqual(status, 400)
                status, _headers, _result = self.request("POST", "/api/download", data={"url": url, "quality": "best"})
                self.assertEqual(status, 400)
        self.assertFalse(self.fake.calls)

    def test_invalid_json_missing_fields_and_oversized_bodies(self):
        cases = (
            {"raw": b"{", "headers": {"Content-Type": "application/json"}},
            {"raw": ('{"url":' + json.dumps(VIDEO_URL) + ',"url":' + json.dumps(VIDEO_URL)
                     + ',"quality":"best"}').encode("utf-8"),
             "headers": {"Content-Type": "application/json"}},
            {"data": []}, {"data": {}}, {"data": {"url": VIDEO_URL, "quality": {"height": 1080}}},
            {"data": {"url": VIDEO_URL, "quality": "best", "command": "whoami"}},
        )
        for kwargs in cases:
            with self.subTest(kwargs=kwargs):
                status, _headers, _result = self.request("POST", "/api/download", **kwargs)
                self.assertEqual(status, 400)
        # The server must reject an oversized declared body before reading it.
        # Sending a megabyte after its early rejection races the socket close
        # and can make the client raise BrokenPipeError instead of reading 413.
        status, _headers, _result = self.request("POST", "/api/download", headers={
            "Content-Type": "application/json",
            "Content-Length": str(server.MAX_BODY_BYTES + 1),
        })
        self.assertEqual(status, 413)
        self.assertFalse(self.fake.calls)

    def test_task_runs_asynchronously_and_duplicate_clicks_share_task(self):
        first = self.create()
        self.assertTrue(self.fake.started.wait(2))
        self.assertTrue(self.fake.progress_reported.wait(2))
        second = self.create()
        self.assertEqual(second["task_id"], first["task_id"])
        self.assertTrue(second["deduplicated"])
        status, _headers, health = self.request("GET", "/api/health")
        self.assertEqual(status, 200, health)
        downloading = self.wait_for_task(first["task_id"], "downloading")
        self.assertGreater(downloading["progress"], 0)
        self.assertLess(downloading["progress"], 100)
        self.assertEqual(self.fake.download_count, 1)
        self.fake.release.set()
        completed = self.wait_for_task(first["task_id"], "completed")
        self.assertEqual(completed["progress"], 100)
        path = Path(completed["filepath"])
        self.assertTrue(path.is_file())
        self.assertEqual(path.parent, Path(self.tempdir.name))
        self.assertEqual(path.suffix, ".mp4")
        self.assertFalse(any(char in path.name for char in '/\\:*?"<>|'))

    def test_concurrent_duplicate_requests_create_only_one_worker(self):
        with ThreadPoolExecutor(max_workers=8) as clients:
            results = list(clients.map(lambda _index: self.create(), range(8)))
        self.assertEqual(len({result["task_id"] for result in results}), 1)
        self.assertTrue(self.fake.started.wait(2))
        self.assertEqual(self.fake.download_count, 1)
        self.fake.release.set()
        self.wait_for_task(results[0]["task_id"], "completed")

    def test_download_errors_are_reported_as_terminal_task_errors(self):
        self.fake.failure = RuntimeError("Network unavailable during fixture download")
        result = self.create()
        self.fake.release.set()
        task = self.wait_for_task(result["task_id"], "error")
        self.assertIn("Сетевая ошибка", task["error"])
        self.assertFalse(any(Path(self.tempdir.name).glob("*.mp4")))

    def test_merge_processing_status_is_visible(self):
        self.fake.release_processing.clear()
        self.fake.release.set()
        result = self.create("1080")
        self.assertTrue(self.fake.processing_started.wait(2))
        task = self.wait_for_task(result["task_id"], "processing")
        self.assertNotIn("error", task)
        self.fake.release_processing.set()
        completed = self.wait_for_task(result["task_id"], "completed")
        self.assertEqual(completed["container"], "mp4")

    def test_invalid_content_lengths_are_rejected_without_reading(self):
        for length in ("-1", "not-a-number"):
            with self.subTest(length=length):
                status, _headers, result = self.request("POST", "/api/download", headers={
                    "Content-Length": length, "Content-Type": "application/json",
                })
                self.assertIn(status, (400, 411), result)
        self.assertFalse(self.fake.calls)

    def test_unknown_task_and_unknown_route_return_json_404(self):
        for path in ("/api/tasks/does-not-exist", "/api/not-an-endpoint"):
            with self.subTest(path=path):
                status, _headers, result = self.request("GET", path)
                self.assertEqual(status, 404)
                self.assertIn("error", result)


if __name__ == "__main__":
    unittest.main()

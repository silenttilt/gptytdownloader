"""Real yt-dlp + FFmpeg integration using generated, local-only media.

Only YouTube metadata extraction is replaced. HTTP transfers, the format
selector, progress hooks, FFmpeg merging, and final file discovery are real.
"""

from __future__ import annotations

import copy
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from urllib.request import Request, urlopen

import yt_dlp

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))
import server


@unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"),
                     "FFmpeg and ffprobe are needed for real media integration")
class MediaIntegrationTests(unittest.TestCase):
    def test_real_video_audio_download_merge_and_completed_file(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            video = root / "video.mp4"
            audio = root / "audio.m4a"
            subprocess.run([
                "ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error",
                "-f", "lavfi", "-i", "color=c=blue:s=640x360:r=10",
                "-t", "0.6", "-c:v", "libx264", "-threads", "1",
                "-pix_fmt", "yuv420p", str(video),
            ], check=True, timeout=20, capture_output=True)
            subprocess.run([
                "ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error",
                "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=44100",
                "-t", "0.6", "-c:a", "aac", "-threads", "1", str(audio),
            ], check=True, timeout=20, capture_output=True)
            files = {"/video.mp4": video.read_bytes(), "/audio.m4a": audio.read_bytes()}

            class FixtureHandler(BaseHTTPRequestHandler):
                def do_GET(self):
                    content = files.get(self.path)
                    if content is None:
                        self.send_error(404)
                        return
                    self.send_response(200)
                    self.send_header("Content-Length", str(len(content)))
                    self.send_header("Content-Type", "application/octet-stream")
                    self.end_headers()
                    self.wfile.write(content)

                def log_message(self, *_args):
                    pass

            media = ThreadingHTTPServer(("127.0.0.1", 0), FixtureHandler)
            media_thread = threading.Thread(target=media.serve_forever, daemon=True)
            media_thread.start()
            origin = f"http://127.0.0.1:{media.server_address[1]}"
            info = {
                "id": "BaW_jenozKc", "title": "Локальная проверка 100% объединения",
                "duration": 0.6, "extractor": "fixture", "extractor_key": "Fixture",
                "webpage_url": "https://www.youtube.com/watch?v=BaW_jenozKc",
                "formats": [
                    {"format_id": "137", "url": origin + "/video.mp4", "ext": "mp4",
                     "width": 640, "height": 360, "vcodec": "avc1.640028", "acodec": "none",
                     "fps": 10, "protocol": "http", "filesize": len(files["/video.mp4"])},
                    {"format_id": "140", "url": origin + "/audio.m4a", "ext": "m4a",
                     "vcodec": "none", "acodec": "mp4a.40.2", "abr": 128,
                     "protocol": "http", "filesize": len(files["/audio.m4a"])},
                ],
            }

            class FixtureYoutubeDL(yt_dlp.YoutubeDL):
                def extract_info(self, url, download=True, **_kwargs):
                    return self.process_ie_result(copy.deepcopy(info), download=download)

            token = "fixture-integration-token-with-32-characters"
            service = server.DownloaderService(
                root / "downloads", token, ffmpeg_available=True,
                youtube_dl_factory=FixtureYoutubeDL,
            )
            api = server.make_server(service, port=0)
            api_thread = threading.Thread(target=api.serve_forever, daemon=True)
            api_thread.start()
            base = f"http://127.0.0.1:{api.server_address[1]}"

            def request(path, body=None):
                headers = {"X-YTD-Token": token}
                payload = None
                if body is not None:
                    payload = json.dumps(body).encode()
                    headers["Content-Type"] = "application/json"
                with urlopen(Request(base + path, data=payload, headers=headers), timeout=10) as response:
                    return json.load(response)

            try:
                created = request("/api/download", {"url": info["webpage_url"], "quality": "360"})
                deadline = time.monotonic() + 20
                while time.monotonic() < deadline:
                    task = request("/api/tasks/" + created["task_id"])
                    if task["status"] in ("completed", "error"):
                        break
                    time.sleep(0.05)
                self.assertEqual(task["status"], "completed", task)
                output = Path(task["filepath"])
                self.assertTrue(output.is_file())
                self.assertEqual(output.suffix, ".mp4")
                self.assertIn("100%", output.name)
                self.assertEqual(task["container"], "mp4")
                self.assertEqual(task["progress"], 100)
                probe = subprocess.run([
                    "ffprobe", "-v", "error", "-show_streams", "-of", "json", str(output),
                ], check=True, capture_output=True, text=True, timeout=10)
                streams = json.loads(probe.stdout)["streams"]
                self.assertEqual({stream["codec_type"] for stream in streams}, {"video", "audio"})
                self.assertEqual({stream["codec_name"] for stream in streams}, {"h264", "aac"})
            finally:
                api.shutdown()
                api.server_close()
                service.close(wait=True)
                api_thread.join(timeout=5)
                media.shutdown()
                media.server_close()
                media_thread.join(timeout=5)


if __name__ == "__main__":
    unittest.main()

import json
from unittest.mock import patch
import urllib.request

from seedance_client import SeedanceClient, USER_AGENT, SeedanceError, build_request


def test_frame_mode_uses_adaptive_ratio():
    body = build_request("a paper boat", duration=5, resolution="720p", aspect_ratio="16:9",
                         generate_audio=True, start_image="https://cdn.example/frame.png")
    assert body["aspect_ratio"] == "adaptive"
    assert body["start_image"]["url"].startswith("https://")


def test_references_have_independent_caps():
    body = build_request("a street", duration=4, resolution="480p", aspect_ratio="1:1",
                         generate_audio=False, images=[f"https://cdn.example/{i}.png" for i in range(2)])
    assert body["reference_images"] == [{"url": "https://cdn.example/0.png"}, {"url": "https://cdn.example/1.png"}]


def test_edit_cannot_mix_reference_assets():
    try:
        build_request("replace the sky", duration=5, resolution="720p", aspect_ratio="16:9",
                      generate_audio=True, videos=["https://cdn.example/ref.mp4"],
                      edit_video="https://cdn.example/source.mp4")
    except SeedanceError as exc:
        assert "only the video it edits" in str(exc)
    else:
        raise AssertionError("mixed edit inputs were accepted")


def test_client_source_declares_explicit_user_agent():
    assert USER_AGENT and "urllib" not in USER_AGENT.lower()


class _Response:
    def __init__(self, payload=b'{"request_id":"job_1"}'):
        self.payload = payload
    def __enter__(self):
        return self
    def __exit__(self, *args):
        return False
    def read(self):
        return self.payload


def test_json_request_and_upload_put_have_user_agent():
    seen = []
    def fake_open(request, timeout=None):
        seen.append((request.get_method(), request.full_url, request.headers.get("User-agent")))
        if request.get_method() == "POST" and request.full_url.endswith("/media/uploads"):
            return _Response(b'{"upload_url":"https://upload.example/ticket","media_url":"https://cdn.example/a.png"}')
        if request.get_method() == "PUT":
            return _Response(b"")
        return _Response()
    with patch.object(urllib.request, "urlopen", side_effect=fake_open):
        client = SeedanceClient("test-key", "https://api.example/v1")
        assert client.submit({"model": "seedance-2.5", "prompt": "x"}) == "job_1"
        assert client.upload(b"png", "image", "image/png").startswith("https://")
    assert [row[2] for row in seen] == [USER_AGENT, USER_AGENT, USER_AGENT]


def test_poll_path_quotes_request_id():
    seen = []
    def fake_open(request, timeout=None):
        seen.append(request.full_url)
        return _Response(b'{"status":"pending"}')
    with patch.object(urllib.request, "urlopen", side_effect=fake_open):
        SeedanceClient("test-key", "https://api.example/v1").status("safe_job-1")
    assert seen == ["https://api.example/v1/videos/safe_job-1"]

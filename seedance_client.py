"""HTTP client, request builder and media encoders for the Seedance 2.5 ComfyUI nodes.

No ComfyUI imports, so it can be tested on its own. Standard library only, except that the
image and audio encoders use numpy and Pillow, which every ComfyUI install already has.
"""
from __future__ import annotations

import io
import json
import os
import urllib.error
import urllib.request
from urllib.parse import quote
import wave
from pathlib import Path

DEFAULT_BASE_URL = "https://argolink.io/v1"
MODEL = "seedance-2.5"
USER_AGENT = "seedance-2-5-comfyui-workflow/1.0"
# USD per output second, read from https://argolink.io/en/models/seedance-2.5 on 2026-10-07.
PRICES = {"480p": 0.0874, "720p": 0.196, "1080p": 0.483}
RATIOS = ("16:9", "9:16", "1:1", "4:3", "3:4", "21:9")
PACK_DIR = Path(__file__).resolve().parent


class SeedanceError(RuntimeError):
    """A request was refused locally or by the API, or a job ended without a video."""


def load_settings(env: dict | None = None, pack_dir: Path = PACK_DIR) -> dict:
    """Read the key and endpoint from the environment, then from <pack>/seedance.env.

    The key is never a node widget, so it never ends up in a saved workflow or in PNG metadata.
    """
    env = dict(os.environ if env is None else env)
    file_values = {}
    config = pack_dir / "seedance.env"
    if config.is_file():
        for line in config.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                name, value = line.split("=", 1)
                file_values[name.strip()] = value.strip().strip('"').strip("'")
    get = lambda name, default=None: env.get(name) or file_values.get(name) or default  # noqa: E731
    poll_seconds = float(get("SEEDANCE_POLL_SECONDS", "10"))
    if not 1 <= poll_seconds <= 60:
        raise SeedanceError("SEEDANCE_POLL_SECONDS must be between 1 and 60")
    return {
        "api_key": get("SEEDANCE_API_KEY"),
        "base_url": get("SEEDANCE_BASE_URL", DEFAULT_BASE_URL).rstrip("/"),
        "poll_seconds": poll_seconds,
    }


class SeedanceClient:
    def __init__(self, api_key: str | None, base_url: str = DEFAULT_BASE_URL, timeout: float = 120):
        if not api_key:
            raise SeedanceError("No API key: set SEEDANCE_API_KEY before starting ComfyUI, "
                                "or put SEEDANCE_API_KEY=... in seedance.env next to this pack")
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

    def _request(self, method: str, path: str, body: dict | None = None, raw: bool = False):
        data = json.dumps(body).encode() if body is not None else None
        request = urllib.request.Request(self.base_url + path, data=data, method=method)
        request.add_header("Authorization", f"Bearer {self.api_key}")
        request.add_header("User-Agent", USER_AGENT)
        if data is not None:
            request.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                payload = response.read()
        except urllib.error.HTTPError as error:
            detail = error.read().decode(errors="replace")
            try:
                message = json.loads(detail)["error"]["message"]
            except (ValueError, KeyError, TypeError):
                message = detail[:300]
            raise SeedanceError(f"{method} {path} returned HTTP {error.code}: {message}") from None
        except urllib.error.URLError as error:
            raise SeedanceError(f"{method} {path} failed: {error.reason}") from None
        return payload if raw else json.loads(payload)

    def upload(self, data: bytes, kind: str, content_type: str) -> str:
        """Upload bytes through POST /media/uploads and return the media_url (readable for 7 days)."""
        ticket = self._request("POST", "/media/uploads", {
            "model": MODEL, "type": kind, "content_type": content_type, "size_bytes": len(data)})
        put = urllib.request.Request(ticket["upload_url"], data=data, method="PUT")
        put.add_header("Content-Type", content_type)
        put.add_header("User-Agent", USER_AGENT)
        try:
            with urllib.request.urlopen(put, timeout=max(self.timeout, 300)):
                pass
        except (urllib.error.HTTPError, urllib.error.URLError) as error:
            if isinstance(error, urllib.error.URLError):
                raise SeedanceError(f"upload PUT failed: {error.reason}") from None
            raise SeedanceError(f"upload PUT returned HTTP {error.code}") from None
        return ticket["media_url"]

    def submit(self, body: dict) -> str:
        return self._request("POST", "/videos/generations", body)["request_id"]

    def status(self, request_id: str) -> dict:
        return self._request("GET", f"/videos/{quote(request_id, safe='-_')}")

    def content(self, request_id: str) -> bytes:
        return self._request("GET", f"/videos/{quote(request_id, safe='-_')}/content", raw=True)


def build_request(prompt: str, *, duration: int, resolution: str, aspect_ratio: str, generate_audio: bool,
                  images=(), videos=(), audios=(), start_image=None, end_image=None, edit_video=None) -> dict:
    """Build the JSON body; media arguments are already-uploaded HTTPS URLs. The mode follows from the media."""
    if not prompt or not prompt.strip():
        raise SeedanceError("Seedance 2.5 needs a prompt in every mode")
    if len(prompt.encode()) > 40_000:
        raise SeedanceError("the prompt is longer than 40,000 bytes")
    if resolution not in PRICES:
        raise SeedanceError("resolution must be 480p, 720p or 1080p")
    body = {"model": MODEL, "prompt": prompt, "resolution": resolution}
    if not generate_audio:
        body["generate_audio"] = False
    has_refs = bool(images or videos or audios)
    if edit_video:
        if has_refs or start_image or end_image:
            raise SeedanceError("an edit carries only the video it edits: disconnect refs and frames")
        body["omni_reference_task_type"] = "edit"
        body["reference_videos"] = [{"url": edit_video}]
        return body  # an edit keeps its source's length and ratio, so no duration or aspect_ratio
    if not 4 <= int(duration) <= 30:
        raise SeedanceError("duration must be 4-30 seconds")
    body["duration"] = int(duration)
    if end_image and not start_image:
        raise SeedanceError("last_frame needs first_frame")
    if start_image:
        if has_refs:
            raise SeedanceError("first/last frames cannot be combined with reference assets")
        body["aspect_ratio"] = "adaptive"  # Seedance 2.5 frame modes take their ratio from the first frame
        body["start_image"] = {"url": start_image}
        if end_image:
            body["end_image"] = {"url": end_image}
        return body
    if aspect_ratio not in RATIOS:
        raise SeedanceError("text and reference jobs take 16:9, 9:16, 1:1, 4:3, 3:4 or 21:9")
    body["aspect_ratio"] = aspect_ratio
    if len(images) > 30 or len(videos) > 10 or len(audios) > 10:
        raise SeedanceError("references are limited to 30 images, 10 videos and 10 audio clips")
    for key, urls in (("reference_images", images), ("reference_videos", videos), ("reference_audios", audios)):
        if urls:
            body[key] = [{"url": url} for url in urls]
    return body


def estimate(body: dict, reference_video_seconds: float = 0, edit_seconds: float = 0) -> tuple[float, float]:
    """Billed seconds and USD: output seconds plus reference-video seconds; an edit bills its source twice."""
    if body.get("omni_reference_task_type") == "edit":
        billed = round(2 * edit_seconds, 3)
    else:
        billed = body.get("duration", 5) + int(reference_video_seconds)
    return billed, round(billed * PRICES[body["resolution"]], 4)


def check_image(width: int, height: int, label: str) -> None:
    if min(width, height) < 300 or max(width, height) > 6000:
        raise SeedanceError(f"{label} is {width}x{height}; Seedance 2.5 takes 300-6000 px per side")
    if not 0.4 <= width / height <= 2.5:
        raise SeedanceError(f"{label} is {width}x{height}; width/height must be between 0.4 and 2.5")


def check_clip(seconds: float, label: str, low: float = 2, high: float = 30) -> None:
    if not low <= seconds <= high:
        raise SeedanceError(f"{label} is {seconds:.1f} s long; it must be {low:g}-{high:g} s")


def image_png_bytes(pixels) -> bytes:
    """One ComfyUI IMAGE frame (height x width x channels, floats 0-1) as PNG bytes."""
    import numpy as np
    from PIL import Image

    array = np.clip(np.asarray(pixels, dtype=np.float32) * 255.0, 0, 255).astype(np.uint8)
    if array.ndim == 3 and array.shape[2] == 4:
        array = array[:, :, :3]
    buffer = io.BytesIO()
    Image.fromarray(array).save(buffer, format="PNG")
    return buffer.getvalue()


def audio_wav_bytes(samples, sample_rate: int) -> bytes:
    """A ComfyUI AUDIO waveform for one clip (channels x samples, floats -1..1) as 16-bit PCM WAV bytes."""
    import numpy as np

    data = np.asarray(samples, dtype=np.float32)
    if data.ndim == 1:
        data = data[np.newaxis, :]
    pcm = (np.clip(data, -1.0, 1.0) * 32767).astype("<i2").T.copy()  # interleave channels
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as writer:
        writer.setnchannels(data.shape[0])
        writer.setsampwidth(2)
        writer.setframerate(int(sample_rate))
        writer.writeframes(pcm.tobytes())
    return buffer.getvalue()

"""Seedance 2.5 nodes for ComfyUI, written against the V3 node schema (comfy_api.latest)."""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import tempfile
import time
import re
from pathlib import Path

import comfy.model_management as model_management
import folder_paths
from comfy_api.latest import ComfyAPI, ComfyExtension, InputImpl, Types, io, ui

from .seedance_client import (PRICES, RATIOS, SeedanceClient, SeedanceError, audio_wav_bytes, build_request,
                              check_clip, check_image, estimate, image_png_bytes, load_settings)

log = logging.getLogger("seedance25")
api = ComfyAPI()
Refs = io.Custom("SEEDANCE_REFS")
CATEGORY = "Seedance 2.5"
OUTPUT_SUBFOLDER = "seedance"
KINDS = ("images", "videos", "audios")
LABELS = {"images": "Image", "videos": "Video", "audios": "Audio"}


def show(node_id, text: str) -> None:
    """Write a status line under the node in the ComfyUI frontend (and to the log)."""
    log.info("[seedance] %s", text)
    try:
        from server import PromptServer

        if node_id and getattr(PromptServer, "instance", None) is not None:
            PromptServer.instance.send_progress_text(text, node_id)
    except Exception:  # no server in headless or test runs
        pass


def numbering(refs: dict) -> str:
    parts = []
    for kind in KINDS:
        count = len(refs[kind])
        if count:
            label = LABELS[kind]
            parts.append(f"@{label} 1" if count == 1 else f"@{label} 1-{count}")
    return " · ".join(parts) or "no assets"


class Seedance25References(io.ComfyNode):
    @classmethod
    def define_schema(cls):
        return io.Schema(
            node_id="Seedance25References",
            display_name="Seedance 2.5 References",
            category=CATEGORY,
            description="Collects reference images, video and audio for a Seedance 2.5 job. Chain several to add "
                        "more; numbering continues along the chain, and the node shows the @ names its assets get.",
            inputs=[
                Refs.Input("previous", optional=True, tooltip="Another References node; its assets keep the lower numbers."),
                io.Image.Input("images", optional=True, tooltip="Each frame of the batch becomes the next @Image N (JPEG/PNG rules: 300-6000 px per side)."),
                io.Video.Input("video", optional=True, tooltip="Becomes the next @Video N. 2-30 s; its seconds are billed with the output."),
                io.Audio.Input("audio", optional=True, tooltip="Becomes the next @Audio N. 2-30 s; audio references are free."),
                io.String.Input("urls", multiline=True, default="", dynamic_prompts=False,
                                tooltip="Assets you already host, one per line: 'image: https://...', 'video: https://...' or 'audio: https://...'. They are numbered after this node's inputs."),
            ],
            outputs=[Refs.Output("refs")],
            hidden=[io.Hidden.unique_id],
        )

    @classmethod
    def execute(cls, urls: str = "", previous=None, images=None, video=None, audio=None) -> io.NodeOutput:
        refs = {kind: list(previous[kind]) if previous else [] for kind in KINDS}
        if images is not None:
            refs["images"] += [("image", frame) for frame in images]
        if video is not None:
            refs["videos"].append(("video", video))
        if audio is not None:
            refs["audios"].append(("audio", audio))
        for line in urls.splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            kind, _, url = line.partition(":")
            kind, url = kind.strip().lower(), url.strip()
            if kind not in ("image", "video", "audio") or not url.startswith("https://"):
                raise SeedanceError(f"cannot read {line!r}: write 'image: https://...', 'video: https://...' or 'audio: https://...'")
            refs[kind + "s"].append(("url", url))
        if len(refs["images"]) > 30 or len(refs["videos"]) > 10 or len(refs["audios"]) > 10:
            raise SeedanceError("Seedance 2.5 takes at most 30 images, 10 videos and 10 audio clips")
        show(cls.hidden.unique_id, numbering(refs))
        return io.NodeOutput(refs)


def _image_check(frame, label: str):
    height, width = int(frame.shape[0]), int(frame.shape[1])
    check_image(width, height, label)
    pixels = frame.cpu().numpy()
    return hashlib.sha256(pixels.tobytes() + repr(pixels.shape).encode()).hexdigest()


def _image_payload(frame, label: str):
    data = image_png_bytes(frame.cpu().numpy())
    if len(data) > 20 * 1024 * 1024:
        raise SeedanceError(f"{label} is larger than 20 MiB as PNG")
    return data, "image", "image/png"


def _video_check(video, label: str, low: float, high: float):
    seconds = float(video.get_duration())
    check_clip(seconds, label, low, high)
    width, height = video.get_dimensions()
    check_image(width, height, label)
    if not 409_600 <= width * height <= 8_295_044:
        raise SeedanceError(f"{label} is {width}x{height}; Seedance 2.5 takes 409,600-8,295,044 pixels per frame")
    digest = hashlib.sha256(repr(video.get_active_trim_window()).encode())
    source = video.get_stream_source()
    if isinstance(source, str):
        with open(source, "rb") as handle:
            for chunk in iter(lambda: handle.read(1 << 20), b""):
                digest.update(chunk)
    else:
        source.seek(0)
        digest.update(source.read())
        source.seek(0)
    return seconds, digest.hexdigest()


def _video_payload(video, label: str):
    with tempfile.TemporaryDirectory() as folder:
        path = os.path.join(folder, "clip.mp4")
        video.save_to(path, format=Types.VideoContainer.MP4, codec=Types.VideoCodec.H264)
        data = Path(path).read_bytes()
    if len(data) > 100 * 1024 * 1024:
        raise SeedanceError(f"{label} is larger than 100 MiB as MP4")
    return data, "video", "video/mp4"


def _audio_check(audio: dict, label: str):
    waveform = audio["waveform"][0]
    rate = int(audio["sample_rate"])
    seconds = waveform.shape[-1] / rate
    check_clip(seconds, label)
    samples = waveform.cpu().numpy()
    return seconds, hashlib.sha256(samples.tobytes() + repr((samples.shape, rate)).encode()).hexdigest()


def _audio_payload(audio: dict, label: str):
    data = audio_wav_bytes(audio["waveform"][0].cpu().numpy(), int(audio["sample_rate"]))
    if len(data) > 15_000_000:
        raise SeedanceError(f"{label} is larger than 15 MB as WAV")
    return data, "audio", "audio/wav"


def _ledger_path() -> Path:
    return Path(folder_paths.get_output_directory()) / OUTPUT_SUBFOLDER / "submitted.jsonl"


def _ledger_lookup(fingerprint: str):
    path = _ledger_path()
    if not path.is_file():
        return None
    found = None
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        if entry.get("fingerprint") == fingerprint:
            found = entry
    return found


def _ledger_add(fingerprint: str, request_id: str, body: dict) -> None:
    path = _ledger_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    entry = {"fingerprint": fingerprint, "request_id": request_id, "submitted_at": int(time.time()),
             "resolution": body.get("resolution"), "duration": body.get("duration")}
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(entry) + "\n")


class Seedance25Submit(io.ComfyNode):
    @classmethod
    def define_schema(cls):
        return io.Schema(
            node_id="Seedance25Submit",
            display_name="Seedance 2.5 Submit",
            category=CATEGORY,
            description="Uploads the connected media and submits one Seedance 2.5 job. The mode follows the inputs: "
                        "edit_video makes an edit, first_frame (and last_frame) a frame job, refs a reference job, "
                        "nothing a text job. Returns the request_id at once; Seedance 2.5 Result waits for the video. "
                        "Identical inputs with the same take reuse the earlier request_id instead of paying again.",
            inputs=[
                io.String.Input("prompt", multiline=True, default="", dynamic_prompts=False,
                                tooltip="Required in every mode. Name references as @Image 1, @Video 1, @Audio 1."),
                io.Int.Input("duration", default=5, min=4, max=30, tooltip="Seconds of output (4-30). Ignored for an edit, which keeps its source's length."),
                io.Combo.Input("resolution", options=list(PRICES), default="720p",
                               tooltip="Billed per second: 480p $0.0874, 720p $0.196, 1080p $0.483 at the default endpoint (2026-10-07)."),
                io.Combo.Input("aspect_ratio", options=list(RATIOS), default="16:9",
                               tooltip="Text and reference jobs only. Frame jobs follow the first frame; an edit keeps its source's ratio."),
                io.Boolean.Input("generate_audio", default=True, tooltip="Off renders a silent video."),
                io.Int.Input("take", default=0, min=0, max=100000,
                             tooltip="Not sent. The same inputs with the same take reuse the job already submitted (recorded in output/seedance/submitted.jsonl), so a re-queue never pays twice. Change it to render a new take."),
                Refs.Input("refs", optional=True, tooltip="From Seedance 2.5 References."),
                io.Image.Input("first_frame", optional=True, tooltip="Locks the opening frame (the first image of the batch is used)."),
                io.Image.Input("last_frame", optional=True, tooltip="Locks the closing frame; needs first_frame."),
                io.Video.Input("edit_video", optional=True, tooltip="The 4-30 s clip to edit. An edit bills the clip twice."),
            ],
            outputs=[io.String.Output("request_id"), io.Float.Output("estimated_usd")],
            hidden=[io.Hidden.unique_id],
        )

    @classmethod
    async def execute(cls, prompt, duration, resolution, aspect_ratio, generate_audio, take,
                      refs=None, first_frame=None, last_frame=None, edit_video=None) -> io.NodeOutput:
        refs = refs or {kind: [] for kind in KINDS}
        node = cls.hidden.unique_id
        placeholder = "https://placeholder.invalid/"
        build = dict(prompt=prompt, duration=duration, resolution=resolution, aspect_ratio=aspect_ratio,
                     generate_audio=generate_audio)
        # 1. Refuse impossible combinations before touching the network.
        build_request(**build, images=[placeholder] * len(refs["images"]), videos=[placeholder] * len(refs["videos"]),
                      audios=[placeholder] * len(refs["audios"]), start_image=placeholder if first_frame is not None else None,
                      end_image=placeholder if last_frame is not None else None,
                      edit_video=placeholder if edit_video is not None else None)

        # 2. Check every local asset against the Seedance 2.5 limits and fingerprint the whole request.
        digests, reference_seconds, audio_seconds, edit_seconds = [], 0.0, 0.0, 0.0
        for kind in KINDS:
            for index, (source, value) in enumerate(refs[kind]):
                label = f"@{LABELS[kind]} {index + 1}"
                if source == "url":
                    digests.append(value)
                elif kind == "images":
                    digests.append(_image_check(value, label))
                elif kind == "videos":
                    seconds, digest = _video_check(value, label, 2, 30)
                    reference_seconds += seconds
                    digests.append(digest)
                else:
                    seconds, digest = _audio_check(value, label)
                    audio_seconds += seconds
                    digests.append(digest)
        if reference_seconds > 30 or audio_seconds > 30:
            raise SeedanceError("reference videos and reference audio may each total at most 30 s")
        if first_frame is not None:
            digests.append(_image_check(first_frame[0], "first_frame"))
        if last_frame is not None:
            digests.append(_image_check(last_frame[0], "last_frame"))
        if edit_video is not None:
            edit_seconds, digest = _video_check(edit_video, "edit_video", 4, 30)
            digests.append(digest)
        fingerprint = hashlib.sha256(json.dumps({**build, "take": take, "media": digests,
                                                 "slots": {k: len(refs[k]) for k in KINDS},
                                                 "frames": [first_frame is not None, last_frame is not None],
                                                 "edit": edit_video is not None}, sort_keys=True).encode()).hexdigest()

        settings = load_settings()
        client = SeedanceClient(settings["api_key"], settings["base_url"])
        shape = build_request(**build, images=[placeholder] * len(refs["images"]), videos=[placeholder] * len(refs["videos"]),
                              audios=[placeholder] * len(refs["audios"]),
                              start_image=placeholder if first_frame is not None else None,
                              end_image=placeholder if last_frame is not None else None,
                              edit_video=placeholder if edit_video is not None else None)
        billed, usd = estimate(shape, reference_seconds, edit_seconds)
        unknown = " + URL reference clips" if any(source == "url" for source, _ in refs["videos"]) else ""

        # 3. Same inputs and take as an earlier job that is still pending or done: reuse it.
        previous = _ledger_lookup(fingerprint)
        if previous:
            try:
                state = (await asyncio.to_thread(client.status, previous["request_id"])).get("status")
            except SeedanceError as error:
                raise SeedanceError(
                    f"cannot confirm the earlier request {previous['request_id']}; "
                    f"it was not submitted again to avoid a duplicate charge: {error}"
                ) from error
            if state in ("pending", "done"):
                show(node, f"{previous['request_id']} (reused, {state}; change take for a new one)")
                return io.NodeOutput(previous["request_id"], usd)
            if state in ("failed", "expired"):
                raise SeedanceError(
                    f"{previous['request_id']} is {state}; change take if a new generation is intentional"
                )
            raise SeedanceError(f"{previous['request_id']} returned unknown status {state!r}; not resubmitted")

        # 4. Upload local media, then submit.
        payloads, uploads, frames = [], {kind: [] for kind in KINDS}, {}
        for kind in KINDS:
            for index, (source, value) in enumerate(refs[kind]):
                label = f"@{LABELS[kind]} {index + 1}"
                if source == "url":
                    uploads[kind].append(value)
                    continue
                uploads[kind].append(None)
                if kind == "images":
                    payloads.append((kind, index, lambda value=value, label=label: _image_payload(value, label)))
                elif kind == "videos":
                    payloads.append((kind, index, lambda value=value, label=label: _video_payload(value, label)))
                else:
                    payloads.append((kind, index, lambda value=value, label=label: _audio_payload(value, label)))
        if first_frame is not None:
            payloads.append(("start_image", 0, lambda: _image_payload(first_frame[0], "first_frame")))
        if last_frame is not None:
            payloads.append(("end_image", 0, lambda: _image_payload(last_frame[0], "last_frame")))
        if edit_video is not None:
            payloads.append(("edit_video", 0, lambda: _video_payload(edit_video, "edit_video")))
        for number, (slot, index, encode) in enumerate(payloads, start=1):
            data, kind, content_type = encode()
            show(node, f"uploading {number}/{len(payloads)} ({len(data) / 1e6:.1f} MB)")
            url = await asyncio.to_thread(client.upload, data, kind, content_type)
            if slot in KINDS:
                uploads[slot][index] = url
            else:
                frames[slot] = url

        body = build_request(**build, images=uploads["images"], videos=uploads["videos"], audios=uploads["audios"],
                             start_image=frames.get("start_image"), end_image=frames.get("end_image"),
                             edit_video=frames.get("edit_video"))
        request_id = await asyncio.to_thread(client.submit, body)
        _ledger_add(fingerprint, request_id, body)
        show(node, f"{request_id}\n≈ {billed} s × ${PRICES[resolution]} = ${usd:.2f}{unknown}")
        return io.NodeOutput(request_id, usd)


class Seedance25Result(io.ComfyNode):
    @classmethod
    def define_schema(cls):
        return io.Schema(
            node_id="Seedance25Result",
            display_name="Seedance 2.5 Result",
            category=CATEGORY,
            description="Waits for a Seedance 2.5 job, downloads the MP4 to output/seedance/ and returns it as VIDEO. "
                        "Paste a request_id to collect a job submitted earlier.",
            inputs=[
                io.String.Input("request_id", default="", tooltip="From Seedance 2.5 Submit, or typed in."),
                io.Int.Input("max_wait_minutes", default=60, min=1, max=1440,
                             tooltip="Stop waiting after this long. The job keeps running; queue again to resume."),
            ],
            outputs=[io.Video.Output("video"), io.String.Output("file_path"), io.Int.Output("billed_seconds"),
                     io.String.Output("usage_json")],
            hidden=[io.Hidden.unique_id],
            is_output_node=True,
        )

    @classmethod
    async def execute(cls, request_id, max_wait_minutes) -> io.NodeOutput:
        request_id = request_id.strip()
        if not request_id:
            raise SeedanceError("connect Seedance 2.5 Submit or paste a request_id")
        settings = load_settings()
        client = SeedanceClient(settings["api_key"], settings["base_url"])
        node = cls.hidden.unique_id
        deadline = time.monotonic() + max_wait_minutes * 60
        while True:
            job = await asyncio.to_thread(client.status, request_id)
            state = job.get("status")
            if state not in ("pending", "done", "failed", "expired"):
                raise SeedanceError(f"{request_id} returned unknown status {state!r}")
            if state == "done":
                break
            if state in ("failed", "expired"):
                error = job.get("error") or {}
                hint = "safe to submit again" if error.get("retryable") else "fix the input first"
                raise SeedanceError(f"{request_id} {state}: {error.get('code', 'unknown')}: "
                                    f"{error.get('message', 'no detail')} ({hint}; change take on "
                                    "Seedance 2.5 Submit to send a new job; failed jobs are not billed)")
            progress = int(job.get("progress") or 0)
            remaining = job.get("estimated_seconds_remaining")
            show(node, f"{state} {progress}%" + (f", about {remaining} s left" if remaining else ""))
            await api.execution.set_progress(value=progress, max_value=100, node_id=node)
            if time.monotonic() > deadline:
                raise SeedanceError(f"{request_id} is still {state} after {max_wait_minutes} min; "
                                    "it keeps running, so queue this node again later")
            wake = time.monotonic() + settings["poll_seconds"]
            while time.monotonic() < wake:
                if model_management.processing_interrupted():
                    show(node, f"stopped waiting; {request_id} keeps running on the server")
                    model_management.throw_exception_if_processing_interrupted()
                await asyncio.sleep(0.25)

        folder = Path(folder_paths.get_output_directory()) / OUTPUT_SUBFOLDER
        folder.mkdir(parents=True, exist_ok=True)
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,200}", request_id):
            raise SeedanceError("request_id contains unsupported filename characters")
        target = folder / f"{request_id}.mp4"
        if not target.is_file():
            data = await asyncio.to_thread(client.content, request_id)
            partial = target.with_suffix(".part")
            partial.write_bytes(data)
            partial.replace(target)
        usage = job.get("usage") or {}
        await api.execution.set_progress(value=100, max_value=100, node_id=node)
        show(node, f"done · billed {usage.get('billed_seconds', '?')} s")
        return io.NodeOutput(InputImpl.VideoFromFile(str(target)), str(target), int(usage.get("billed_seconds") or 0),
                             json.dumps(usage),
                             ui=ui.PreviewVideo([ui.SavedResult(target.name, OUTPUT_SUBFOLDER, io.FolderType.output)]))


class SeedanceExtension(ComfyExtension):
    async def get_node_list(self) -> list[type[io.ComfyNode]]:
        return [Seedance25References, Seedance25Submit, Seedance25Result]


async def comfy_entrypoint() -> ComfyExtension:
    return SeedanceExtension()

# Seedance 2.5 ComfyUI custom nodes

A small ComfyUI extension for submitting Seedance 2.5 jobs, tracking the
server-side result, and keeping reference assets visible in the graph. The
node pack is useful when a visual workflow is easier to review than a script:
connect an image, video, or audio reference, submit one job, and collect the
finished MP4 without putting an API key into a workflow JSON file. The backend
can be reached through [ArgoLink](https://argolink.io/gh-seedance-2-5-comfyui-workflow),
but the node deliberately keeps its base URL and key configurable.

Last checked: 2026-10-07.

## What is included

| Node or file | Responsibility |
|---|---|
| `Seedance25References` | Collects ComfyUI image/video/audio inputs and assigns stable `@Image N`, `@Video N`, and `@Audio N` labels. |
| `Seedance25Submit` | Uploads connected media, validates the selected mode, submits one job, and returns `request_id` plus an estimate. |
| `Seedance25Result` | Polls `pending` jobs, downloads `/content` after `done`, and saves a resumable MP4 under ComfyUI's output directory. |
| `seedance_client.py` | Standard-library HTTP client, media upload ticket handling, request validation, and local cost estimation. |
| `workflows/` | Five importable examples for text, first-frame, reference, edit, and repeated-take workflows. |

The current V3 loader invokes `comfy_entrypoint`; this pack intentionally does
not advertise a legacy V1 registry because the V3 schema and V1 `INPUT_TYPES`
contract are different. ComfyUI V3 loads the `ComfyExtension` returned by that
entry point.

## Install

From the ComfyUI `custom_nodes` directory:

```bash
git clone https://github.com/evangelinesnow/seedance-2-5-comfyui-workflow.git
cd seedance-2-5-comfyui-workflow
# ComfyUI supplies Pillow and numpy; install them only if your distribution is missing either dependency.
```

Restart ComfyUI after the clone. Set the key in the process environment or in
a local, ignored `seedance.env` beside the pack:

```bash
export SEEDANCE_API_KEY='replace-me'
export SEEDANCE_BASE_URL='https://argolink.io/v1'
```

The key is never a node input, workflow field, PNG metadata value, or committed
file. The client sends an explicit User-Agent because the public edge rejects
Python's default `urllib` identity.

## The three-node flow

1. Add **Seedance 2.5 References** when the job needs assets. A connected batch
   is numbered in input order, so prompts can say `@Image 1` or `@Video 1`.
2. Add **Seedance 2.5 Submit**. Choose `resolution`, `duration`, ratio, and
   `generate_audio`; connect either references, first/last frames, or one edit
   video. The node uploads media through `POST /v1/media/uploads`, then calls
   `POST /v1/videos/generations` and returns the `request_id` immediately.
3. Add **Seedance 2.5 Result**. It polls `GET /v1/videos/{request_id}` until
   the status is `done`; only then does it download
   `GET /v1/videos/{request_id}/content`. A `failed` or `expired` job raises a
   useful error and is not silently submitted again.

A stopped ComfyUI run does not cancel a server job. Reconnect the result node
with the saved request ID, or change the Submit node's `take` value only when a
new generation is intentional. A JSONL ledger in `output/seedance/` prevents
identical retries from charging twice.

## Model limits and price snapshot

| Mode | Inputs | Ratio | Duration |
|---|---|---|---|
| Text | prompt | `21:9`, `16:9`, `4:3`, `1:1`, `3:4`, `9:16` | 4–30 s |
| First frame / first + last frame | 1 / 2 images | `adaptive` (taken from the frame) | 4–30 s |
| Multimodal reference | up to 30 images, 10 videos, 10 audio clips | fixed ratios above | 4–30 s |
| Edit | one 4–30 s video | source video keeps its ratio | source duration |

| Output tier | ArgoLink, per output second | Official reference, per output second |
|---|---:|---:|
| 480p | $0.0874 | $0.1028 |
| 720p | $0.196 | $0.2311 |
| 1080p | $0.483 | $0.5686 |

For context, the two other public providers whose Seedance 2.5 pages were
checked on the same date list these output-second rates: [BytePlus ModelArk]
(https://docs.byteplus.com/en/docs/modelark/model-pricing), $0.1028 / $0.2311 /
$0.5686, and [fal](https://fal.ai/models/bytedance/seedance-2.5/text-to-video),
$0.2205 / $0.473 / $1.164. These are reference prices, not a promise that
their quotas, taxes, or account tiers match this node pack. Reference video
seconds can change the effective total, so compare the complete request rather
than only the first cell in a pricing table. ArgoLink is a routing option in
the examples; this extension does not require that service and its endpoint is
configurable.

The ArgoLink values and the official comparison were checked on the [model
page](https://argolink.io/en/models/seedance-2.5) on 2026-10-07. A reference
video adds its seconds to billed seconds; failed, rejected, and expired jobs
are not charged. The account concurrency limit is 50 Seedance 2.5 jobs.

## Troubleshooting

- **401**: check `SEEDANCE_API_KEY`; do not put it in a workflow export.
- **429**: slow down submissions and respect the returned rate-limit details.
- **503**: keep the `request_id` and retry the status call after `Retry-After`;
  do not create a duplicate job automatically.
- **first/last frame rejects the ratio**: leave the node at `adaptive`.
- **audio reference is rejected**: check the current model contract and the
  total reference count; audio-only references are supported by the current
  Seedance 2.5 contract, so do not add an unrelated visual asset as a workaround.
- **ComfyUI cannot import the pack**: check the server log and make sure the
  V3 ComfyUI build and its bundled Pillow/numpy versions are installed.

## API contract used by the client

The client deliberately keeps the wire contract small. A submit body includes
`model`, `prompt`, `duration`, `resolution`, `aspect_ratio`, and optional
`generate_audio`; local media is first exchanged for a short upload ticket and
the returned `media_url` is placed in `reference_images`, `reference_videos`,
`reference_audios`, `start_image`, `end_image`, or the edit video field. The
result node reads only `pending`, `done`, `failed`, and `expired`. It does not
reinterpret an unknown server value as success, and it never starts another
billable job after a network error while checking a saved request. A 429 or a
503 should be handled by keeping the request ID and trying the status call
again after the server's retry hint. A 409 content response means the job is
not ready; a 424 means the job failed. Failed and expired jobs are not charged.

The JSONL ledger is an operational safety feature rather than a remote billing
guarantee: it records the request ID only after the submit response is received.
If the process stops before that response, inspect the provider dashboard before
submitting a second job. The `take` field is local bookkeeping; it is not sent
to the API and changing it is an explicit request for a new generation.

## Tests

The offline tests exercise request construction, frame-mode restrictions,
reference caps, the retry ledger, and the User-Agent on both JSON and upload
requests. They never contact the service:

```bash
python -m pytest -q
```

The five workflow JSON files are examples, not test fixtures with a secret.
Before sharing an export, remove any local `seedance.env` file and any returned
media URL that may contain a private token.

The JSON files use ComfyUI's API workflow format (the object keyed by node ID,
with `class_type` and `inputs`). Import them through the API/workflow loader or
copy the node graph into the UI; the `LoadImage` and `LoadVideo` entries are
deliberate placeholders that must be replaced by files in your ComfyUI input
directory. They are not claims that a remote asset is bundled with this pack.

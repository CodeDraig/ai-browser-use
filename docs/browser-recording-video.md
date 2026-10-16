# Record a browser workflow and export a video

The `browser-use` and `browser-harness` commands use the same implementation.
Recording is opt-in and works with direct CDP and the Chrome/Firefox extension
backends. The SDK's continuous browser video recorder remains a separate API.

## Capture the actual workflow

```sh
browser-use <<'PY'
new_tab("https://example.com")
recording = start_recording("example-research", title="Example research")
print(recording)
# Perform and verify the requested browser actions here.
stop_recording()
PY
```

Retain the exact directory returned by `start_recording`. An existing recording
name is rejected so earlier captures cannot be replaced. Recordings contain
`meta.json`, `events.jsonl`, and JPEG frames, under the existing harness workspace.
The same helpers work through `browser-use --cli-mcp`.

For opt-in automatic recording use `browser-use recordings enable`; disable it
with `browser-use recordings disable`. `BH_RECORD=1` or `BH_RECORD=0` overrides
that preference for a process. Read-only inspection does not create action
frames. Nested helper calls produce one frame for the outer public action.

The recorder gathers screenshots, viewport information, and focus geometry.
Password action text and credential query parameters are masked; screenshots
still require review. A headless Chromium tab that has never been visible can
defer rendering: use `activate_tab(target)` if that browser pauses screenshots
while hidden. Tab switching otherwise preserves the visible tab by default.

## Prepare and review an edit

```sh
browser-use video init /absolute/path/to/the-recording
```

Inspect `recording-summary.json`, choose the numbered events that show the
workflow, and create `edit-brief.json` in that directory. A minimal brief is:

```json
{
  "task": "Research a source",
  "summary": "Read the source and preserve the evidence",
  "plan": ["Read source", "Preserve evidence"],
  "actions": [
    {
      "event": 2,
      "chapter": 0,
      "route": "Research",
      "narration": "Enter the research query"
    }
  ],
  "outcomes": ["Source evidence preserved"],
  "privacy": {
    "reviewedFrames": ["0002.jpg"],
    "redact": {}
  }
}
```

Event numbers are 1-based indices into the summary, not raw source line numbers.
Use the actual event and frame names from your recording. `chapter` indexes the
plan starting at zero. `route` is a descriptive label, not a URL. Review every
used frame and declare any redaction rectangles under `privacy.redact` before
export. The compiler rejects missing privacy review coverage.

```sh
browser-use video review /absolute/path/to/the-recording
```

Review the generated contact sheet, full-resolution privacy images, and renderer
report. Review checks normal and reduced-motion rendering. It records content
hashes so a later change to the edit or sources requires another review.

## Render an MP4

The renderer needs Chromium with CDP plus local `ffmpeg` and `ffprobe`. When the
task uses an extension backend, supply an independently managed local Chromium
endpoint for rendering:

```sh
export BH_VIDEO_CDP_URL=http://127.0.0.1:9222
browser-use video review /absolute/path/to/the-recording
browser-use video export /absolute/path/to/the-recording --reviewed
```

`BH_VIDEO_CDP_URL` also works when the task uses direct CDP. Rendering uses a
separate daemon/runtime, disables recording of the renderer, and preserves the
task's backend selection. A Firefox recording can be rendered this way without
requiring arbitrary JavaScript or CDP in Firefox. Use a local renderer because
the local artifact server and download destination are on the harness machine.

Export refuses to replace existing output files. Set `--output another.mp4` to
make a new export. The exporter verifies the review hashes, duration, decoding,
and sampled output frames. Source JPEGs, metadata, and events remain in place.

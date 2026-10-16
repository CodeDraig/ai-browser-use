# Consolidated harness validation — 2026-10-16

The first-party `browser_harness` package combines the former bundled harness
with the extension, transfer, managed-operation, and research modules from the
local `browser-harness` checkout. Both CLI names use that package. The previous
vendored source/build copies are outside the active tree, preserved in the
backup below. No cloud, proxy bootstrap, or telemetry was added to this fork.

## Evidence locations

- Backup: `/srv/data/code/ai-browser-use-consolidation-backup-20261016T223405Z/`.
- Logs, profiles, manifests, recordings, videos, distributions, and receipts:
  `/srv/data/code/ai-browser-use-validation-20261016T223806Z/`.
- Original-checkout receipt:
  `original-browser-harness-preservation.json` in the backup directory.
- The original `/srv/data/code/browser-harness` checkout was read only. All
  **5,601 files** matched before/after contents, symlink targets, and modes.
  Its untracked live-test reports and ignored files were included in that check.
- The existing `ai-browser-use/uv.lock` also matched its pre-change backup.

These absolute locations describe this local run. They are not included in a
clone. The executable probes and regression tests are included in this repository.

## Regression, packaging, and static checks

The focused Python suite passed **140 tests**, covering the imported harness behavior,
recording, combined interfaces, CLI and skill installation, wheel/source
packaging, local-only repository guards, and MCP annotations. The exact final
count is in `complete-python-tests.log` in the evidence directory.

```sh
PYTHONPATH=. PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 .venv/bin/python -m pytest \
  tests/harness/unit \
  tests/ci/test_browser_use_cli.py \
  tests/ci/test_browser_use_skill_install_docs.py \
  tests/ci/test_local_distribution_boundaries.py \
  tests/ci/security/test_repository_security_guards.py \
  tests/ci/test_mcp_tool_annotations.py \
  -p pytest_asyncio.plugin -p pytest_timeout \
  -o addopts= -o log_cli=false -q --tb=short -p no:cacheprovider

node --test tests/harness/extension/*.test.cjs
python scripts/sync_browser_harness_skill.py --check
```

The JavaScript suite passed **23 tests**, including fixed recording-context
metadata and a regression that forces download updates to race native-host
startup. Ruff checks, Python AST parsing, TOML/JSON parsing, and `git diff --check`
passed. Scoped Ruff exceptions retain the imported compact statements and
intentional public helper exports without broadly reformatting those files.

`uv build --wheel --sdist` produced a distribution containing both console
scripts, one harness package, Chrome/Firefox assets, research code, and video
templates. The wheel was installed into a separate local environment. Imports
and command smoke tests ran outside the checkout; add-on export worked for
both browsers. The final wheel receipt checks packaged source bytes against
the working implementation. See `release-wheel-smoke.json` and `release-artifacts/`.

## Live browser workflows

Actual Linux x86-64 browsers were used: **Chrome for Testing 148.0.7778.96** and
**Firefox 156.0.1**. Selenium prepared disposable profiles and authenticated
them to a loopback fixture before the extension connected. No existing user
profile or real account was used. Firefox ran from a copy of the original
checkout's browser binary, leaving that checkout untouched.

Both extension backends passed:

- Native-host connection and reading an existing authenticated fixture session.
- Form filling/search, page reading, screenshots, explicit tab selection,
  new-tab readiness, and an offscreen selector click.
- A **3,080,192-byte** tracked download, bridge transfer, independent content
  comparison and SHA-256 verification.
- Research page captures and source-download manifests with verified artifacts.
- Action recording with viewport metadata and decodable JPEG frames.
- Broker restart, extension reload, download-registry retention, and cancellation.

Chrome additionally passed a complete browser process restart, retaining login,
connection identity, and tracked downloads. Receipts are
`chrome-recovery-fixed-result.json` and `firefox-recovery-result.json`.

Reproduction entry point:

```sh
python tests/harness/integration/extension_live_probe.py chrome \
  --binary /path/to/chrome --harness-python /absolute/path/to/python \
  --source-root /absolute/path/to/ai-browser-use \
  --output /new/isolated/output --research-checks --recording-checks --recovery
```

Use `firefox` and its binary for Firefox. The runner needs Selenium and Pillow;
they can be supplied with a temporary `uv run --no-project --with ...` environment.
Keep driver caches and output in a task-local directory. The probe closes its
browsers and daemons and retains evidence/profile files for inspection.

## CDP, MCP, research extraction, and video

`tests/harness/integration/combined_live_probe.py` passed an independent local
Chromium/CDP workflow containing form interaction, a managed JavaScript
operation, page evidence capture, a hash-verified PNG download, and actual
Tesseract OCR. It also connected a real stdio MCP client to
`browser-use --cli-mcp`, executed navigation/form/recording helpers, and received
an image screenshot.

The existing renderer reviewed and exported recordings made through direct CDP,
Chrome extension, and Firefox extension control. Output MP4s were approximately
10.6–10.7 seconds, with ffprobe metadata, ffmpeg decoding, and output-frame checks.
Generated review contact sheets were visually inspected. Source event, metadata,
and JPEG hashes remained unchanged. `combined-final/result.json` records the
uncorrected final CDP/MCP/OCR run and the CDP/Firefox MP4s; `combined/result.json`
also records the earlier Chrome-extension video export.

The renderer was given its own local Chromium/CDP session through
`BH_VIDEO_CDP_URL`, with separate runtime paths and recording disabled. It did
not replace the task backend. Its browser process and both owned daemons were
stopped afterward; the receipt records browser shutdown. An independent process
check found no owned browser, native-host, or harness-daemon processes remaining
(`cleanup.json`).

## Corrections made during verification

- The first Firefox setup attempt clicked before the popup handler was ready.
  The probe now waits for that handler before clicking. Successful subsequent
  runs did not require a programmatic connection fallback.
- The first CDP recording check encountered a never-activated headless tab that
  deferred screenshots. It completed after explicit activation. The final
  reproducible probe activates its owned tab before recording and needs no
  intervention. Ordinary tab switching still defaults to background operation.
- Chrome restart exposed a real handshake-ordering race: a download registry
  could be sent before `hello`, causing the native host to exit. A test
  reproduced `downloads` arriving first. The extension now holds those messages
  until it sends `hello`, then reconciles the full registry. The regression and
  complete Chrome restart workflow passed after the fix.
- Imported tests that assumed cloud-update functions were adjusted to the
  existing local-only contract. A PID-reuse test now supplies explicit process
  fingerprints instead of accidentally depending on a real host PID.

## Limits

This validates local Linux fixtures and artifact production. It does not renew
the historical real-site acceptance claims from the standalone project, test
SSH transport, or establish native Windows/macOS acceptance. Firefox input
remains synthetic; arbitrary JavaScript and raw CDP remain unsupported there.
Video review/export still requires a local Chromium/CDP renderer plus ffmpeg
and ffprobe. Poppler/Tesseract are optional external extraction tools. The
separate SDK continuous-video implementation was retained and was not modified
or exercised by these harness checks. No full provider/LLM test suite ran.

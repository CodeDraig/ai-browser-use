# Logged-in Chrome and Firefox sessions

The extension backend connects to your existing browser profile. It does not launch a replacement browser, copy cookies, or require a remote-debugging port. All ordinary HTTP(S) tabs in a connected profile are available; private windows and browser-internal pages are excluded. This does not guarantee that any particular site accepts automated interactions.

## Local setup

Use the Python environment in which browser-harness is installed. Python 3.11 or newer is required. Run setup as your normal user, not as root.

```sh
browser-harness extension configure
browser-harness extension package chrome ./chrome-addon
browser-harness extension package firefox ./firefox-addon
```

Opening `popup.html` directly does not install the extension.

For Chrome, open `chrome://extensions`, enable Developer mode, choose **Load unpacked**, and select `chrome-addon`. Copy the displayed extension ID, then register its native host:

```sh
browser-harness extension install-host chrome --extension-id YOUR_EXTENSION_ID
```

For Firefox, register the native host, then open `about:debugging#/runtime/this-firefox`, choose **Load Temporary Add-on**, and select `firefox-addon/manifest.json`:

```sh
browser-harness extension install-host firefox
```

Firefox temporary installation must be repeated after a browser restart. For a custom Chrome user-data directory, add `--manifest-dir /path/to/user-data/NativeMessagingHosts` when installing or removing its host registration. Chrome packages target Manifest V3; Firefox requires version 128 or newer. Grant the extension access to website data when the browser requests it. Firefox's full screenshot API requires the manifest's broad host permission, but the adapter permits only normal HTTP(S) tabs.

Start the broker with a dedicated instance name:

```sh
export BH_BACKEND=extension BU_NAME=research
browser-harness extension list
```

Click **Connect** in the extension popup. The popup shows the profile connection ID and the local bridge endpoint. Configure the endpoint using the CLI, not a web page. The profile label is editable. **Disconnect** releases browser control immediately. An extension background restart reconnects a previously connected profile; explicitly disconnect to prevent that.

```sh
browser-harness extension list
browser-harness extension select CONNECTION_ID
browser-harness <<'PY'
print(browser_capabilities())
print(list_tabs())
PY
```

Choose a tab ID from that output. No automatic selection occurs when multiple tabs are available.

```sh
browser-harness <<'PY'
switch_tab('TAB_ID')
print(read_page())
fill_input('#search', 'Cleveland Torso Murders')
click_element('button[type=submit]')
print(page_info())
print(capture_screenshot('/tmp/research.png'))
PY
```

`read_page(frame_id=...)` and `click_element(selector, frame_id=...)` address a frame explicitly. Page snapshots include frame IDs and whether each frame is accessible. Selectors must match one element. Cross-origin frames still require browser-granted site permission. Firefox events are synthetic; browser shortcuts and controls requiring trusted input may not respond. Arrow/Escape events are dispatched but are not OS keyboard input.

## Downloads

Enable tracking before clicking a site's export/download control. Tracking covers new downloads in the connected profile, not its historical download list. Browser download prompts remain under browser/user control. Existing configured download directories are used; arbitrary directory/context overrides are rejected.

```python
cursor = enable_downloads()['cursor']
click_element('#download')
record = wait_for_download(after=cursor, timeout=60)
receipt = save_download(record['download_id'], '/path/to/research/')
print(receipt)  # local path, size, SHA-256, opaque connection-qualified ID
```

If several downloads match, select an explicit ID from `list_downloads(after=cursor)`. Completion tracking survives extension background restarts. A download ID belongs to its original profile: selecting another profile does not reinterpret it. File retrieval is restricted to completed downloads registered by the extension, streamed in chunks, and checked against size and SHA-256 before the final local file is created. The bridge cannot read an arbitrary supplied filesystem path.

## Remote harness through SSH

Install browser-harness and the native host on the **browser machine**, using the same account that runs the browser. Run the broker on the **harness machine**. Transfer a pairing token privately once; never put it in a URL or command-line argument.

On the harness machine:

```sh
browser-harness extension configure --port 8766 --export-token ./pairing-token
export BH_BACKEND=extension BU_NAME=research
browser-harness extension list
```

Copy that token file to the browser machine through SSH/SCP, then configure there:

```sh
browser-harness extension configure --port 8766 --token-file ./pairing-token
ssh -N -o ExitOnForwardFailure=yes -L 127.0.0.1:8766:127.0.0.1:8766 HARNESS_HOST
```

The native host connects to its local forwarded port. The broker binds only loopback, uses mutual challenge-response authentication without transmitting the pairing secret, and rejects web origins. Choose a different local port with `configure --port` and adjust the left side of `-L` if needed. Keep the SSH tunnel running, then click **Connect** in the browser extension. Treat pairing files as credentials and remove transferred copies when no longer needed. Disconnect the extension before reconfiguring; restart the named daemon after changing its broker configuration.

## Capabilities and recovery

| Capability | Chrome extension | Firefox extension |
|---|---|---|
| Existing-profile tabs and navigation | Yes | Yes |
| Page text, links, controls, frames | Yes | Yes |
| Click/type/forms/scroll | Debugger and DOM operations | Synthetic DOM operations |
| Viewport screenshots | Yes | Yes |
| Full-page screenshot | Yes | Unsupported |
| Tracked downloads and verified transfer | Yes | Yes |
| Arbitrary `js()` | Yes | Unsupported |
| Raw CDP | Selected-tab DOM/Runtime/Input/Page/Accessibility subset | Unsupported |
| Uploads, managed jobs, network-idle, native dialogs | Outside extension v1 | Outside extension v1 |

Direct CDP remains available when `BH_BACKEND` is unset. `http_get()` always requests directly from the local process and does not inherit browser login state. Extension mode suppresses CDP discovery. This fork has no cloud provisioning or proxy bootstrap. Use distinct `BU_NAME` values for different backends.

Navigation, tab closure, debugger detachment, timeout, and bridge loss return errors instead of retargeting or replaying a command. A timed-out mutation may have completed: inspect the page/download list before trying it again. After Chrome detaches a debugger, explicitly call `switch_tab()` to reattach. After a browser connection is lost, select a tab again. `extension status` reports connections, endpoint, configuration path, and native-host registration paths without printing credentials; extension-mode `--doctor` uses these bridge diagnostics.

Remove native registration with `browser-harness extension remove-host chrome` or `... firefox`, then remove the add-on in the browser. This leaves configuration, downloaded files, and shared launcher files intact. Windows registration uses HKCU; macOS and Linux use per-user native host manifests. Snap/Flatpak confinement can prevent native-host discovery and is not bypassed by this installer.

## Development verification

```sh
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python -m pytest tests/harness -o addopts= -q
node --test tests/harness/extension/*.test.cjs
```

`tests/harness/integration/extension_live_probe.py` is an opt-in Selenium fixture check with isolated profiles and native-host registrations. Pass `chrome` or `firefox`, `--binary`, `--harness-python`, and `--output`. Use `--recovery` to exercise broker/extension restart and cancellation; `--remote-host` targets an explicitly prepared remote harness fixture. It authenticates to a local fixture before connecting the extension, then performs the research workflow through the actual harness CLI. Selenium and browser drivers are test-only dependencies. A fixture login is not evidence of Newspapers.com acceptance. See the validation receipt for actual tested platforms and remaining live checks.

## Research evidence and local extraction

The popup manages the connection. Agents and scripts use these helpers on the selected tab:

```python
capture = capture_research("./research/source-001", metadata={"topic": "Cleveland Torso Murders"})
# After a user/site export has completed and an explicit download ID is known:
source = {"url": "https://www.newspapers.com/image/1164744664/",
          "title": "The Cleveland Press", "issue_date": "1936-09-12", "page": 1}
receipt = save_research_download(download_id, "./research/download-001", source=source)
extraction = extract_document("./research/download-001/original/scan.jpg", "./research/text-001")
```

Capture and download destinations must be new directories. Each contains `manifest.json` (schema version 1), `RECEIPT.md`, and hashed artifacts. Capture includes visible page text and controls, not OCR of a newspaper image. A navigation race returns `status="incomplete"`; check status before using artifacts. Captures redact common signed-link credential parameters. Caller annotations and supplied citation fields remain separate from observed page metadata.

`extract_document(path, destination, *, method="auto", languages="eng", pages=None, max_pages=50, timeout=300)` preserves its source. Pages are 1-based. `auto` uses embedded PDF text on each page and OCR when that text is empty; `text` extracts PDF text only; `ocr` forces OCR. OCR is explicitly uncorrected. PDF reading order and OCR accuracy require checking against the original.

Install optional **Tesseract** (and language data) plus **Poppler** (`pdfinfo`, `pdftotext`, `pdftoppm`) on the harness machine, not necessarily the browser machine. No software is installed automatically. On missing tools or partial failure, the helper returns an incomplete manifest with the reason. Successful page outputs survive failure and are hash-checked when resuming with the same source, destination, and settings. Limits are per invocation; rerun to continue, or explicitly raise `max_pages`/`timeout`. Tool execution uses argument lists without a shell. Source files and generated derivatives remain separate.

## Performance, readiness, and upgrading

`new_tab(http_url)` waits up to 20 seconds for an ordinary document to commit; timeout includes the created tab ID and never creates a replacement. Explicit redirects to restricted pages fail. Clicks wait up to five seconds for a visible, enabled, stable, unobstructed target before dispatch, and are never automatically repeated. Firefox retains synthetic-event limitations. Mutation acknowledgement does not imply that the site completed its action: inspect the resulting page or tracked download.

`read_page(frame_id=0, include_frames=False)` skips frame enumeration for repeated reads. Default behavior retains frame descriptions; probes use at most four concurrent frame operations. Helpers initialize once per document/frame and are invalidated on navigation.

Updated peers negotiate incremental download registries and transfer chunks up to 192 KiB; older peers retain full-registry updates and 48 KiB chunks. Interrupted downloads are distinct from user-canceled downloads. Both are terminal failures for `wait_for_download`. Reconnection reconciles the registry without retrying exports. `extension status` and extension-mode `--doctor` report negotiated features, a live capability probe, selected tab ID, and the last 100 command timing/error summaries; they exclude page content and URLs.

To upgrade, rebuild/export both packages, update the native-host runtime, restart the named harness daemon, and reload the unpacked Chrome extension or reinstall Firefox's temporary add-on. Select the profile and tab again after reconnecting. A new research helper used with an older extension may report an unsupported operation; upgrade all three components together. Existing browser login remains in its profile.

## Record extension workflows

Call `start_recording(name, title=...)` before the workflow and `stop_recording()` afterward. Action frames, viewport and focus metadata are captured through the selected backend, including Firefox without arbitrary JavaScript. Recording is opt-in and supported through both CLI names and `browser-use --cli-mcp`. Browser-owned password fields are masked in action text; review screenshots before sharing.

Video review and export require Chromium CDP plus ffmpeg/ffprobe. Set `BH_VIDEO_CDP_URL=http://127.0.0.1:9222` to an independently managed local Chromium renderer when the task backend is extension. Rendering uses separate runtime paths and never changes the task backend. Capture manifests and recording inputs are preserved.

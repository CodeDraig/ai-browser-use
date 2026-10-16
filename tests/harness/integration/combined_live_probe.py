"""Opt-in local CDP, MCP, research, and real video acceptance probe.

Run directly with --binary and --output. It creates isolated browser profiles,
keeps its artifacts, and closes only the browser and daemons it starts.
"""

import argparse
import asyncio
import hashlib
import io
import json
import os
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--binary', required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--extension-recording', type=Path)
    args = parser.parse_args()
    root = args.output.resolve()
    root.mkdir(parents=True, exist_ok=False)
    repo = Path(__file__).resolve().parents[3]
    env = {**os.environ, 'PYTHONPATH': str(repo), 'BH_HOME': str(root / 'config'),
           'BH_RUNTIME_DIR': str(root / 'runtime'), 'BH_TMP_DIR': str(root / 'tmp'),
           'BH_BACKEND': 'cdp', 'BU_NAME': 'combined-probe', 'BH_RECORD': '0'}
    env.pop('BU_CDP_WS', None)
    scan = Image.new('RGB', (900, 200), 'white')
    font = ImageFont.truetype('/usr/share/fonts/dejavu-sans-fonts/DejaVuSans.ttf', 36)
    ImageDraw.Draw(scan).text((25, 60), 'Combined research workflow', font=font, fill='black')
    image = io.BytesIO()
    scan.save(image, format='PNG')
    payload = image.getvalue()

    class Fixture(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            if self.path == '/download':
                self.send_response(200)
                self.send_header('Content-Type', 'image/png')
                self.send_header('Content-Disposition', 'attachment; filename="source.png"')
                self.end_headers()
                self.wfile.write(payload)
                return
            self.send_response(200)
            self.send_header('Content-Type', 'text/html')
            self.end_headers()
            self.wfile.write(b'<title>Combined fixture</title><h1>Combined research workflow</h1>'
                            b'<input id="query"><button id="button" onclick="document.querySelector(\'#result\').textContent=\'Complete\'">Run</button>'
                            b'<p id="result">Ready</p><a id="download" href="/download">Download scan</a>')

    server = ThreadingHTTPServer(('127.0.0.1', 0), Fixture)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    url = f'http://127.0.0.1:{server.server_port}'
    profile = root / 'profile'
    browser_log = (root / 'browser.log').open('w')
    browser = subprocess.Popen([args.binary, '--headless=new', '--no-sandbox', '--disable-dev-shm-usage',
                                '--no-first-run', '--remote-debugging-port=0', '--autoplay-policy=no-user-gesture-required',
                                '--user-data-dir=' + str(profile), 'about:blank'], stdout=browser_log, stderr=browser_log)
    receipt = {'checks': []}

    def cli(code=None, arguments=(), command='browser_harness.run', overrides=None):
        result = subprocess.run([sys.executable, '-m', command, *arguments], input=code, text=True,
                                capture_output=True, env={**env, **(overrides or {})}, timeout=180)
        with (root / 'commands.log').open('a') as log:
            log.write(json.dumps({'module': command, 'args': list(arguments), 'code': code,
                                  'returncode': result.returncode, 'stdout': result.stdout, 'stderr': result.stderr}) + '\n')
        if result.returncode:
            raise RuntimeError(result.stderr or result.stdout)
        return result.stdout

    def query(expression, **kwargs):
        return json.loads(cli('import json\nprint(json.dumps(' + expression + '))', **kwargs).splitlines()[-1])

    try:
        deadline = time.monotonic() + 30
        while not (profile / 'DevToolsActivePort').exists():
            if browser.poll() is not None or time.monotonic() >= deadline:
                raise RuntimeError('isolated Chromium did not start; inspect browser.log')
            time.sleep(.1)
        port = (profile / 'DevToolsActivePort').read_text().splitlines()[0]
        env['BU_CDP_URL'] = 'http://127.0.0.1:' + port
        query(f'new_tab({url!r})')
        query('wait_for_load()')
        # Headless Chromium can defer screenshots of never-activated tabs.
        query('activate_tab(current_tab())')
        recording = query("start_recording('cdp-recording', title='Combined local workflow')", overrides={'BH_RECORD': '1'})
        query("fill_input('#query', 'Research')", overrides={'BH_RECORD': '1'})
        query("click_element('#button')", overrides={'BH_RECORD': '1'})
        assert query("js('document.querySelector(\"#result\").textContent')") == 'Complete'
        capture = query(f'capture_research({str(root / "capture")!r})')
        assert capture['status'] == 'completed'
        operation = query("start_js('await new Promise(r=>setTimeout(r,50)); return 42;')")
        assert query(f'operation_result({operation!r}, timeout=5)') == 42
        query('enable_downloads()')
        query("click_element('#download')")
        downloaded = query('wait_for_download(timeout=20)')
        evidence = query(f'save_research_download({downloaded["download_id"]!r}, {str(root / "download")!r}, source={{"url": {url!r}}})')
        original = Path(evidence['path']) / evidence['artifacts'][0]['path']
        assert hashlib.sha256(original.read_bytes()).hexdigest() == hashlib.sha256(payload).hexdigest()
        extraction = query(f'extract_document({str(original)!r}, {str(root / "extraction")!r})')
        assert extraction['status'] == 'completed', extraction
        assert 'Combined research workflow' in (root / 'extraction/page-0001.txt').read_text()
        assert query('stop_recording()', overrides={'BH_RECORD': '1'}) == recording
        receipt.update(research=evidence, extraction=extraction, cdp_recording=recording)
        receipt['checks'].append('CDP actions, managed operation, research capture, verified PNG download and real OCR')

        async def check_mcp():
            from mcp import ClientSession, StdioServerParameters
            from mcp.client.stdio import stdio_client

            params = StdioServerParameters(command=sys.executable, args=['-m', 'browser_use.cli', '--cli-mcp'],
                                          env={**env, 'BH_RECORD': '1'})
            async with stdio_client(params) as (read, write):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    result = await session.call_tool('browser_exec', {'code':
                        f'new_tab({url!r})\nactivate_tab(current_tab())\nstart_recording("mcp-recording")\nfill_input("#query", "MCP")\nprint(page_info())\nstop_recording()'})
                    assert not result.isError, result
                    assert 'Combined fixture' in str(result.content)
                    shot = await session.call_tool('browser_screenshot', {})
                    assert any(part.type == 'image' for part in shot.content)
            events = list((root / 'config/agent-workspace/recordings/mcp-recording').glob('*.jpg'))
            assert len(events) >= 3
            receipt['checks'].append('actual stdio MCP navigation, form action, recording and image screenshot')

        asyncio.run(check_mcp())
        recordings = [Path(recording)]
        if args.extension_recording:
            recordings.append(args.extension_recording.resolve())
        receipt['videos'] = []
        for index, directory in enumerate(recordings):
            source_hashes = {p.name: hashlib.sha256(p.read_bytes()).hexdigest()
                             for p in directory.iterdir() if p.suffix == '.jpg' or p.name in {'events.jsonl', 'meta.json'}}
            cli(arguments=['video', 'init', str(directory)])
            summary = json.loads((directory / 'recording-summary.json').read_text())
            event_number = next(i for i, event in enumerate(summary['events'], 1) if event['helper'] == 'fill_input')
            frame = summary['events'][event_number - 1]['frame']
            brief = {'task': 'Combined workflow', 'summary': 'Capture research and record actions',
                     'plan': ['Read source', 'Preserve evidence'], 'outcomes': ['Verified source and recording'],
                     'actions': [{'event': event_number, 'chapter': 0, 'route': 'Research', 'narration': 'Enter the research query'}],
                     'privacy': {'reviewedFrames': [frame], 'redact': {}}}
            (directory / 'edit-brief.json').write_text(json.dumps(brief, indent=2))
            overrides = {'BH_BACKEND': 'extension', 'BH_VIDEO_CDP_URL': env['BU_CDP_URL']}
            cli(arguments=['video', 'review', str(directory)], overrides=overrides)
            cli(arguments=['video', 'export', str(directory), '--reviewed'], overrides=overrides)
            mp4 = directory / 'video.mp4'
            assert mp4.is_file()
            probe = json.loads(subprocess.check_output(['ffprobe', '-v', 'error', '-show_format', '-show_streams', '-of', 'json', str(mp4)], text=True))
            assert float(probe['format']['duration']) > 0
            assert all(hashlib.sha256((directory / name).read_bytes()).hexdigest() == digest for name, digest in source_hashes.items())
            receipt['videos'].append({'recording': str(directory), 'mp4': str(mp4), 'duration': probe['format']['duration'],
                                      'sha256': hashlib.sha256(mp4.read_bytes()).hexdigest(), 'sources_unchanged': True})
        receipt['checks'].append('CDP and extension recordings rendered, reviewed, exported and decoded as MP4 without source changes')
        receipt['status'] = 'passed'
    except Exception as exc:
        receipt.update(status='failed', error=str(exc))
        raise
    finally:
        for overrides in ({}, {'BH_VIDEO_CDP_URL': env.get('BU_CDP_URL', '')}):
            try:
                if overrides:
                    overrides.update(BU_NAME='video-render', BH_RUNTIME_DIR=str(root / 'video-runtime'), BH_TMP_DIR=str(root / 'video-tmp'))
                cli(arguments=['--reload'], overrides=overrides)
            except Exception:
                pass
        browser.terminate()
        try:
            browser.wait(timeout=10)
        except subprocess.TimeoutExpired:
            browser.kill()
            browser.wait()
        server.shutdown()
        browser_log.close()
        receipt['browser_stopped'] = browser.poll() is not None
        (root / 'result.json').write_text(json.dumps(receipt, indent=2) + '\n')
    print(json.dumps(receipt, indent=2))


if __name__ == '__main__':
    main()

"""Opt-in real-browser fixture probe. Runs only when invoked directly.

Requires Selenium, installed browser binary and harness runtime. All profiles,
registrations, credentials, and downloads are isolated under --output.
"""
import argparse
import contextlib
import hashlib
import json
import os
import shutil
import socket
import subprocess
import threading
import time
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

PAYLOAD=b'Browser Harness authenticated download fixture\n'*65536
class Fixture(BaseHTTPRequestHandler):
    def log_message(self,*args):pass
    def do_GET(self):
        if self.path=='/login':
            self.send_response(302);self.send_header('Set-Cookie','fixture=authenticated; Max-Age=3600; HttpOnly; SameSite=Lax');self.send_header('Location','/');self.end_headers();return
        if self.path=='/slow-page':time.sleep(.5)
        authenticated='fixture=authenticated' in self.headers.get('Cookie','')
        if self.path=='/slow-download' and authenticated:
            self.send_response(200);self.send_header('Content-Type','application/octet-stream');self.send_header('Content-Disposition','attachment; filename=slow.bin');self.send_header('Content-Length',str(64*65536));self.end_headers()
            try:
                for _ in range(64):self.wfile.write(b'x'*65536);self.wfile.flush();time.sleep(.2)
            except (BrokenPipeError,ConnectionResetError):pass
            return
        if self.path=='/download' and authenticated:
            self.send_response(200);self.send_header('Content-Type','application/octet-stream');self.send_header('Content-Disposition','attachment; filename="fixture.txt"');self.send_header('Content-Length',str(len(PAYLOAD)));self.end_headers();self.wfile.write(PAYLOAD);return
        self.send_response(200);self.send_header('Content-Type','text/html');self.end_headers()
        if not authenticated:self.wfile.write(b'<a id="login" href="/login">Log in</a>');return
        self.wfile.write(b'<title>Authenticated fixture</title><h1>Signed in</h1><form action="/search"><input id="query" name="q"><button id="search">Search</button></form><a id="download" href="/download">Download</a><a id="slow" href="/slow-download">Slow download</a><iframe src="/frame"></iframe>' if self.path!='/frame' else b'<p>Frame content</p>')
        if getattr(self.server,'research_checks',False) and self.path!='/frame':self.wfile.write(b'<div style="height:2000px"></div><a id="distant" href="/distant-result">Distant result</a>')


def main():
    p=argparse.ArgumentParser();p.add_argument('browser',choices=['chrome','firefox']);p.add_argument('--binary',required=True);p.add_argument('--harness-python',required=True);p.add_argument('--output',required=True);p.add_argument('--remote-host');p.add_argument('--recovery',action='store_true');p.add_argument('--benchmark',action='store_true');p.add_argument('--source-root');p.add_argument('--research-checks',action='store_true');p.add_argument('--small-payload',action='store_true')
    p.add_argument('--recording-checks', action='store_true')
    a=p.parse_args()
    global PAYLOAD
    if a.small_payload:PAYLOAD=b'Browser Harness authenticated download fixture\n'*2048
    root=Path(a.output).resolve();root.mkdir(parents=True,exist_ok=True)
    repo=Path(__file__).resolve().parents[3]
    home=root/'home';home.mkdir(exist_ok=True)
    env={**os.environ,'HOME':str(home),'XDG_CONFIG_HOME':str(home/'.config'),'BH_HOME':str(root/'config'),'BH_RUNTIME_DIR':str(root/'runtime'),'BH_TMP_DIR':str(root/'tmp'),'BH_BACKEND':'extension','BU_NAME':'extension-probe','BH_TELEMETRY':'0','PYTHONPATH':str(Path(a.source_root).resolve() if a.source_root else repo),'PYTHONNOUSERSITE':'1'}
    # Native messaging resolves per-user registration in the browser's HOME.
    os.environ['SE_AVOID_STATS']='true'
    os.environ.update({k:env[k] for k in ('HOME','XDG_CONFIG_HOME')})
    remote_ready=False
    def cli(args=(),code=None):
        if a.remote_host and remote_ready and (code is not None or tuple(args[:2]) in [('extension','list'),('extension','select')] or tuple(args)==('--reload',)):
            import shlex
            remote_env={'PYTHONPATH':'/tmp/bh-extension-probe/src','BH_HOME':'/tmp/bh-extension-probe/config','BH_RUNTIME_DIR':'/tmp/bh-extension-probe/run','BH_TMP_DIR':'/tmp/bh-extension-probe/tmp','BH_BACKEND':'extension','BU_NAME':'probe','BH_TELEMETRY':'0'}
            command=['ssh','-o','BatchMode=yes',a.remote_host,shlex.join(['env',*[f'{k}={v}' for k,v in remote_env.items()],'/tmp/bh-extension-probe-venv/bin/python','-m','browser_harness.run',*args])]
        else: command=[a.harness_python,'-m','browser_harness.run',*args]
        result=subprocess.run(command,input=code,text=True,capture_output=True,env=env,timeout=180)
        if result.returncode:raise RuntimeError(result.stderr or result.stdout)
        return result.stdout.strip()
    def query(code):return json.loads(cli(code='import json\nprint(json.dumps('+code+'))\n').splitlines()[-1])
    with socket.socket() as s:s.bind(('127.0.0.1',0));port=s.getsockname()[1]
    cli(['extension','configure','--port',str(port)])
    extension=root/'addon';cli(['extension','package',a.browser,str(extension)])
    if a.browser=='chrome':
        raw=hashlib.sha256(str(extension).encode()).hexdigest()[:32];identity=''.join(chr(ord('a')+int(c,16)) for c in raw)
        cli(['extension','install-host','chrome','--extension-id',identity])
        source=home/'.config/google-chrome/NativeMessagingHosts'
        for target in (home/'.config/chromium/NativeMessagingHosts', home/'.config/google-chrome-for-testing/NativeMessagingHosts',root/'profile/NativeMessagingHosts'):
            target.parent.mkdir(parents=True,exist_ok=True);shutil.copytree(source,target,dirs_exist_ok=True)
    else:cli(['extension','install-host','firefox'])
    tunnel=None
    if a.remote_host:
        config=(root/'config/extension.json').read_bytes()
        subprocess.run(['ssh',a.remote_host,'mkdir -p /tmp/bh-extension-probe/config && chmod 700 /tmp/bh-extension-probe/config && umask 077 && cat > /tmp/bh-extension-probe/config/extension.json'],input=config,check=True)
        subprocess.run(['ssh',a.remote_host,'mkdir','-p',str(root)],check=True)
        remote_ready=True
        cli(['extension','list'])
        tunnel=subprocess.Popen(['ssh','-N','-o','ExitOnForwardFailure=yes','-L',f'127.0.0.1:{port}:127.0.0.1:{port}',a.remote_host])
    else: cli(['extension','list'])
    server=ThreadingHTTPServer(('127.0.0.1',0),Fixture);threading.Thread(target=server.serve_forever,daemon=True).start()
    url=f'http://127.0.0.1:{server.server_port}'
    def pull(path):
        if a.remote_host:
            import shlex
            with open(path,'wb') as output: subprocess.run(['ssh',a.remote_host,'cat -- '+shlex.quote(str(path))],stdout=output,check=True)
    driver=None
    receipt={'browser':a.browser,'login_method':'automated fixture login before extension connection','checks':[],'remote_harness':a.remote_host}
    try:
        from selenium import webdriver
        if a.browser=='chrome':
            options=webdriver.ChromeOptions();options.binary_location=a.binary
            options.add_argument('--headless=new');options.add_argument('--enable-unsafe-extension-debugging');options.add_argument('--no-sandbox');options.add_argument('--disable-dev-shm-usage');options.add_argument('--user-data-dir='+str(root/'profile'));options.add_argument('--load-extension='+str(extension));options.add_argument('--disable-extensions-except='+str(extension))
            options.add_experimental_option('prefs',{'download.default_directory':str(root/'downloads'),'download.prompt_for_download':False})
            driver=webdriver.Chrome(options=options)
        else:
            options=webdriver.FirefoxOptions();options.binary_location=a.binary;options.add_argument('-headless')
            options.set_preference('browser.download.dir',str(root/'downloads'));options.set_preference('browser.download.folderList',2);options.set_preference('browser.helperApps.neverAsk.saveToDisk','application/octet-stream')
            from selenium.webdriver.firefox.service import Service
            driver=webdriver.Firefox(options=options,service=Service(service_args=['--allow-system-access']))
            archive=root/'addon.zip'
            with zipfile.ZipFile(archive,'w') as z:
                for file in extension.iterdir():z.write(file,file.name)
            driver.install_addon(str(archive),temporary=True)
        driver.get(url);driver.find_element('id','login').click()
        assert 'Signed in' in driver.page_source
        original=driver.current_window_handle
        driver.switch_to.new_window('tab')
        if a.browser=='chrome':popup=f'chrome-extension://{identity}/popup.html'
        else:
            driver.set_context('chrome')
            uuids=driver.execute_script("return Services.prefs.getCharPref('extensions.webextensions.uuids');")
            driver.set_context('content')
            popup='moz-extension://'+json.loads(uuids)['browser-harness@local']+'/popup.html'
        driver.get(popup)
        from selenium.webdriver.support.ui import WebDriverWait
        WebDriverWait(driver, 10).until(lambda d: d.execute_script("return typeof document.querySelector('#connect').onclick === 'function'"))
        driver.find_element('id','connect').click()
        deadline=time.monotonic()+15
        while time.monotonic()<deadline:
            connections=json.loads(cli(['extension','list']))['connections']
            if connections:break
            time.sleep(.2)
        else:raise RuntimeError('native host did not connect: '+driver.page_source)
        receipt['checks'].append('native host connected')
        receipt['connection']=connections[0]['connection_id']
        tabs=query('list_tabs()');tab=next(t for t in tabs if t['url'].startswith(url) and '/frame' not in t['url'])
        query(f"switch_tab({tab['targetId']!r})")
        page=query('read_page()');assert 'Signed in' in page['text'];receipt['checks'].append('existing authenticated session read through harness')
        if a.recording_checks:
            recording=query("start_recording('fixture-recording', title='Extension research workflow')")
        query("fill_input('#query','Cleveland Torso Murders')");query("click_element('#search')")
        time.sleep(.2);assert 'q=Cleveland' in query('page_info()')['url'];receipt['checks'].append('fill and search')
        shot=query(f"capture_screenshot({str(root/'screenshot.png')!r})");pull(shot);assert Path(shot).stat().st_size>100;receipt['checks'].append('screenshot')
        query('enable_downloads()');query("click_element('#download')")
        downloaded=query('wait_for_download(timeout=15)')
        receipt['download_record']=downloaded
        saved=query(f"save_download({downloaded['download_id']!r},{str(root/'retrieved.txt')!r})")
        pull(saved['path'])
        assert Path(saved['path']).read_bytes()==PAYLOAD and saved['sha256']==hashlib.sha256(PAYLOAD).hexdigest()
        receipt['download']=saved;receipt['checks'].append('browser download retrieved and SHA-256 verified')
        if a.benchmark:
            code = '''import json,time,tracemalloc,statistics,math
import browser_harness.helpers as h
original=h._send
counts={'messages':0}
def measured(*a,**kw):
    counts['messages']+=1
    return original(*a,**kw)
h._send=measured
result={}
for kind in ('reads','transfers'):
    samples=[];counts['messages']=0;tracemalloc.start()
    for i in range(30 if kind=='reads' else 10):
        start=time.perf_counter()
        if kind=='reads': read_page()
        else: save_download(DOWNLOAD, DEST + str(i), overwrite=True)
        samples.append(time.perf_counter()-start)
    _,peak=tracemalloc.get_traced_memory();tracemalloc.stop()
    result[kind]={'median_seconds':statistics.median(samples),'p95_seconds':sorted(samples)[math.ceil(len(samples)*.95)-1], 'ipc_messages':counts['messages'],'peak_python_bytes':peak,'samples':len(samples)}
    if kind=='transfers':result[kind]['bytes_per_second']=SIZE/statistics.median(samples)
print(json.dumps(result))
'''.replace('DOWNLOAD',repr(downloaded['download_id'])).replace('DEST',repr(str(root/'bench-transfer-'))).replace('SIZE',str(len(PAYLOAD)))
            receipt['benchmark']=json.loads(cli(code=code).splitlines()[-1])
        if a.recovery:
            cli(['--reload']);cli(['extension','list'])
            deadline=time.monotonic()+15
            while time.monotonic()<deadline:
                if json.loads(cli(['extension','list']))['connections']:break
                time.sleep(.2)
            else:raise RuntimeError('broker reconnect failed')
            query(f"switch_tab({tab['targetId']!r})")
            assert query(f"download_status({downloaded['download_id']!r})")['state']=='completed'
            receipt['checks'].append('broker restart reconnected without replay and retained download')
            driver.switch_to.new_window('tab');driver.get(popup)
            driver.execute_script('chrome.runtime.reload()')
            time.sleep(1)
            driver.switch_to.window(original)
            deadline=time.monotonic()+15
            while time.monotonic()<deadline:
                if json.loads(cli(['extension','list']))['connections']:break
                time.sleep(.2)
            else:raise RuntimeError('extension reload reconnect failed')
            query(f"switch_tab({tab['targetId']!r})")
            assert query(f"download_status({downloaded['download_id']!r})")['state']=='completed'
            receipt['checks'].append('extension reload reconnected and retained download')
            cursor=query('enable_downloads()')['cursor'];query("click_element('#slow')")
            deadline=time.monotonic()+10
            while time.monotonic()<deadline:
                items=query(f'list_downloads(after={cursor})')
                if items:break
                time.sleep(.1)
            else:raise RuntimeError('slow download missing')
            slow=items[0]['download_id'];query(f'cancel_download({slow!r})')
            deadline=time.monotonic()+5
            while time.monotonic()<deadline:
                if query(f'download_status({slow!r})')['state']=='canceled':break
                time.sleep(.1)
            else:raise RuntimeError('download cancellation not observed')
            receipt['checks'].append('interrupted download tracked as canceled')
            if a.browser=='chrome':
                driver.quit();driver=None
                driver=webdriver.Chrome(options=options)
                driver.get(url)
                assert 'Signed in' in driver.page_source
                original=driver.current_window_handle
                deadline=time.monotonic()+15
                while time.monotonic()<deadline:
                    if json.loads(cli(['extension','list']))['connections']:break
                    time.sleep(.2)
                else:raise RuntimeError('browser restart reconnect failed')
                tab=next(t for t in query('list_tabs()') if t['url'].startswith(url))
                query(f"switch_tab({tab['targetId']!r})")
                assert query(f"download_status({downloaded['download_id']!r})")['state']=='completed'
                receipt['checks'].append('Chrome process restart retained login, connection identity, and downloads')

        new=query(f'new_tab({url!r})');query(f"switch_tab({tab['targetId']!r})");assert query('current_tab()')['targetId']==tab['targetId'];receipt['checks'].append('tab selection')
        if a.research_checks:
            server.research_checks=True
            fresh=query(f'new_tab({(url+"/slow-page")!r})')
            query(f'switch_tab({fresh!r})')
            assert query('page_info()')['url']==url+'/slow-page'
            query("wait_for_element('#distant',timeout=5)")
            query("click_element('#distant')")
            time.sleep(.2)
            assert query('page_info()')['url']==url+'/distant-result'
            receipt['checks'].append('new-tab readiness and offscreen result click')
            capture=query(f'capture_research({str(root/"research-capture")!r})')
            assert capture['status']=='completed'
            evidence=query(f'save_research_download({downloaded["download_id"]!r},{str(root/"research-download")!r},source={{"url":{url!r},"title":"Fixture"}})')
            assert evidence['status']=='completed' and evidence['artifacts'][0]['sha256']==hashlib.sha256(PAYLOAD).hexdigest()
            receipt['checks'].append('page evidence and download citation manifests verified')
            receipt['research']={'capture':capture,'download':evidence}
        if a.recording_checks:
            assert query('stop_recording()') == recording
            events=[json.loads(line) for line in (Path(recording)/'events.jsonl').read_text().splitlines()]
            frames=[event for event in events if event.get('frame')]
            assert len(frames)>=3, events
            assert all(event.get('w') and event.get('h') for event in frames)
            from PIL import Image
            for event in frames:
                with Image.open(Path(recording)/event['frame']) as image:
                    image.verify()
            assert any(event['helper']=='click_element' for event in frames)
            receipt['recording']={'path':recording,'frames':len(frames),'helpers':[event['helper'] for event in frames]}
            receipt['checks'].append('extension action recording, viewport metadata and JPEG frames verified')
        receipt['status']='passed'
    except Exception as exc:
        receipt['status']='failed';receipt['error']=str(exc)
        if driver:
            with contextlib.suppress(Exception):
                driver.switch_to.window(original)
                driver.switch_to.new_window('tab')
                driver.get(popup)
            with contextlib.suppress(Exception): receipt['browser_logs']=driver.get_log('browser')
            with contextlib.suppress(Exception): receipt['popup_state']=driver.execute_async_script("const done=arguments[0];chrome.runtime.sendMessage({popup:'status'}, r=>done({response:r,error:chrome.runtime.lastError?.message}));")
        if driver and a.browser=='chrome':
            with contextlib.suppress(Exception): receipt['targets']=driver.execute_cdp_cmd('Target.getTargets',{})
        print(json.dumps(receipt,indent=2))
        raise
    finally:
        (root/'receipt.json').write_text(json.dumps(receipt,indent=2))
        if driver:
            with contextlib.suppress(Exception):driver.quit()
        server.shutdown()
        if tunnel:tunnel.terminate();tunnel.wait(timeout=10)
        with contextlib.suppress(Exception):cli(['--reload'])
    print(json.dumps(receipt,indent=2))

if __name__=='__main__':main()

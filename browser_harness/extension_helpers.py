"""Public helper dispatch for the extension backend; direct CDP remains the default."""
import base64
import functools
import hashlib
import inspect
import math
import os
import tempfile
import time
from pathlib import Path


def enabled(): return os.environ.get('BH_BACKEND') == 'extension'


def call(operation, **args):
    from .helpers import _send
    return _send({'meta':'extension_call', 'operation':operation, 'args':args})['result']


def browser_capabilities():
    if enabled(): return call('capabilities')
    return {'backend':'cdp', 'browser':'chromium', 'raw_cdp':True, 'js':True,
            'screenshots':True, 'full_page_screenshot':True, 'downloads':True, 'uploads':True}


def read_page(frame_id=0, *, include_frames=True):
    if enabled(): return call('read_page', frame_id=frame_id, include_frames=include_frames)
    from .helpers import js
    if frame_id: raise RuntimeError('unsupported_capability: use js(target_id=...) for CDP frames')
    return js("({url:location.href,title:document.title,text:document.body.innerText,links:[...document.querySelectorAll('a[href]')].map(a=>({text:a.innerText,url:a.href}))})")


def click_element(selector, frame_id=0):
    if enabled(): return call('click_element', selector=selector, frame_id=frame_id)
    import json

    from .helpers import click_at_xy, js
    if frame_id: raise RuntimeError('unsupported_capability: explicit CDP frame click')
    box = js("(()=>{const es=document.querySelectorAll(" + json.dumps(selector) + ");if(es.length!==1)throw Error('selector must match one element');es[0].scrollIntoView({block:'center'});const r=es[0].getBoundingClientRect();return {x:r.x+r.width/2,y:r.y+r.height/2};})()")
    return click_at_xy(box['x'],box['y'])


def save_download(download_id, destination, *, timeout=300, overwrite=False, progress=None):
    from .files import download_status
    from .helpers import _send
    record = download_status(download_id)
    if record['state'] != 'completed': raise ValueError('download_not_complete')
    destination = Path(destination).expanduser()
    if destination.is_dir(): destination /= (record.get('suggested_filename') or download_id).replace('\\','/').split('/')[-1]
    if destination.exists() and not overwrite: raise FileExistsError(destination)
    if not math.isfinite(timeout) or timeout <= 0: raise ValueError('timeout must be positive finite seconds')
    end = time.monotonic() + timeout
    def transfer(action, **args):
        remaining = end - time.monotonic()
        if remaining <= 0: raise TimeoutError('download_transfer_timeout')
        return _send({'meta':'extension_file','action':action,'args':args}, timeout=remaining)
    opened = transfer('open', download_id=download_id, chunk_size=192 * 1024)
    tid = opened['transfer_id']
    temporary = None
    try:
        digest = hashlib.sha256(); count=0
        with tempfile.NamedTemporaryFile(dir=destination.parent, prefix='.browser-harness-', delete=False) as stream:
            temporary = Path(stream.name)
            while True:
                chunk = transfer('chunk',transfer_id=tid,offset=count)
                data = base64.b64decode(chunk['data'],validate=True)
                stream.write(data);digest.update(data);count+=len(data)
                if chunk['offset'] != count: raise ValueError('invalid_transfer_offset')
                if progress: progress({'received_bytes':count,'total_bytes':opened['size']})
                if chunk['eof']:
                    if count != opened['size'] or digest.hexdigest() != chunk['sha256']: raise ValueError('download_verification_failed')
                    break
                if not data: raise ValueError('empty_transfer_chunk')
            stream.flush();os.fsync(stream.fileno())
        if overwrite: os.replace(temporary,destination)
        else: os.link(temporary,destination);temporary.unlink()
        temporary=None
        return {'path':str(destination),'size':count,'sha256':digest.hexdigest(),'download_id':download_id}
    finally:
        if temporary: temporary.unlink(missing_ok=True)
        try: _send({'meta':'extension_file','action':'close','args':{'transfer_id':tid}},timeout=2)
        except Exception: pass


def install(namespace):
    """Wrap only backend-sensitive helpers; retain original signatures for scripts."""
    supported = {'goto_url','page_info','click_at_xy','type_text','fill_input','press_key','scroll','activate_tab',
                 'capture_screenshot','list_tabs','current_tab','switch_tab','new_tab','close_tab',
                 'ensure_real_tab','wait_for_load','wait_for_element','js'}
    unsupported = {'iframe_target','wait_for_network_idle','dispatch_key','upload_file','fetch_browser_file',
                   'start_js','start_cdp','operation_status','operation_result','list_operations','cancel_operation','forget_operation','drain_events'}
    def wrap(name, original):
        signature=inspect.signature(original)
        @functools.wraps(original)
        def wrapper(*args,**kwargs):
            if not enabled(): return original(*args,**kwargs)
            if name in unsupported: raise RuntimeError('unsupported_capability: '+name)
            bound=signature.bind(*args,**kwargs);bound.apply_defaults();a=dict(bound.arguments)
            if name=='save_download': return save_download(*args,**kwargs)
            if name=='capture_screenshot':
                from . import _ipc
                response=call(name,full=a['full'])
                path=a['path'] or str(_ipc._TMP/'shot.png')
                Path(path).write_bytes(base64.b64decode(response['data']))
                if a['max_dim']:
                    from PIL import Image
                    with Image.open(path) as image:
                        image.thumbnail((a['max_dim'],a['max_dim']));image.save(path)
                return path
            if name=='ensure_real_tab':
                try: return call('current_tab')
                except RuntimeError as exc:
                    if 'tab_not_selected' not in str(exc): raise
                    tabs=call('list_tabs')
                    if len(tabs)!=1: raise RuntimeError('select_tab: use list_tabs() and switch_tab(targetId)')
                    return call('switch_tab',target=tabs[0]['targetId'])
            if name in ('switch_tab','close_tab','activate_tab') and isinstance(a.get('target'),dict):
                a['target']=a['target'].get('targetId') or a['target'].get('target_id')
            if name=='new_tab': return call(name,**a)['targetId']
            if name=='switch_tab': return call(name,**a)['targetId']
            if name=='activate_tab': return call(name,**a)['targetId']
            if name=='close_tab' and a.get('target') is not None:
                current=call('current_tab')
                if str(a['target']) != current['targetId']: raise RuntimeError('switch_tab before closing a different tab')
            if name=='js' and a.get('target_id') is not None: raise RuntimeError('unsupported_capability: js iframe target')
            if name in ('wait_for_load','wait_for_element'):
                if not math.isfinite(a['timeout']) or a['timeout'] < 0: raise ValueError('timeout must be nonnegative finite seconds')
                deadline=time.monotonic()+a['timeout']
                while True:
                    found=call('ready' if name=='wait_for_load' else 'element_exists',**a)
                    if found:return True
                    if time.monotonic()>=deadline:return False
                    time.sleep(.1)
            return call(name,**a)
        return wrapper
    for name in supported | unsupported | {'save_download'}:
        if name in namespace: namespace[name]=wrap(name,namespace[name])

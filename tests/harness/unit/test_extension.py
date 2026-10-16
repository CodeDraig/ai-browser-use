import asyncio
import hashlib
import io
import json
import struct
from pathlib import Path

import pytest

from browser_harness.extension import Broker, handshake_proof, package
from browser_harness.native_host import DownloadFiles, read_message, write_message


def test_native_framing_and_limit():
    stream=io.BytesIO();write_message(stream,{'text':'é'});stream.seek(0)
    assert read_message(stream)=={'text':'é'}
    with pytest.raises(ValueError):read_message(io.BytesIO(struct.pack('=I',2**24)))
    with pytest.raises(EOFError):read_message(io.BytesIO(b'\x00'))


def test_file_registry_and_integrity(tmp_path):
    files=DownloadFiles(); data=b'abc123'*30000; path=tmp_path/'source';path.write_bytes(data)
    with pytest.raises(ValueError):files.call('file_open',{'download_id':'1','path':str(path)})
    files.register([dict(download_id='1',state='completed',browser_path=str(path),received_bytes=len(data))])
    opened=files.call('file_open',{'download_id':'1'}); offset=0
    while True:
        result=files.call('file_chunk',{'transfer_id':opened['transfer_id'],'offset':offset});offset=result['offset']
        if result['eof']:break
    assert offset==len(data) and result['sha256']==hashlib.sha256(data).hexdigest()
    files.close()
    path.unlink();path.symlink_to('/etc/passwd')
    with pytest.raises(ValueError):files.call('file_open',{'download_id':'1'})


def test_file_change_rejected(tmp_path):
    path=tmp_path/'file';path.write_bytes(b'first');files=DownloadFiles()
    files.register([dict(download_id='2',state='completed',browser_path=str(path),received_bytes=5)])
    opened=files.call('file_open',{'download_id':'2'});path.write_bytes(b'longer')
    with pytest.raises(ValueError,match='changed'):files.call('file_chunk',{'transfer_id':opened['transfer_id'],'offset':0})


def test_manifests(tmp_path):
    for browser in ['chrome','firefox']:
        path=Path(package(browser,tmp_path/browser));manifest=json.loads((path/'manifest.json').read_text())
        assert manifest['manifest_version']==3 and manifest['incognito']=='not_allowed'
        assert ('debugger' in manifest['permissions']) == (browser=='chrome')
        assert (path/'page.js').is_file()


def test_selection_does_not_migrate():
    broker=Broker({'token':'a'*64,'port':0})
    broker.connections={'a':{},'b':{}}
    with pytest.raises(RuntimeError,match='select_connection'):broker.connection()
    del broker.connections['b'];assert broker.connection()=='a'
    broker.connections={'b':{}}
    with pytest.raises(RuntimeError,match='disconnected'):broker.connection()


def test_live_broker_auth_and_disconnect():
    async def run():
        from websockets.asyncio.client import connect
        from websockets.exceptions import ConnectionClosed
        broker=Broker({'token':'a'*64,'port':0});await broker.start()
        port=broker.server.sockets[0].getsockname()[1]
        uri=f'ws://127.0.0.1:{port}'
        try:
            async with connect(uri) as bad:
                await bad.recv()
                await bad.send(json.dumps(dict(version=1,connection_id='bad',proof='wrong')))
                with pytest.raises(ConnectionClosed):await bad.recv()
            async with connect(uri) as ws:
                challenge=json.loads(await ws.recv())['challenge']
                hello=dict(version=1,connection_id='good',nonce='b'*64,browser='firefox')
                hello['proof']=handshake_proof('a'*64,'client',challenge,hello)
                await ws.send(json.dumps(hello))
                acknowledgement=json.loads(await ws.recv())
                assert acknowledgement['connected']
                assert acknowledgement['proof']==handshake_proof('a'*64,'server',challenge,hello)
                assert acknowledgement['proof']!=hello['proof']
                pending=asyncio.create_task(broker.call('read_page'))
                req=json.loads(await ws.recv());assert req['operation']=='read_page'
                await ws.send(json.dumps({'id':req['id'],'result':{'text':'hello'}}))
                assert await pending=={'text':'hello'}
                pending=asyncio.create_task(broker.call('click_element'))
                await ws.recv();await ws.close()
                with pytest.raises(RuntimeError,match='not replayed'):await pending
        finally:await broker.close()
    asyncio.run(run())


def test_extension_helpers_preserve_default(monkeypatch):
    from browser_harness import extension_helpers as e
    namespace={'page_info':lambda: {'original':True}}
    e.install(namespace);monkeypatch.delenv('BH_BACKEND',raising=False)
    assert namespace['page_info']()=={'original':True}
    monkeypatch.setenv('BH_BACKEND','extension');monkeypatch.setattr(e,'call',lambda op,**kw:{'op':op})
    assert namespace['page_info']()=={'op':'page_info'}


def test_downloads_are_connection_qualified_and_transfers_pinned():
    async def run():
        broker=Broker({'token':'a'*64,'port':0})
        broker.connections={'one':{},'two':{}};broker.selected='one';calls=[]
        async def call(op,args=None,**kwargs):
            calls.append((op,args,kwargs))
            if op=='download_list':return {'downloads':[{'download_id':'7'}]}
            if op=='file_open':return {'transfer_id':'t','size':1}
            return {'data':'eA=='}
        broker.call=call
        assert (await broker.handle({'meta':'download_list'}))['downloads'][0]['download_id']=='one:7'
        with pytest.raises(RuntimeError,match='mismatch'):
            await broker.handle({'meta':'download_status','download_id':'two:7'})
        await broker.handle({'meta':'extension_file','action':'open','args':{'download_id':'one:7'}})
        broker.selected='two'
        await broker.handle({'meta':'extension_file','action':'chunk','args':{'transfer_id':'t','offset':0}})
        assert calls[-1][2]['connection']=='one'
    asyncio.run(run())


def test_save_download_verifies_before_replacing(tmp_path,monkeypatch):
    import base64

    from browser_harness import extension_helpers, files, helpers
    monkeypatch.setattr(files,'download_status',lambda _:dict(state='completed',suggested_filename='result'))
    content=b'contents';digest=hashlib.sha256(content).hexdigest()
    def send(req,**kwargs):
        if req['action']=='open':return {'transfer_id':'t','size':len(content)}
        if req['action']=='chunk':return {'data':base64.b64encode(content).decode(),'offset':len(content),'eof':True,'sha256':digest}
        return {}
    monkeypatch.setattr(helpers,'_send',send)
    dest=tmp_path/'out'
    assert extension_helpers.save_download('one:7',dest)['sha256']==digest
    assert dest.read_bytes()==content
    digest='wrong'
    with pytest.raises(ValueError,match='verification'):
        extension_helpers.save_download('one:7',dest,overwrite=True)
    assert dest.read_bytes()==content
    assert not list(tmp_path.glob('.browser-harness-*'))


def test_extension_mode_never_probes_chrome_or_spawns_cloud(monkeypatch):
    import sys
    from io import StringIO

    from browser_harness import run
    monkeypatch.setenv('BH_BACKEND','extension');monkeypatch.setenv('BU_AUTOSPAWN','1')
    monkeypatch.setattr(sys,'argv',['browser-harness']);monkeypatch.setattr(sys,'stdin',StringIO('pass'))
    monkeypatch.setattr(run,'ensure_daemon',lambda:None)
    def forbidden(*a,**kw):raise AssertionError('extension mode must not invoke CDP/cloud discovery')
    from browser_harness import daemon
    monkeypatch.setattr(daemon,'get_ws_url',forbidden)
    assert not hasattr(run, 'start_remote_daemon')
    run.main()


def test_mutual_auth_proof_bound_to_peer_and_role():
    hello=dict(nonce='b'*64,connection_id='profile',browser='chrome',version=1)
    proof=handshake_proof('a'*64,'client','c'*64,hello)
    assert proof != handshake_proof('a'*64,'server','c'*64,hello)
    assert proof != handshake_proof('a'*64,'client','d'*64,hello)
    assert proof != handshake_proof('a'*64,'client','c'*64,{**hello,'connection_id':'other'})


def test_negotiated_chunks_and_incremental_registry(tmp_path):
    from browser_harness.native_host import CHUNK, MAX_CHUNK
    path=tmp_path/'download';path.write_bytes(b'x'*(MAX_CHUNK+10))
    record=dict(download_id='1',state='completed',browser_path=str(path),received_bytes=path.stat().st_size)
    files=DownloadFiles();files.update([record])
    old=files.call('file_open',{'download_id':'1','chunk_size':MAX_CHUNK})
    assert old['chunk_size']==CHUNK
    files.close();files.chunk_size=MAX_CHUNK
    new=files.call('file_open',{'download_id':'1','chunk_size':MAX_CHUNK*20})
    assert new['chunk_size']==MAX_CHUNK
    first=files.call('file_chunk',{'transfer_id':new['transfer_id'],'offset':0})
    assert first['offset']==MAX_CHUNK and not first['eof']
    files.close();files.update([],['1'])
    with pytest.raises(ValueError,match='not_registered'):files.call('file_open',{'download_id':'1'})


def test_feature_negotiation_legacy_and_new_peers():
    async def run():
        from websockets.asyncio.client import connect
        broker=Broker({'token':'a'*64,'port':0});await broker.start()
        uri=f"ws://127.0.0.1:{broker.server.sockets[0].getsockname()[1]}"
        try:
            for i,offered in enumerate([[],['transfer_192k','unknown','download_deltas']]):
                async with connect(uri) as ws:
                    challenge=json.loads(await ws.recv())['challenge']
                    hello=dict(version=1,nonce='x',connection_id=f'peer{i}',features=offered,native_features=offered,browser='chrome')
                    hello['proof']=handshake_proof('a'*64,'client',challenge,hello)
                    await ws.send(json.dumps(hello));ack=json.loads(await ws.recv())
                    assert set(ack['features'])==set(offered)-{'unknown'}
        finally:await broker.close()
    asyncio.run(run())


def test_extension_doctor_uses_extension_diagnostics(monkeypatch):
    from browser_harness import admin, extension
    monkeypatch.setenv('BH_BACKEND','extension')
    monkeypatch.setattr(extension,'cli',lambda args:17 if args==['status'] else 1)
    assert admin.run_doctor()==17


def test_download_uses_final_file_size_not_network_progress(tmp_path):
    path=tmp_path/'file';path.write_bytes(b'complete')
    files=DownloadFiles();files.register([dict(download_id='1',state='completed',browser_path=str(path),received_bytes=3,file_size=8)])
    opened=files.call('file_open',{'download_id':'1'})
    assert opened['size']==8
    files.close()

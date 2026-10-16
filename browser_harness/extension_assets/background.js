'use strict';
const api = globalThis.browser || chrome;
const firefox = typeof api.runtime.getBrowserInfo === 'function';
const HOST = 'org.browser_harness.bridge';
let harness='', wanted=false, nativeFeatures=[];
const FEATURES=['download_deltas','document_token'];
const initialized=new Map(), frameGenerations=new Map();
const pause=ms=>new Promise(resolve=>setTimeout(resolve,ms));
let port=null, helloSent=false, connected=false, error='', connectionId='', label='', enabled=false, sequence=0, records={};
const attached=new Set(), detached=new Set(), generations=new Map(), requests=new Map();
let ready=load();
async function load() {
  const state=await api.storage.local.get(['connectionId','label','enabled','sequence','records','wanted']);
  connectionId=state.connectionId || crypto.randomUUID(); label=state.label || (firefox?'Firefox':'Chrome');
  wanted=state.wanted || false;
  enabled=state.enabled || false; sequence=state.sequence || 0; records=state.records || {};
  await api.storage.local.set({connectionId});
}
async function persist() {await api.storage.local.set({enabled,sequence,records});}
function post(message) {
  if(!port)return;
  // Startup download events can arrive while connect() awaits storage. The
  // native host must see hello first; reconcile() sends the complete registry
  // immediately after it, including updates held back here.
  if(!helloSent && message.type!=='hello')return;
  const raw=JSON.stringify(message);
  if(raw.length<=60000){port.postMessage(message);return;}
  if(raw.length>16*1024*1024){port.postMessage({id:message.id,error:'result_too_large'});return;}
  const transfer=crypto.randomUUID();
  for(let offset=0;offset<raw.length;offset+=60000)port.postMessage({type:'chunk',transfer,data:raw.slice(offset,offset+60000),last:offset+60000>=raw.length});
}
function registry() {post({type:'downloads',records:Object.values(records)});}
function ordinary(tab) {return !tab.incognito && /^https?:\/\//.test(tab.url || '');}
async function getTab(id) {
  if(id===null || id===undefined) throw Error('tab_not_selected');
  let tab;try{tab=await api.tabs.get(Number(id));}catch{throw Error('tab_closed');}
  if(!ordinary(tab))throw Error('restricted_tab');return tab;
}
function info(tab) {return {targetId:String(tab.id),target_id:String(tab.id),url:tab.url,title:tab.title || '',type:'page'};}
async function normalTabs() {return (await api.tabs.query({})).filter(ordinary).map(info);}
async function page(id,operation,args={}) {
  await getTab(id);
  const frameId=args.frame_id || 0;
  const generation=()=>`${generations.get(Number(id)) || 0}:${frameGenerations.get(`${id}:${frameId}`) || 0}`;
  const epoch=generation();
  try {
    const key=`${id}:${frameId}`;
    if(!initialized.has(key)) {
      const pending=api.scripting.executeScript({target:{tabId:Number(id),frameIds:[frameId]},files:['page.js']});
      initialized.set(key,pending);
      try {await pending;} catch(e){initialized.delete(key);throw e;}
    } else await initialized.get(key);
    if(generation()!==epoch)throw Error('navigation_changed');
    const response=await api.tabs.sendMessage(Number(id),{bhPage:true,operation,args},{frameId});
    if(response.error)throw Error(response.error);
    if(generation()!==epoch)throw Error('navigation_changed: outcome may be unknown');
    return response.result;
  } catch(e) {throw Error('page_operation_failed: '+e.message);}
}
async function cdp(id,method,params={}) {
  if(firefox)throw Error('unsupported_capability: raw_cdp');
  await getTab(id);
  if(detached.has(Number(id)))throw Error('debugger_detached: explicitly switch_tab to reattach');
  if(!attached.has(Number(id))) {await api.debugger.attach({tabId:Number(id)},'1.3');attached.add(Number(id));}
  // Raw protocol commands must not escape the selected ordinary tab.
  if(!/^(DOM|DOMSnapshot|Runtime|Input|Page|Accessibility)\./.test(method) || ['Page.navigate','Page.navigateToHistoryEntry','Page.crash','Page.close'].includes(method))throw Error('unsupported_capability: '+method);
  const epoch=generations.get(Number(id)) || 0;
  const result=await api.debugger.sendCommand({tabId:Number(id)},method,params);
  if((generations.get(Number(id)) || 0)!==epoch)throw Error('navigation_changed: outcome may be unknown');
  return result;
}
if(!firefox)api.debugger.onDetach.addListener(source=>{attached.delete(source.tabId);detached.add(source.tabId);});
api.webNavigation.onCommitted.addListener(d=>{
  if(d.frameId===0){
    generations.set(d.tabId,(generations.get(d.tabId)||0)+1);
    for(const key of frameGenerations.keys())if(key.startsWith(`${d.tabId}:`))frameGenerations.delete(key);
  }else{const key=`${d.tabId}:${d.frameId}`;frameGenerations.set(key,(frameGenerations.get(key)||0)+1);}
  for(const key of initialized.keys())if(key.startsWith(`${d.tabId}:`) && (d.frameId===0 || key===`${d.tabId}:${d.frameId}`))initialized.delete(key);
});
api.tabs.onRemoved.addListener(id=>{attached.delete(id);detached.delete(id);generations.delete(id);for(const key of frameGenerations.keys())if(key.startsWith(`${id}:`))frameGenerations.delete(key);for(const key of initialized.keys())if(key.startsWith(`${id}:`))initialized.delete(key);});
function downloadRecord(item,old) {
  return {...old,url:item.url,suggested_filename:item.filename.split(/[\\/]/).pop(),browser_path:item.filename,
    state:item.state==='complete'?'completed':item.state==='interrupted'?(item.error==='USER_CANCELED'?'canceled':'interrupted'):'inProgress',
    received_bytes:item.bytesReceived,total_bytes:item.totalBytes,file_size:Number.isFinite(item.fileSize) && item.fileSize>=0?item.fileSize:null,error:item.error || null,browser_context_id:null};
}
let downloadQueue=Promise.resolve(), progressTimer=null;
const dirtyDownloads=new Set(), removedDownloads=new Set();
async function flushDownloads() {
  if(progressTimer)clearTimeout(progressTimer);progressTimer=null;
  const changed=[...dirtyDownloads].map(id=>records[id]).filter(Boolean), removed=[...removedDownloads];
  dirtyDownloads.clear();removedDownloads.clear();
  await persist();
  if(nativeFeatures.includes('download_deltas'))post({type:'download_delta',records:changed,removed});else registry();
}
function changedDownload(id,terminal=false) {
  dirtyDownloads.add(String(id));
  if(terminal)return flushDownloads();
  if(!progressTimer)progressTimer=setTimeout(()=>{downloadQueue=downloadQueue.then(flushDownloads).catch(()=>{});},250);
}
api.downloads.onCreated.addListener(item=>{
  downloadQueue=downloadQueue.catch(()=>{}).then(async()=>{await ready;if(!enabled || item.incognito)return;
    const id=String(item.id);records[id]=downloadRecord(item,{download_id:id,sequence:++sequence,started_at:Date.now()/1000});await changedDownload(id,item.state!=='in_progress');});
});
api.downloads.onChanged.addListener(delta=>{
  downloadQueue=downloadQueue.catch(()=>{}).then(async()=>{await ready;if(!records[String(delta.id)])return;
    const [item]=await api.downloads.search({id:delta.id});if(item){records[String(item.id)]=downloadRecord(item,records[String(item.id)]);await changedDownload(item.id,item.state!=='in_progress');}});
});
async function reconcile() {for(const id of Object.keys(records)){const [item]=await api.downloads.search({id:Number(id)});if(item)records[id]=downloadRecord(item,records[id]);else records[id]={...records[id],state:'interrupted',error:'download_removed'};}await persist();registry();}
async function createReadyTab(url) {
  if(!/^https?:\/\//.test(url || ''))throw Error('restricted_url: supply http(s) URL');
  const created=await api.tabs.create({url,active:false});
  const deadline=Date.now()+20000;
  while(Date.now()<deadline) {
    let tab;try{tab=await api.tabs.get(created.id);}catch{throw Error(`tab_closed: ${created.id}`);}
    if(tab.incognito)throw Error('restricted_tab');
    if(tab.url && tab.url!=='about:blank' && !/^https?:\/\//.test(tab.url))throw Error(`restricted_redirect: ${created.id}`);
    const frames=await api.webNavigation.getAllFrames({tabId:created.id});
    const main=frames?.find(f=>f.frameId===0);
    if(ordinary(tab) && main?.url===tab.url && !tab.pendingUrl)return info(tab);
    await pause(50);
  }
  throw Error(`tab_ready_timeout: ${created.id}; tab was not recreated`);
}
async function mapFrames(frames,fn) {
  const results=new Array(frames.length);let next=0;
  await Promise.all(Array.from({length:Math.min(4,frames.length)},async()=>{while(next<frames.length){const i=next++;results[i]=await fn(frames[i]);}}));
  return results;
}
async function execute(message) {
  const {operation:op,args:a={},tab_id:id}=message;
  const active=()=>{if(message.nativePort && port!==message.nativePort)throw Error('bridge_disconnected');};
  active();
  if(op==='recording_context')return page(id,op,a);
  if(op==='capabilities')return {browser:firefox?'firefox':'chrome',backend:'extension',tabs:true,read_page:true,input:firefox?'synthetic':'debugger',screenshots:true,downloads:true,raw_cdp:!firefox,js:!firefox,full_page_screenshot:!firefox,uploads:false,native_dialogs:false,network_idle:false,document_token:true,research_capture:true};
  if(op==='list_tabs')return normalTabs();
  if(op==='switch_tab') {const tab=await getTab(a.target);if(a.activate)await api.tabs.update(tab.id,{active:true});detached.delete(tab.id);return info(tab);}
  if(op==='activate_tab') {const tab=await getTab(a.target);await api.tabs.update(tab.id,{active:true});return info(tab);}
  if(op==='current_tab')return info(await getTab(id));
  if(op==='new_tab')return createReadyTab(a.url);
  if(op==='close_tab'){await getTab(id);await api.tabs.remove(Number(id));return true;}
  if(op==='goto_url'){await getTab(id);if(!/^https?:\/\//.test(a.url))throw Error('restricted_url');await api.tabs.update(Number(id),{url:a.url});return {};}
  if(op==='downloads_configure'){if(a.browser_dir || a.browser_context_id)throw Error('unsupported_capability: download directory/context override');enabled=true;await persist();return {enabled:true,cursor:sequence,browser_dir:null,browser_context_id:null};}
  if(op==='downloads_disable'){enabled=false;await persist();return {enabled:false};}
  if(op.startsWith('download_')) {
    await downloadQueue;
    if(op==='download_list')return {downloads:Object.values(records),cursor:sequence};
    const record=records[String(a.download_id)];if(!record)throw Error('unknown_download');
    if(op==='download_status')return {download:record};
    if(op==='download_cancel'){await api.downloads.cancel(Number(a.download_id));return {download:{...record,cancel_requested:true}};}
    if(op==='download_forget'){if(record.state==='inProgress')throw Error('download_in_progress');delete records[String(a.download_id)];removedDownloads.add(String(a.download_id));await flushDownloads();return {ok:true};}
  }
  await getTab(id);
  if(op==='document_token')return page(id,op,a);
  if(op==='read_page') {
    const epoch=generations.get(Number(id)) || 0;
    const result=await page(id,op,a);result.frames=[];
    if(a.include_frames!==false){
      const frames=await api.webNavigation.getAllFrames({tabId:Number(id)});
      result.frames=await mapFrames((frames || []).filter(f=>f.frameId!==0),async frame=>{
        try{await page(id,'page_info',{frame_id:frame.frameId});return {frame_id:frame.frameId,url:frame.url,accessible:true};}
        catch{return {frame_id:frame.frameId,url:frame.url,accessible:false};}
      });
    }
    if((generations.get(Number(id)) || 0)!==epoch)throw Error('navigation_changed');
    return result;
  }
  if(op==='cdp')return cdp(id,a.method,a.params);
  if(op==='js'){const result=await cdp(id,'Runtime.evaluate',{expression:a.expression,returnByValue:true,awaitPromise:true});if(result.exceptionDetails)throw Error(result.exceptionDetails.text);return result.result.value;}
  if(op==='capture_screenshot') {
    if(firefox){if(a.full)throw Error('unsupported_capability: full_page_screenshot');return {data:(await api.tabs.captureTab(Number(id),{format:'png'})).split(',')[1]};}
    await cdp(id,'Page.bringToFront',{});
    const options={format:'png',captureBeyondViewport:!!a.full};
    if(a.full){const metrics=await cdp(id,'Page.getLayoutMetrics',{});const size=metrics.cssContentSize || metrics.contentSize;options.clip={x:0,y:0,width:size.width,height:size.height,scale:1};}
    return cdp(id,'Page.captureScreenshot',options);
  }
  if(!firefox && op==='click_at_xy') {await cdp(id,'Input.dispatchMouseEvent',{type:'mousePressed',x:a.x,y:a.y,button:a.button || 'left',clickCount:a.clicks || 1});await cdp(id,'Input.dispatchMouseEvent',{type:'mouseReleased',x:a.x,y:a.y,button:a.button || 'left',clickCount:a.clicks || 1});return true;}
  if(op==='click_element') {
    const epoch=generations.get(Number(id)) || 0;
    const box=await page(id,'prepare_click',a);
    if((generations.get(Number(id)) || 0)!==epoch)throw Error('navigation_changed');
    active();
    if(firefox || a.frame_id)return page(id,'commit_click',{...a,...box});
    await cdp(id,'Input.dispatchMouseEvent',{type:'mouseMoved',x:box.x,y:box.y});
    await page(id,'verify_click',{...a,...box});
    if((generations.get(Number(id)) || 0)!==epoch)throw Error('navigation_changed');
    active();
    return execute({...message,operation:'click_at_xy',args:box});
  }
  if(!firefox && op==='type_text') {await cdp(id,'Input.insertText',{text:a.text});return true;}
  if(!firefox && op==='press_key') {const codes={Enter:13,Tab:9,Escape:27,Backspace:8,ArrowLeft:37,ArrowUp:38,ArrowRight:39,ArrowDown:40};const p={key:a.key,modifiers:a.modifiers || 0,windowsVirtualKeyCode:codes[a.key] || (a.key.length===1?a.key.toUpperCase().charCodeAt(0):0)};await cdp(id,'Input.dispatchKeyEvent',{type:'keyDown',...p});await cdp(id,'Input.dispatchKeyEvent',{type:'keyUp',...p});return true;}
  return page(id,op,a);
}
async function onNative(message, nativePort=port) {
  if(port!==nativePort)return;
  if(message.type==='status'){nativeFeatures=message.features || [];connected=message.connected;harness=message.harness || harness;error=message.error || '';if(connected)registry();return;}
  if(!message.id)return;
  if(requests.has(message.id)){const reply=await requests.get(message.id);if(port===nativePort)post(reply);return;}
  if(requests.size>=4096){post({id:message.id,error:'request_limit: disconnect and reconnect'});return;}
  const promise=(async()=>{try{return {id:message.id,result:await execute({...message,nativePort})};}catch(e){return {id:message.id,error:e.message};}})();
  requests.set(message.id,promise);const reply=await promise;if(port===nativePort)post(reply);
}
async function connect() {
  await ready;if(port)return;wanted=true;
  helloSent=false;
  port=api.runtime.connectNative(HOST);
  const active=port;
  port.onMessage.addListener(message=>onNative(message,active));
  port.onDisconnect.addListener(()=>{if(port===active){port=null;helloSent=false;connected=false;error=api.runtime.lastError?.message || 'Native host disconnected';}});
  await api.storage.local.set({wanted});
  if(port!==active)return;
  post({type:'hello',connection_id:connectionId,browser:firefox?'firefox':'chrome',label,features:FEATURES});
  helloSent=true;
  await reconcile();
}
async function disconnect() {
  const old=port;port=null;helloSent=false;connected=false;wanted=false;enabled=false;await api.storage.local.set({wanted});await persist();if(old)old.disconnect();
  for(const tabId of attached)try{await api.debugger.detach({tabId});}catch{}attached.clear();requests.clear();
}
api.runtime.onMessage.addListener((message,sender,respond)=>{
  if(sender.url !== api.runtime.getURL('popup.html') || !message.popup)return;
  (async()=>{await ready;if(message.popup==='connect'){label=message.label || label;await api.storage.local.set({label});await connect();}
    if(message.popup==='disconnect')await disconnect();return {connected,error,connection_id:connectionId,label,harness,wanted};})().then(respond,e=>respond({error:e.message}));return true;
});

ready.then(()=>{if(wanted)return connect();}).catch(e=>{error=e.message;});

function resumeConnection(){ready.then(()=>{if(wanted)return connect();}).catch(e=>{error=e.message;});}
api.runtime.onStartup.addListener(resumeConnection);
api.runtime.onInstalled.addListener(resumeConnection);

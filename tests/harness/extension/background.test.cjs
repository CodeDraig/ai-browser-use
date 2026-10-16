const test=require('node:test');
const assert=require('node:assert/strict');
const vm=require('node:vm');
const fs=require('node:fs');
const {webcrypto}=require('node:crypto');
function fixture(firefox=true){
  const listeners={};const sent=[];const store={};let calls=0, connects=0;
  const event=name=>({addListener:fn=>listeners[name]=fn});
  const api={
    storage:{local:{get:async()=>store,set:async x=>Object.assign(store,x)}},
    tabs:{update:async()=>{},get:async id=>({id,url:id===3?'chrome://settings':'https://example.org/',incognito:id===2}),query:async()=>[{id:1,url:'https://example.org/'},{id:2,url:'https://example.org/',incognito:true},{id:3,url:'about:blank'}],onRemoved:event('removed'),sendMessage:async()=>({result:true})},
    webNavigation:{onCommitted:event('navigation'),getAllFrames:async()=>[]},
    scripting:{executeScript:async()=>{calls++;}},
    downloads:{onCreated:event('created'),onChanged:event('changed'),search:async()=>[]},
    debugger:{onDetach:event('detach'),attach:async()=>{},sendCommand:async()=>({}),detach:async()=>{}},
    runtime:{...(firefox?{getBrowserInfo:async()=>({name:'Firefox'})}:{}),onStartup:event('startup'),onInstalled:event('installed'),onMessage:event('popup'),connectNative:()=>{connects++;return {postMessage:x=>sent.push(x),onMessage:event('native'),onDisconnect:event('disconnect'),disconnect(){}};}}
  };
  const context=vm.createContext({...(firefox?{browser:api}:{chrome:api}),crypto:webcrypto,console,setTimeout,clearTimeout});
  vm.runInContext(fs.readFileSync('browser_harness/extension_assets/background.js','utf8'),context);
  return {context,api,listeners,sent,store,call:expr=>vm.runInContext(expr,context),calls:()=>calls,connects:()=>connects};
}
test('ordinary tabs and Firefox capability errors',async()=>{const f=fixture();await f.call('ready');assert.equal((await f.call("execute({operation:'list_tabs'})")).length,1);
 await assert.rejects(f.call("execute({operation:'page_info',tab_id:2})"),/restricted_tab/);
 await assert.rejects(f.call("execute({operation:'cdp',tab_id:1,args:{method:'Runtime.evaluate'}})"),/unsupported_capability/);
 await assert.rejects(f.call("execute({operation:'capture_screenshot',tab_id:1,args:{full:true}})"),/full_page_screenshot/);
});
test('duplicate requests execute only once',async()=>{const f=fixture();await f.call('ready');await f.call('connect()');const req={id:'one',operation:'page_info',tab_id:1};await f.listeners.native(req);await f.listeners.native(req);assert.equal(f.calls(),1);assert.equal(f.sent.filter(x=>x.id==='one').length,2);});
test('Chrome detach requires explicit tab selection',async()=>{const f=fixture(false);await f.call('ready');f.listeners.detach({tabId:1});await assert.rejects(f.call("execute({operation:'cdp',tab_id:1,args:{method:'Runtime.evaluate'}})"),/debugger_detached/);await f.call("execute({operation:'switch_tab',args:{target:'1'}})");await f.call("execute({operation:'cdp',tab_id:1,args:{method:'Runtime.evaluate'}})");});
test('navigation invalidates a page operation',async()=>{const f=fixture();await f.call('ready');f.api.scripting.executeScript=async()=>f.listeners.navigation({tabId:1,frameId:0});await assert.rejects(f.call("execute({operation:'page_info',tab_id:1})"),/navigation_changed/);});
test('download records exclude private items and survive reload data',async()=>{const f=fixture();await f.call('ready');await f.call("execute({operation:'downloads_configure',args:{}})");f.listeners.created({id:1,filename:'/tmp/a',url:'https://example.org/a',state:'in_progress',bytesReceived:0,totalBytes:10,incognito:true});await f.call('downloadQueue');assert.equal(Object.keys(f.store.records).length,0);f.listeners.created({id:2,filename:'/tmp/b',url:'https://example.org/b',state:'complete',bytesReceived:10,totalBytes:10});await f.call('downloadQueue');assert.equal(f.store.records['2'].state,'completed');});
test('Chrome browser namespace alias is not mistaken for Firefox',async()=>{const f=fixture(false);f.context.browser=f.api;assert.equal((await f.call("execute({operation:'capabilities'})")).browser,'chrome');});
test('full Chrome screenshots use content dimensions',async()=>{const f=fixture(false);const commands=[];f.api.debugger.sendCommand=async(source,method,params)=>{commands.push({method,params});return method==='Page.getLayoutMetrics'?{cssContentSize:{width:800,height:2000}}:{data:'abc'};};await f.call('ready');await f.call("execute({operation:'capture_screenshot',tab_id:1,args:{full:true}})");assert.equal(commands.at(-1).params.clip.height,2000);});

test('concurrent startup events create one native port',async()=>{const f=fixture();await f.call('ready');await Promise.all([f.call('connect()'),f.call('connect()'),f.call('connect()')]);assert.equal(f.connects(),1);});

test('page helper is injected once per frame and invalidated on navigation',async()=>{
 const f=fixture();await f.call('ready');
 await f.call("execute({operation:'page_info',tab_id:1})");
 await f.call("execute({operation:'page_info',tab_id:1})");assert.equal(f.calls(),1);
 f.listeners.navigation({tabId:1,frameId:0});
 await f.call("execute({operation:'page_info',tab_id:1})");assert.equal(f.calls(),2);
});
test('new tabs wait for a committed ordinary document without recreation',async()=>{
 const f=fixture();let creates=0,reads=0;
 f.api.tabs.create=async()=>{creates++;return {id:8,pendingUrl:'https://example.org/'}};
 f.api.tabs.get=async()=>++reads<3?{id:8,url:'about:blank',pendingUrl:'https://example.org/'}:{id:8,url:'https://example.org/'};
 f.api.webNavigation.getAllFrames=async()=>[{frameId:0,url:'https://example.org/'}];
 const result=await f.call("execute({operation:'new_tab',args:{url:'https://example.org/'}})");
 assert.equal(result.targetId,'8');assert.equal(creates,1);assert.ok(reads>=3);
 f.api.tabs.get=async()=>({id:8,url:'chrome://settings'});
 await assert.rejects(f.call("execute({operation:'new_tab',args:{url:'https://example.org/'}})"),/restricted_redirect/);
 f.api.tabs.get=async()=>{throw Error('closed')};
 await assert.rejects(f.call("execute({operation:'new_tab',args:{url:'https://example.org/'}})"),/tab_closed/);
});
test('frame probing is bounded and optional',async()=>{
 const f=fixture();let active=0,peak=0,frames=0;
 f.api.webNavigation.getAllFrames=async()=>{frames++;return Array.from({length:10},(_,i)=>({frameId:i+1,url:'https://example.org/'}));};
 f.api.tabs.sendMessage=async(id,msg)=>{active++;peak=Math.max(peak,active);await new Promise(r=>setTimeout(r,2));active--;return {result:{text:'ok'}};};
 await f.call("execute({operation:'read_page',tab_id:1,args:{include_frames:false}})");assert.equal(frames,0);
 const result=await f.call("execute({operation:'read_page',tab_id:1})");assert.equal(result.frames.length,10);assert.equal(peak,4);
});
test('download deltas distinguish interruption and cancellation and remove records',async()=>{
 const f=fixture();await f.call('ready');await f.call('connect()');
 await f.call("onNative({type:'status',connected:true,features:['download_deltas']})");
 await f.call("execute({operation:'downloads_configure'})");
 f.listeners.created({id:4,filename:'/tmp/d',url:'https://example.org/d',state:'interrupted',error:'NETWORK_FAILED',bytesReceived:0,totalBytes:1});
 await f.call('downloadQueue');assert.equal(f.store.records['4'].state,'interrupted');
 assert.equal(f.sent.at(-1).type,'download_delta');
 await f.call("execute({operation:'download_forget',args:{download_id:'4'}})");assert.deepEqual(Array.from(f.sent.at(-1).removed),['4']);
 assert.equal((await f.call("downloadRecord({filename:'/tmp/d',state:'interrupted',error:'USER_CANCELED'}, {})")).state,'canceled');
});

test('new-tab timeout returns existing ID and never recreates',async()=>{
 const f=fixture();let time=0,creates=0;
 f.context.Date={now:()=>time+=21000};
 f.api.tabs.create=async()=>{creates++;return {id:9};};
 await assert.rejects(f.call("execute({operation:'new_tab',args:{url:'https://example.org/'}})"),/tab_ready_timeout: 9/);
 assert.equal(creates,1);
});
test('disconnect during click preparation prevents dispatch',async()=>{
 const f=fixture(false);await f.call('ready');await f.call('connect()');let inputs=0;
 f.api.tabs.sendMessage=async()=>{await f.call('disconnect()');return {result:{x:10,y:10}};};
 f.api.debugger.sendCommand=async()=>{inputs++;return {};};
 await f.call("onNative({id:'click',operation:'click_element',tab_id:1,args:{selector:'#a'}})");
 assert.equal(inputs,0);
});
test('progress updates coalesce, terminal updates flush immediately',async()=>{
 const f=fixture();await f.call('ready');await f.call('connect()');
 await f.call("onNative({type:'status',connected:true,features:['download_deltas']})");
 await f.call("execute({operation:'downloads_configure'})");
 const timers=[];f.context.setTimeout=(fn,ms)=>{timers.push({fn,ms});return 99;};f.context.clearTimeout=()=>{};
 f.listeners.created({id:5,filename:'/tmp/e',state:'in_progress',bytesReceived:1,totalBytes:10});await f.call('downloadQueue');
 f.api.downloads.search=async()=>[{id:5,filename:'/tmp/e',state:'in_progress',bytesReceived:3,totalBytes:10}];
 f.listeners.changed({id:5});await f.call('downloadQueue');assert.equal(timers.length,1);assert.equal(timers[0].ms,250);
 f.api.downloads.search=async()=>[{id:5,filename:'/tmp/e',state:'complete',bytesReceived:10,totalBytes:10,fileSize:10}];
 f.listeners.changed({id:5});await f.call('downloadQueue');assert.equal(f.sent.at(-1).type,'download_delta');assert.equal(f.sent.at(-1).records[0].state,'completed');
});

test('unrelated subframe navigation does not invalidate main-page reads',async()=>{
 const f=fixture();await f.call('ready');
 f.api.tabs.sendMessage=async()=>{f.listeners.navigation({tabId:1,frameId:42});return {result:{text:'main'}};};
 assert.equal((await f.call("execute({operation:'read_page',tab_id:1,args:{include_frames:false}})")).text,'main');
 await assert.rejects(f.call("execute({operation:'read_page',tab_id:1,args:{frame_id:42,include_frames:false}})"),/navigation_changed/);
});

test('native handshake precedes download registry updates during startup',async()=>{
 const f=fixture(false);await f.call('ready');
 f.api.storage.local.set=async state=>{
   Object.assign(f.store,state);
   if(state.wanted)await f.call('registry()');
 };
 await f.call('connect()');
 assert.equal(f.sent[0].type,'hello');
 assert.equal(f.sent.filter(message=>message.type==='hello').length,1);
});

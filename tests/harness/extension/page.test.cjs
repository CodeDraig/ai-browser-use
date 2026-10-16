const test=require('node:test');const assert=require('node:assert/strict');const vm=require('node:vm');const fs=require('node:fs');const {webcrypto}=require('node:crypto');
function fixture(){
 let listener,clock=0,clicks=0,covered=false,moves=0;
 const element={isConnected:true,disabled:false,getAttribute:()=>null,getClientRects:()=>[1],getBoundingClientRect:()=>({left:10+Math.min(moves++,2),top:10,right:30+Math.min(moves,2),bottom:30}),scrollIntoView:()=>{},contains:()=>false,click:()=>clicks++};
 const document={querySelectorAll:()=>[element],elementFromPoint:()=>covered?{}:element};
 vm.runInNewContext(fs.readFileSync('browser_harness/extension_assets/page.js','utf8'),{browser:{runtime:{onMessage:{addListener:fn=>listener=fn}}},document,crypto:webcrypto,innerWidth:800,innerHeight:600,getComputedStyle:()=>({visibility:'visible'}),Date:{now:()=>clock},setTimeout:fn=>{clock+=100;queueMicrotask(fn);}});
 return {element,setCovered:v=>covered=v,clicks:()=>clicks,run:(operation,args={selector:'#target'})=>new Promise(resolve=>listener({bhPage:true,operation,args},{},resolve))};
}
test('stable target clicked once; covered or detached target never clicked',async()=>{
 const f=fixture();assert.equal((await f.run('click_element')).result,true);assert.equal(f.clicks(),1);
 f.setCovered(true);assert.match((await f.run('click_element')).error,/element_obstructed/);assert.equal(f.clicks(),1);
 f.setCovered(false);f.element.isConnected=false;assert.match((await f.run('click_element')).error,/element_detached/);
});
test('replacement between preparation and dispatch is rejected',async()=>{
 const f=fixture();const prepared=await f.run('prepare_click');
 f.element.isConnected=false;
 const response=await f.run('verify_click',{selector:'#target',...prepared.result});assert.match(response.error,/element_detached/);assert.equal(f.clicks(),0);
});

test('click waits for visibility and enabled state',async()=>{
 const f=fixture();let reads=0;
 f.element.getClientRects=()=>++reads>2?[1]:[];
 let disabled=0;Object.defineProperty(f.element,'disabled',{get:()=>++disabled<3});
 assert.equal((await f.run('click_element')).result,true);assert.equal(f.clicks(),1);
});

test('recording context includes geometry and password type without form values',async()=>{
 let listener;
 const input={type:'password',value:'private-password',getBoundingClientRect:()=>({x:12,y:20,width:100,height:30})};
 const document={activeElement:input,body:{},documentElement:{}};
 vm.runInNewContext(fs.readFileSync('browser_harness/extension_assets/page.js','utf8'),{
   browser:{runtime:{onMessage:{addListener:fn=>listener=fn}}},document,crypto:webcrypto,
   location:{href:'https://example.test/'},innerWidth:800,innerHeight:600,scrollX:0,scrollY:0,devicePixelRatio:2,
 });
 const response=await new Promise(resolve=>listener({bhPage:true,operation:'recording_context'},{},resolve));
 assert.equal(response.result.input,'password');assert.equal(response.result.dpr,2);
 assert.equal(response.result.box.w,100);assert.equal(response.result.w,800);
 assert.equal(JSON.stringify(response).includes('private-password'),false);
});

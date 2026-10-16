const test=require('node:test');
const assert=require('node:assert/strict');
const vm=require('node:vm');
const fs=require('node:fs');
test('opening popup as a local file explains installation and disables unusable controls',()=>{
  for(const chrome of [undefined,{}]){
    const elements=Object.fromEntries(['#status','#connect','#disconnect','#label'].map(id=>[id,{}]));
    const context=vm.createContext({chrome,document:{querySelector:id=>elements[id]},setInterval:()=>{throw Error('must not poll unavailable APIs');}});
    vm.runInContext(fs.readFileSync('browser_harness/extension_assets/popup.js','utf8'),context);
    assert.match(elements['#status'].textContent,/Load unpacked/);
    assert.equal(elements['#connect'].disabled,true);
    assert.equal(elements['#disconnect'].disabled,true);
  }
});

test('popup acknowledges connection and shows command failures',async()=>{
  const elements=Object.fromEntries(['#status','#connect','#disconnect','#label'].map(id=>[id,{}]));
  let finish;
  let fail=false;
  const runtime={sendMessage:async message=>{
    if(message.popup==='status')return {connected:false};
    if(fail)throw Error('Native host unavailable');
    return new Promise(resolve=>{finish=resolve;});
  }};
  vm.runInNewContext(fs.readFileSync('browser_harness/extension_assets/popup.js','utf8'),{
    chrome:{runtime},document:{querySelector:id=>elements[id]},setInterval:()=>{}
  });
  await new Promise(resolve=>setImmediate(resolve));
  const pending=elements['#connect'].onclick();
  assert.equal(elements['#status'].textContent,'Connecting…');
  assert.equal(elements['#connect'].disabled,true);
  finish({connected:true,harness:'localhost:8766'});
  await pending;
  assert.match(elements['#status'].textContent,/Connected — ready/);
  assert.equal(elements['#connect'].textContent,'Connected');
  assert.equal(elements['#connect'].disabled,true);
  assert.equal(elements['#disconnect'].disabled,false);
  fail=true;
  await elements['#disconnect'].onclick();
  assert.match(elements['#status'].textContent,/Disconnect failed: Native host unavailable/);
});

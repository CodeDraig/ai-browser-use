/* Bundled operations only: never evaluate code received from page content. */
(() => {
  if (globalThis.__browserHarnessPage) return;
  globalThis.__browserHarnessPage = true;
  const api = globalThis.browser || chrome;
  function visible(e) { return !!(e.getClientRects().length && getComputedStyle(e).visibility !== 'hidden'); }
  function selector(e) {
    if (e.id) return '#' + CSS.escape(e.id);
    const parts = [];
    while (e && e.nodeType === 1) {
      const tag = e.localName;
      const siblings = e.parentElement ? [...e.parentElement.children].filter(x => x.localName === tag) : [e];
      parts.unshift(tag + (siblings.length > 1 ? `:nth-of-type(${siblings.indexOf(e)+1})` : ''));
      e = e.parentElement;
    }
    return parts.join(' > ');
  }
  function element(s) {
    const matches = document.querySelectorAll(s);
    if (matches.length !== 1) throw Error(matches.length ? 'ambiguous_selector' : 'element_not_found');
    const e = matches[0];
    if (!visible(e)) throw Error('element_not_visible');
    return e;
  }
  function input(e, value) {
    if (e.disabled || e.readOnly) throw Error('element_not_editable');
    if (e.isContentEditable) e.textContent = value;
    else {
      const prototype = e instanceof HTMLTextAreaElement ? HTMLTextAreaElement.prototype : e instanceof HTMLSelectElement ? HTMLSelectElement.prototype : HTMLInputElement.prototype;
      const setter = Object.getOwnPropertyDescriptor(prototype, 'value').set;
      setter.call(e, value);
    }
    e.dispatchEvent(new Event('input', {bubbles:true}));
    e.dispatchEvent(new Event('change', {bubbles:true}));
  }
  const documentToken=crypto.randomUUID();
  const sleep=ms=>new Promise(resolve=>setTimeout(resolve,ms));
  const clickTargets=new Map();
  function point(e) {
    const r=e.getBoundingClientRect();
    const left=Math.max(0,r.left),right=Math.min(innerWidth,r.right),top=Math.max(0,r.top),bottom=Math.min(innerHeight,r.bottom);
    if(right<=left || bottom<=top)throw Error('element_not_visible');
    return {x:(left+right)/2,y:(top+bottom)/2};
  }
  function verify(a) {
    const e=element(a.selector);
    if(a.click_token && clickTargets.get(a.click_token)!==e)throw Error('element_detached');
    if(!e.isConnected)throw Error('element_detached');
    if(e.disabled || e.getAttribute('aria-disabled')==='true')throw Error('element_disabled');
    const hit=document.elementFromPoint(a.x,a.y);
    if(!hit || !(hit===e || e.contains(hit)))throw Error('element_obstructed');
    if(a.click_token)clickTargets.delete(a.click_token);
    return e;
  }
  async function prepare(a) {
    const deadline=Date.now()+5000;let previous=null,last='click_timeout',target=null,scrolled=false;
    while(Date.now()<deadline) {
      if(target && !target.isConnected)throw Error('element_detached');
      try {
        const e=element(a.selector);
        if(target && target!==e)throw Error('element_detached');
        target=e;
        if(!scrolled){e.scrollIntoView({block:'center',inline:'center',behavior:'instant'});scrolled=true;}
        const p=point(e);verify({...a,...p});
        if(previous && Math.abs(p.x-previous.x)<.5 && Math.abs(p.y-previous.y)<.5){
          const token=crypto.randomUUID();clickTargets.clear();clickTargets.set(token,e);return {...p,click_token:token};
        }
        previous=p;
      }catch(error){
        if(['element_detached','ambiguous_selector'].includes(error.message))throw error;
        last=error.message;previous=null;
      }
      await sleep(50);
    }
    throw Error(last==='click_timeout'?last:last+': click_timeout');
  }
  const operations = {
    document_token:()=>({token:documentToken,url:location.href}),
    prepare_click:prepare,
    verify_click:a=>{verify(a);return true;},
    commit_click:a=>{verify(a).click();return true;},
    recording_context: () => {
      const context={url:location.href,title:document.title,w:innerWidth,h:innerHeight,sx:scrollX,sy:scrollY,dpr:devicePixelRatio};
      const element=document.activeElement;
      if(element && element!==document.body && element!==document.documentElement) {
        const box=element.getBoundingClientRect();
        if(box.width || box.height)context.box={x:box.x,y:box.y,w:box.width,h:box.height};
        context.input=String(element.type || element.tagName || '').toLowerCase();
      }
      return context;
    },
    page_info: () => ({url:location.href,title:document.title,w:innerWidth,h:innerHeight,sx:scrollX,sy:scrollY,pw:document.documentElement.scrollWidth,ph:document.documentElement.scrollHeight}),
    read_page: () => ({document_token:documentToken,url:location.href,title:document.title,text:document.body?.innerText || '',
      links:[...document.querySelectorAll('a[href]')].filter(visible).map(e=>({text:e.innerText,url:e.href,selector:selector(e)})),
      controls:[...document.querySelectorAll('input,textarea,select,button,[role="button"],[contenteditable="true"]')].filter(visible).map(e=>({tag:e.localName,type:e.type || '',role:e.getAttribute('role'),label:e.getAttribute('aria-label') || e.labels?.[0]?.innerText || e.innerText || '',value:e.type === 'password' ? null : e.value,selector:selector(e)}))}),
    click_element: async a => {const p=await prepare(a);const e=verify({...a,...p});e.click();return true;},
    focus: a => { element(a.selector).focus(); return true; },
    click_at_xy: a => {
      if ((a.button && a.button !== 'left') || (a.clicks && a.clicks !== 1)) throw Error('unsupported_capability: firefox mouse button or double click');
      const e=document.elementFromPoint(a.x,a.y); if(!e) throw Error('element_not_found'); e.focus(); e.click(); return true;
    },
    fill_input: a => {const e=element(a.selector); e.focus(); input(e, a.clear_first === false ? (e.value || '') + a.text : a.text); return true;},
    type_text: a => {const e=document.activeElement; if(!(e instanceof HTMLInputElement || e instanceof HTMLTextAreaElement || e.isContentEditable)) throw Error('element_not_editable');
      if(e.isContentEditable) input(e, (e.textContent || '') + a.text);
      else { const start=e.selectionStart ?? e.value.length, end=e.selectionEnd ?? start; input(e,e.value.slice(0,start)+a.text+e.value.slice(end)); if(e.selectionStart!==null) e.setSelectionRange(start+a.text.length,start+a.text.length); } return true;},
    press_key: a => {
      const e=document.activeElement;
      if(a.modifiers) throw Error('unsupported_capability: firefox key modifiers');
      if(!['Enter','Tab','Escape','Backspace','ArrowLeft','ArrowRight','ArrowUp','ArrowDown'].includes(a.key)) throw Error('unsupported_capability: key '+a.key);
      const allowed=e.dispatchEvent(new KeyboardEvent('keydown',{key:a.key,bubbles:true,cancelable:true}));
      if(allowed && a.key==='Enter' && e.form && !(e instanceof HTMLTextAreaElement)) e.form.requestSubmit();
      if(allowed && a.key==='Tab') {const list=[...document.querySelectorAll('a[href],button,input,textarea,select,[tabindex]')].filter(x=>visible(x)&&!x.disabled&&x.tabIndex>=0);list[(list.indexOf(e)+1)%list.length]?.focus();}
      if(allowed && a.key==='Backspace' && typeof e.value==='string') {const start=e.selectionStart??e.value.length,end=e.selectionEnd??start;input(e,e.value.slice(0,Math.max(0,start-(start===end?1:0)))+e.value.slice(end));}
      e.dispatchEvent(new KeyboardEvent('keyup',{key:a.key,bubbles:true})); return true;
    },
    scroll: a => {let e=document.elementFromPoint(a.x,a.y); while(e && !(e.scrollHeight>e.clientHeight && /auto|scroll/.test(getComputedStyle(e).overflowY)))e=e.parentElement;(e || document.scrollingElement).scrollBy(a.dx || 0,a.dy ?? -300);return true;},
    element_exists: a => {const e=document.querySelector(a.selector);return !!e && (!a.visible || visible(e));},
    ready: () => document.readyState === 'complete'
  };
  api.runtime.onMessage.addListener((message, sender, respond) => {
    if(!message.bhPage) return;
    try {
      const fn=operations[message.operation]; if(!fn)throw Error('unsupported_capability: '+message.operation);
      Promise.resolve(fn(message.args || {})).then(result=>respond({result}),e=>respond({error:e.message}));
      return true;
    } catch(e) {respond({error:e.message});}
  });
})();

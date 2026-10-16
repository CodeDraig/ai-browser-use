'use strict';
const api = globalThis.browser || globalThis.chrome;
if (!api?.runtime?.sendMessage) {
  document.querySelector('#status').textContent = 'This is a local file, not an installed extension. In Chrome, open chrome://extensions, enable Developer mode, click Load unpacked, and select the chrome-extension folder. Then open Browser Harness from the toolbar Extensions menu.';
  document.querySelector('#connect').disabled = true;
  document.querySelector('#disconnect').disabled = true;
  document.querySelector('#label').disabled = true;
} else {
let labelInitialized=false, busy=false;
const status = document.querySelector('#status');
const connectButton = document.querySelector('#connect');
const disconnectButton = document.querySelector('#disconnect');
function render(state) {
  if (!state) throw Error('Extension background is unavailable. Reload the extension.');
  const heading = state.connected ? 'Connected — ready for browser-harness commands.' : state.wanted && !state.error ? 'Connecting…' : 'Disconnected';
  status.textContent = [heading, state.harness, state.error].filter(Boolean).join(' · ');
  connectButton.disabled = busy || !!state.connected;
  connectButton.textContent = state.connected ? 'Connected' : 'Connect';
  disconnectButton.disabled = busy || !(state.connected || state.wanted);
  if(!labelInitialized){document.querySelector('#label').value=state.label || '';labelInitialized=true;}
}
async function refresh() {
  if (busy) return;
  try { const state = await api.runtime.sendMessage({popup:'status'}); if (!busy) render(state); }
  catch(error){if (!busy) status.textContent=error.message;}
}
async function act(action) {
  busy=true;
  connectButton.disabled=true;
  disconnectButton.disabled=true;
  status.textContent=action==='connect' ? 'Connecting…' : 'Disconnecting…';
  try {
    const state = await api.runtime.sendMessage({popup:action, label:document.querySelector('#label').value});
    busy=false;
    render(state);
  } catch(error) {
    busy=false;
    status.textContent=`${action==='connect' ? 'Connection' : 'Disconnect'} failed: ${error.message}`;
    connectButton.disabled=false;
    disconnectButton.disabled=false;
  }
}
connectButton.onclick = () => act('connect');
disconnectButton.onclick = () => act('disconnect');
refresh();
setInterval(refresh,1000);
}

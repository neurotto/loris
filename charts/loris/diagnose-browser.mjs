import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import {spawn} from 'node:child_process';

const [base, executable, credentialsFile, output] = process.argv.slice(2);
const origin = new URL(base);
if (origin.protocol !== 'http:' || !['localhost', '127.0.0.1'].includes(origin.hostname)) {
  throw new Error('Browser authentication is restricted to loopback HTTP.');
}
if (typeof WebSocket === 'undefined') throw new Error('Node.js >=22 is required.');
const passwordLine = fs.readFileSync(credentialsFile, 'utf8').split('\n')
  .find(line => line.startsWith('LORIS_FRONTEND_ADMIN_PASSWORD='));
if (!passwordLine) throw new Error('Local admin credential is missing.');
const password = passwordLine.slice(passwordLine.indexOf('=') + 1);
const profile = fs.mkdtempSync(path.join(os.tmpdir(), 'loris-diagnostic-browser-'));
const chrome = spawn(executable, [
  '--headless', '--disable-gpu', '--remote-debugging-port=0', '--remote-debugging-address=127.0.0.1',
  `--user-data-dir=${profile}`, '--no-first-run', '--no-default-browser-check', 'about:blank',
], {stdio: ['ignore', 'ignore', 'pipe']});
let socket;
let current;
let sequence = 0;
const calls = new Map();
const listeners = new Set();
const report = {pages: [], note: 'Direct isolated Chrome session only; not the user browser or authenticated tunnel.'};

function safeUrl(value) {
  try {
    const parsed = new URL(value);
    return parsed.origin + parsed.pathname;
  } catch {
    return 'unknown URL';
  }
}

function send(method, params = {}) {
  const id = ++sequence;
  return new Promise((resolve, reject) => {
    const timer = setTimeout(() => {
      calls.delete(id);
      reject(new Error(`Browser command timed out: ${method}`));
    }, 25000);
    calls.set(id, {resolve, reject, timer});
    socket.send(JSON.stringify({id, method, params}));
  });
}

function waitForLoad() {
  return new Promise(resolve => {
    const finish = loaded => {
      clearTimeout(timer);
      listeners.delete(listener);
      resolve(loaded);
    };
    const listener = event => {
      if (event.method === 'Page.loadEventFired') finish(true);
    };
    const timer = setTimeout(() => finish(false), 15000);
    listeners.add(listener);
  });
}

function collectEvent(event) {
  for (const listener of listeners) listener(event);
  if (!current) return;
  const params = event.params;
  if (event.method === 'Runtime.exceptionThrown') {
    current.errors.push((params.exceptionDetails.exception?.description || params.exceptionDetails.text)
      .split('\n')[0].replace(/https?:\/\/[^\s)]+/g, value => safeUrl(value)));
  }
  if (event.method === 'Network.requestWillBeSent') {
    current.requests.set(params.requestId, {
      url: safeUrl(params.request.url), type: params.type, started: params.timestamp, status: null,
    });
  }
  const request = current.requests.get(params?.requestId);
  if (!request) return;
  if (event.method === 'Network.responseReceived') request.status = params.response.status;
  if (event.method === 'Network.loadingFinished' || event.method === 'Network.loadingFailed') {
    request.ms = Math.round((params.timestamp - request.started) * 1000);
    request.done = true;
    if (event.method === 'Network.loadingFailed' && !params.canceled) {
      current.failed.push({url: request.url, error: params.errorText, blocked: params.blockedReason || null});
    }
  }
}

try {
  const endpoint = await new Promise((resolve, reject) => {
    const timer = setTimeout(() => reject(new Error('Chrome did not expose a debugging endpoint.')), 10000);
    chrome.once('error', error => { clearTimeout(timer); reject(error); });
    chrome.once('exit', () => { clearTimeout(timer); reject(new Error('Chrome exited before initialization.')); });
    let buffer = '';
    chrome.stderr.on('data', data => {
      buffer = (buffer + data.toString()).slice(-8000);
      const match = buffer.match(/DevTools listening on (ws:\/\/\S+)/);
      if (match) { clearTimeout(timer); resolve(new URL(match[1])); }
    });
  });
  const targets = await (await fetch(`http://${endpoint.host}/json/list`)).json();
  socket = new WebSocket(targets.find(target => target.type === 'page').webSocketDebuggerUrl);
  await new Promise((resolve, reject) => {
    socket.addEventListener('open', resolve, {once: true});
    socket.addEventListener('error', () => reject(new Error('Chrome debugging connection failed.')), {once: true});
  });
  socket.addEventListener('message', event => {
    const message = JSON.parse(event.data);
    if (message.id) {
      const call = calls.get(message.id);
      if (!call) return;
      clearTimeout(call.timer);
      calls.delete(message.id);
      if (message.error) call.reject(new Error(message.error.message));
      else call.resolve(message.result);
    } else collectEvent(message);
  });
  await send('Page.enable');
  await send('Runtime.enable');
  await send('Network.enable');
  const loginLoaded = waitForLoad();
  await send('Page.navigate', {url: base + '/login'});
  if (!await loginLoaded) throw new Error('Local login page did not finish loading.');
  const authentication = await send('Runtime.evaluate', {
    expression: `fetch('/login/Authentication', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify(${JSON.stringify({login:'true', command:'login', username:'admin', password})})}).then(response => response.status)`,
    awaitPromise: true, returnByValue: true,
  });
  if (authentication.result.value !== 200) throw new Error('Browser authentication failed.');

  for (const route of ['/', '/configuration/', '/module_manager/', '/user_accounts/', '/imaging_browser/']) {
    current = {path: route, errors: [], failed: [], requests: new Map(), problems: []};
    const started = Date.now();
    const loaded = waitForLoad();
    await send('Page.navigate', {url: base + route});
    if (await loaded) current.load_ms = Date.now() - started;
    else current.problems.push('Window load exceeded 15 seconds; inspect pending resource requests.');
    const result = await send('Runtime.evaluate', {
      expression: `new Promise(resolve => {
        const ready = () => {
          const workspace = document.getElementById('lorisworkspace');
          if (!workspace) return false;
          if (${JSON.stringify(route)} === '/module_manager/') return !!workspace.querySelector('select');
          if (${JSON.stringify(route)} === '/user_accounts/') return !!workspace.querySelector('table');
          if (${JSON.stringify(route)} === '/imaging_browser/') return !!workspace.querySelector('form, table, select');
          if (${JSON.stringify(route)} === '/configuration/') return !!workspace.querySelector('form');
          return workspace.innerText.trim().length > 20;
        };
        const inspect = () => {
          if (ready()) { clearTimeout(timer); observer.disconnect(); resolve({rendered:true}); }
        };
        const observer = new MutationObserver(inspect);
        const timer = setTimeout(() => { observer.disconnect(); resolve({rendered:false}); }, 10000);
        observer.observe(document.documentElement, {childList:true,subtree:true});
        inspect();
      })`, awaitPromise: true, returnByValue: true,
    });
    if (result.result.value?.rendered) current.render_ms = Date.now() - started;
    else current.problems.push('Expected page controls were not detected within 10 seconds (heuristic, not proof of a hang).');
    const routing = await send('Runtime.evaluate', {
      expression: `({baseURL: typeof loris === 'undefined' ? null : loris.BaseURL,
        externalScripts: Array.from(document.scripts).filter(script => script.src && new URL(script.src).origin !== location.origin).map(script => new URL(script.src).origin + new URL(script.src).pathname)})`,
      returnByValue: true,
    });
    current.routing = routing.result.value || {};
    if (current.routing.baseURL && new URL(current.routing.baseURL, base).origin !== origin.origin) {
      current.problems.push('LORIS BaseURL differs from browser origin; same-origin API requests may fail.');
    }
    if (current.routing.externalScripts?.length) current.problems.push('Page references cross-origin JavaScript assets.');
    await new Promise(resolve => {
      let idleTimer;
      const finish = () => {
        clearTimeout(idleTimer);
        clearTimeout(deadline);
        listeners.delete(listener);
        resolve();
      };
      const inspect = () => {
        clearTimeout(idleTimer);
        if ([...current.requests.values()].every(request => request.done)) idleTimer = setTimeout(finish, 250);
      };
      const listener = () => setImmediate(inspect);
      const deadline = setTimeout(finish, 2000);
      listeners.add(listener);
      inspect();
    });
    current.resources = [...current.requests.values()].map(({started: ignored, ...request}) => request);
    current.pending = current.resources.filter(request => !request.done).map(request => request.url);
    for (const pendingUrl of current.pending) current.problems.push(`Still in flight after bounded completion window: ${pendingUrl}`);
    for (const request of current.resources.filter(request => request.ms > 2000)) {
      current.problems.push(`Slow resource (${request.ms}ms): ${request.url}`);
    }
    for (const request of current.resources.filter(request => request.status >= 400)) {
      current.problems.push(`HTTP ${request.status}: ${request.url}`);
    }
    for (const failure of current.failed) current.problems.push(`${failure.error}: ${failure.url}`);
    for (const error of current.errors) current.problems.push(`JavaScript: ${error}`);
    if (current.render_ms > 2000) current.problems.push('Direct browser render exceeded 2 seconds.');
    current.verdict = current.errors.length || current.failed.length ? 'FAIL' : current.problems.length ? 'WARN' : 'OK';
    delete current.requests;
    report.pages.push(current);
    current = null;
  }
} catch (error) {
  report.error = error.message;
  process.exitCode = 1;
} finally {
  if (socket?.readyState === WebSocket.OPEN) {
    try { await send('Browser.close'); } catch { chrome.kill('SIGTERM'); }
    socket.close();
  } else chrome.kill('SIGTERM');
  if (chrome.exitCode === null && chrome.signalCode === null) {
    await new Promise(resolve => {
      const timer = setTimeout(() => { chrome.kill('SIGKILL'); resolve(); }, 3000);
      chrome.once('exit', () => { clearTimeout(timer); resolve(); });
    });
  }
  fs.rmSync(profile, {recursive: true, force: true});
  fs.writeFileSync(output, JSON.stringify(report, null, 2), {mode: 0o600});
}
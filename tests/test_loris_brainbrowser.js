const assert = require('node:assert/strict');
const {execFileSync} = require('node:child_process');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const vm = require('node:vm');
const {test} = require('node:test');

const root = path.resolve(__dirname, '..');
const relative = 'modules/brainbrowser/js/brainbrowser.loris.js';
const original = execFileSync('docker', [
  'run', '--rm', '--network', 'none', '--entrypoint', 'cat',
  process.env.LORIS_IMAGE || 'scilus/loris:29.0.0',
  '/var/www/loris/' + relative,
], {encoding: 'utf8'});
const temporary = fs.mkdtempSync(path.join(os.tmpdir(), 'loris-brainbrowser-'));
let patched;
try {
  fs.mkdirSync(path.dirname(path.join(temporary, relative)), {recursive: true});
  fs.writeFileSync(path.join(temporary, relative), original);
  // Released images ship patched files; each must then be exactly its patch result.
  for (const name of ['brainbrowser-react-mount.patch', 're-move-annex-content.patch']) {
    const patch = path.join(root, 'charts/loris/image', name);
    const args = ['--include=' + relative, patch];
    try {
      execFileSync('git', ['apply', '--check', ...args], {cwd: temporary, stdio: 'ignore'});
      execFileSync('git', ['apply', ...args], {cwd: temporary});
    } catch {
      execFileSync('git', ['apply', '--reverse', '--check', ...args], {cwd: temporary});
    }
  }
  patched = fs.readFileSync(path.join(temporary, relative), 'utf8');
} finally {
  fs.rmSync(temporary, {recursive: true});
}

function environment(mounted, hasRoot = true) {
  const state = {mounted, starts: 0, observer: null, disconnected: false};
  const ready = [];
  const stop = new Error('Stop after verified viewer startup');
  const reactRoot = {};
  const context = {
    window: {},
    document: {
      getElementById(id) {
        if (id === 'brainbrowser') return state.mounted ? {} : null;
        if (id === 'brainbrowserPage') return hasRoot ? reactRoot : null;
        return null;
      },
    },
    $(value) {
      if (typeof value === 'function') ready.push(value);
      return {button() {}};
    },
    MutationObserver: class {
      constructor(callback) { state.observer = callback; }
      observe(target, options) {
        assert.equal(target, reactRoot);
        assert.equal(options.childList, true);
        assert.equal(options.subtree, true);
      }
      disconnect() { state.disconnected = true; }
    },
    BrainBrowser: {VolumeViewer: {start(id) {
      assert.equal(id, 'brainbrowser');
      assert.ok(state.mounted, 'Viewer must not start before React mounts');
      state.starts++;
      throw stop;
    }}},
  };
  vm.runInNewContext(patched, context);
  assert.equal(ready.length, 1);
  return {state, run: ready[0], stop};
}

test('starts immediately when React committed before document-ready', () => {
  const {state, run, stop} = environment(true);
  assert.throws(run, error => error === stop);
  assert.equal(state.starts, 1);
  assert.equal(state.observer, null);
});

test('waits for the viewer container and disconnects before startup', () => {
  const {state, run, stop} = environment(false);
  run();
  assert.equal(state.starts, 0);
  state.observer();
  assert.equal(state.starts, 0);
  state.mounted = true;
  assert.throws(state.observer, error => error === stop);
  assert.equal(state.starts, 1);
  assert.equal(state.disconnected, true);
});

test('reports a missing React root rather than silently waiting', () => {
  const {state, run} = environment(false, false);
  assert.throws(run, /BrainBrowser React root is missing/);
  assert.equal(state.starts, 0);
  assert.equal(state.observer, null);
});

function viewerEnvironment(responses) {
  const state = {loading: [], volumes: null, timeouts: []};
  const chain = new Proxy(function() {}, {
    get(target, name) {
      if (name === 'val') return () => '256';
      if (name === 'text') return (value) => { state.loading.push(value); return chain; };
      return () => chain;
    },
    apply() { return chain; },
  });
  const viewer = {
    addEventListener() {},
    setDefaultPanelSize() {},
    loadDefaultColorMapFromURL(url, color, callback) { callback(); },
    render() {},
    loadVolumes(options) { state.volumes = options.volumes; },
  };
  const context = {
    window: {location: {search: '?minc_id=[7]&file_url=[]'}, addEventListener() {}},
    document: {getElementById: (id) => id === 'brainbrowser' ? {} : null},
    $(value) {
      if (typeof value === 'function') {
        value();
      }
      return chain;
    },
    setTimeout(callback, delay) { state.timeouts.push(delay); callback(); },
    fetch() {
      const [status, body, retry] = responses.shift();
      return Promise.resolve({
        status,
        headers: {get: (name) => name === 'Retry-After' ? retry : null},
        json: () => Promise.resolve(body),
      });
    },
    BrainBrowser: {
      VolumeViewer: {start(id, callback) { return callback(viewer); }},
      events: {addEventListener() {}},
      config: {get: () => [{url: '/map', cursor_color: '#fff'}]},
    },
  };
  context.window.document = context.document;
  vm.runInNewContext(patched, context);
  return state;
}

const settle = () => new Promise((resolve) => setImmediate(resolve));

test('retries while a site image is fetched, then loads it', async () => {
  const fetching = {state: 'fetching', error: 'Fetching the images from site S.'};
  const state = viewerEnvironment([
    [503, fetching, '3'],
    [503, fetching, null],
    [200, [{type: 'nifti1', URL: '/a.nii.gz', Filename: 'a.nii.gz'}]],
  ]);
  for (let i = 0; i < 10 && !state.volumes; i++) await settle();
  assert.deepEqual(state.timeouts, [3000, 5000]);
  assert.match(state.loading[0], /^Fetching the images from site S\. \(\d+ s\)$/);
  assert.equal(state.volumes.length, 1);
  assert.equal(state.volumes[0].nii_url, '/a.nii.gz');
});

test('reports an unavailable site image instead of retrying', async () => {
  const state = viewerEnvironment([
    [503, {state: 'unavailable', error: 'Site S is offline.'}, '60'],
  ]);
  for (let i = 0; i < 10 && state.loading.length === 0; i++) await settle();
  assert.deepEqual(state.loading, ['Site S is offline.']);
  assert.deepEqual(state.timeouts, []);
  assert.equal(state.volumes, null);
});

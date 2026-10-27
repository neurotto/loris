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
  const patch = path.join(root, 'charts/loris/image/brainbrowser-react-mount.patch');
  try {
    execFileSync('git', ['apply', '--check', patch], {cwd: temporary, stdio: 'ignore'});
    execFileSync('git', ['apply', patch], {cwd: temporary});
  } catch {
    // Released images ship the patched file; it must then be exactly the patch result.
    execFileSync('git', ['apply', '--reverse', '--check', patch], {cwd: temporary});
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

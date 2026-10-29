/* Run: node frontend/tests/startup.test.cjs. No packages/browser needed.
 * A browser-like nonconfigurable window.location catches declaration failures
 * that a syntax-only JavaScript check misses. This is not full browser QA.
 */
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const source = fs.readFileSync(path.join(__dirname, '..', 'app.js'), 'utf8');

async function scenario(mode) {
  const elements = new Map();
  const node = id => {
    if (!elements.has(id)) elements.set(id, {
      textContent: '', value: '', checked: false, addEventListener() {},
      append() {}, replaceChildren() {}, setAttribute() {}, classList: {toggle() {}}
    });
    return elements.get(id);
  };
  const requests = [];
  const sandbox = {
    document: {getElementById: node, querySelectorAll: () => []},
    AbortController,
    setTimeout: callback => setTimeout(callback, 15),
    clearTimeout,
    fetch: async (url, options) => {
      requests.push(url);
      if (mode === 'failure') throw new Error('offline');
      if (mode === 'timeout') return new Promise((resolve, reject) => {
        options.signal.addEventListener('abort', () => reject(new Error('aborted')));
      });
      return {ok: true, json: async () => ({openai_configured: true, database_configured: false, max_rows: 42})};
    }
  };
  Object.defineProperty(sandbox, 'location', {get() { return {}; }, set() {}, configurable: false});
  sandbox.window = sandbox;
  vm.createContext(sandbox);
  new vm.Script(source).runInContext(sandbox);
  await new Promise(resolve => setTimeout(resolve, 40));
  assert.deepEqual(requests, ['/api/v1/status']);
  return {message: node('configuration').textContent, maxRows: sandbox.configuredMaxRows};
}

module.exports = async function run() {
  const success = await scenario('success');
  assert.match(success.message, /OpenAI: configured/);
  assert.match(success.message, /Database: not configured/);
  assert.equal(success.maxRows, 42);
  assert.match((await scenario('failure')).message, /Could not read server configuration/);
  assert.match((await scenario('timeout')).message, /Could not read server configuration/);
  return '3 startup/configuration regression scenarios passed.';
};
if (require.main === module) module.exports().then(console.log).catch(error => { console.error(error); process.exitCode = 1; });

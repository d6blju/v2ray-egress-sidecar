// Run with: node tests/test_auth_client.js
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');

function client(page, replies) {
  const requests = [], redirects = [], elements = [];
  const account = {replaceChildren() {}, append(...values) { elements.push(...values); }};
  const context = {
    window: {}, Headers,
    location: {pathname: '/sidecar-stats/' + page, replace: path => redirects.push(path)},
    document: {getElementById: () => account, createElement: () => ({})},
    fetch: async (url, options) => {
      requests.push({url, options});
      const reply = replies.shift();
      assert.ok(reply, 'unexpected request or automatic retry');
      return {status: reply.status || 200, ok: !reply.status || reply.status < 400,
              json: async () => reply.body};
    }
  };
  vm.runInNewContext(fs.readFileSync(require.resolve('../web/auth.js'), 'utf8'), context);
  return {auth: context.window.SidecarAuth, requests, redirects, elements};
}

(async () => {
  const logged = {ok: true, authenticated: true, username: 'test', csrf_token: 'test-csrf'};
  const instance = client('control.html', [{body: logged}, {body: {ok: true}}, {status: 401, body: {ok: false}}]);
  await instance.auth.request('api/clear', {method: 'POST', headers: {'Content-Type': 'application/json', 'X-Sidecar-Admin': '1'}, body: '{}'});
  const mutation = instance.requests[1];
  assert.equal(mutation.url, '/sidecar-stats/api/clear');
  assert.equal(mutation.options.headers.get('X-CSRF-Token'), 'test-csrf');
  assert.equal(mutation.options.headers.has('X-Sidecar-Admin'), false);
  assert.equal(mutation.options.credentials, 'same-origin');
  await instance.auth.request('api/apply', {method: 'POST', body: '{}'});
  assert.equal(instance.requests.length, 3, 'a failed mutation must never be replayed');
  assert.equal(instance.redirects[0], '/sidecar-stats/login.html');

  const login = client('login.html', [{body: {ok: true, authenticated: false, csrf_token: 'prelogin-csrf'}}, {body: logged}]);
  await login.auth.login('test', 'test-only-password');
  assert.equal(login.requests[1].options.headers['X-CSRF-Token'], 'prelogin-csrf');
  assert.equal(login.requests[1].options.method, 'POST');
  assert.equal(login.redirects[0], '/sidecar-stats/control.html');

  const logout = client('control.html', [{body: logged}, {body: {ok: true}}]);
  await logout.auth.ready();
  await logout.elements.find(element => element.type === 'button').onclick();
  assert.equal(logout.requests[1].url, '/sidecar-stats/api/logout');
  assert.equal(logout.requests[1].options.headers['X-CSRF-Token'], 'test-csrf');
  assert.equal(logout.redirects[0], '/sidecar-stats/login.html');
  console.log('PASS: client login, CSRF injection, logout and no mutation replay');
})().catch(error => { console.error(error); process.exitCode = 1; });

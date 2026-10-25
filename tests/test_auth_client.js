// Run with: node tests/test_auth_client.js
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');

function client(page, replies) {
  const requests = [], redirects = [];
  const context = {
    window: {}, Headers,
    location: {pathname: '/sidecar-stats/' + page, replace: path => redirects.push(path)},
    // Session setup must not render account labels or logout controls.
    document: new Proxy({}, {get(_target, property) {
      assert.fail(`auth client must not access document.${String(property)}`);
    }}),
    fetch: async (url, options) => {
      requests.push({url, options});
      const reply = replies.shift();
      assert.ok(reply, 'unexpected request or automatic retry');
      return {status: reply.status || 200, ok: !reply.status || reply.status < 400,
              json: async () => reply.body};
    }
  };
  vm.runInNewContext(fs.readFileSync(require.resolve('../web/auth.js'), 'utf8'), context);
  return {auth: context.window.SidecarAuth, requests, redirects};
}

(async () => {
  const logged = {ok: true, authenticated: true, username: 'test', csrf_token: 'test-csrf'};
  const instance = client('control.html', [{body: logged}, {body: {ok: true}}, {status: 401, body: {ok: false}}]);
  assert.deepEqual(Object.keys(instance.auth).sort(), ['login', 'ready', 'request']);
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
  const session = await logout.auth.ready();
  assert.equal(session.username, 'test');
  assert.equal(logout.requests.length, 1, 'ready only bootstraps the session');
  await logout.auth.request('api/logout', {method: 'POST', body: '{}'});
  assert.equal(logout.requests[1].url, '/sidecar-stats/api/logout');
  assert.equal(logout.requests[1].options.headers.get('X-CSRF-Token'), 'test-csrf');
  assert.equal(logout.redirects.length, 0, 'a successful request does not render or navigate a logout widget');
  console.log('PASS: headless session setup, login, CSRF injection, API logout and no mutation replay');
})().catch(error => { console.error(error); process.exitCode = 1; });

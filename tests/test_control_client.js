// Run with: node tests/test_control_client.js (no browser or package dependencies).
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');

const clone = value => JSON.parse(JSON.stringify(value));
const hongKong = {name: 'Hong Kong', code: 'HK'};
const japan = {name: 'Japan', code: 'JP'};
const route = (profile, domains) => ({profile, domains, country: 'Hong Kong', protocol: 'udp',
  include_ip_sb: false, proxy: '127.0.0.1:18892'});

// Model the select behavior that matters here: assigning options selects the
// first option, and assigning a value missing from the filtered list clears it.
class Element {
  constructor(id) {
    this.id = id; this._value = ''; this._html = ''; this.children = [];
    this.disabled = false; this.hidden = false; this.checked = false;
    this.textContent = ''; this.style = {}; this.dataset = {};
  }
  get value() { return this._value; }
  set value(value) {
    value = String(value);
    this._value = this.options && !this.options.includes(value) ? '' : value;
  }
  get innerHTML() { return this._html; }
  set innerHTML(html) {
    this._html = html;
    if (this.id === 'profile' || this.id === 'country') {
      this.options = [...html.matchAll(/<option value="([^"]*)"/g)].map(match => match[1]);
      this._value = this.options[0] || '';
    }
    this.children = [...html.matchAll(/<button\b([^>]*)>([\s\S]*?)<\/button>/g)].map(match => {
      const child = new Element('');
      child.className = (match[1].match(/class="([^"]*)"/) || [])[1] || '';
      child.dataset.profile = (match[1].match(/data-profile="([^"]*)"/) || [])[1];
      child.dataset.enabled = (match[1].match(/data-enabled="([^"]*)"/) || [])[1];
      child.textContent = match[2].replace(/<[^>]*>/g, '');
      return child;
    });
  }
  querySelectorAll(selector) {
    return this.children.filter(child => selector === 'button' ||
      child.className.split(' ').includes(selector.slice(1)));
  }
  scrollIntoView() {}
  focus() {}
}

async function client(routes) {
  const elements = new Map(), mutations = [];
  const element = id => {
    if (!elements.has(id)) elements.set(id, new Element(id));
    return elements.get(id);
  };
  let current = {active: routes.some(saved => saved.enabled !== false), routes: clone(routes), ...(routes.at(-1) || {})};
  const fixture = () => ({ok: true, profiles: [
    {id: 'binance', name: 'Binance', domains: ['binance.com']},
    {id: 'polymarket', name: 'Polymarket', domains: ['polymarket.com']},
  ], country_options: [hongKong, japan], state: clone(current), exits: []});
  const response = body => ({ok: true, status: 200, text: async () => JSON.stringify(body)});
  const context = {
    location: {pathname: '/sidecar-stats/control.html'},
    document: {
      getElementById: element,
      querySelectorAll: () => [...element('current').children, element('apply'), element('clear'), element('cancel-edit')],
    },
    confirm: () => true,
    setTimeout: () => 1, clearTimeout: () => {},
    fetch: async () => response({windows: {'1h': {domains: []}}}),
    SidecarAuth: {request: async (path, options = {}) => {
      if (path === 'api/state') return response(fixture());
      const payload = JSON.parse(options.body || '{}');
      mutations.push({path, payload});
      if (path === 'api/apply') {
        const old = current.routes.find(saved => saved.profile === payload.profile);
        const selected = {...payload, enabled: old?.enabled !== false, proxy: '127.0.0.1:18892'};
        const routes = [...current.routes.filter(saved => saved.profile !== selected.profile), selected];
        current = {...selected, active: routes.some(saved => saved.enabled !== false), routes};
        return response({ok: true, state: clone(current), exit_check: {ok: true}, ip_check: {ok: true}});
      }
      if (path === 'api/routes/toggle') {
        const routes = current.routes.map(saved => saved.profile === payload.profile ? {...saved, enabled: payload.enabled} : saved);
        current = {...current, routes, active: routes.some(saved => saved.enabled !== false)};
        return response({ok: true, state: clone(current)});
      }
      if (path === 'api/routes/preview') {
        return response({ok: true, preview: {domain: payload.domain.trim(), is_ip: false, matched: true,
          rule: {type: 'domain_suffix', value: '.binance.com', index: 0, source: 'ui'},
          outbound: {tag: 'nord-ui', kind: 'http_connect', proxy: '127.0.0.1:18892'},
          outcome: 'proxy', services: ['binance'], country: 'Hong Kong'}});
      }
      assert.equal(path, 'api/routes/delete');
      const remaining = current.routes.filter(saved => saved.profile !== payload.profile);
      current = remaining.length ? {...remaining.at(-1), active: true, routes: remaining} : {
        active: false, profile: null, country: null, domains: [], include_ip_sb: false, routes: [],
      };
      return response({ok: true, state: clone(current)});
    }},
  };
  const html = fs.readFileSync(require.resolve('../web/control.html'), 'utf8');
  const script = [...html.matchAll(/<script>([\s\S]*?)<\/script>/g)][0][1];
  vm.runInNewContext(script, context);
  await new Promise(resolve => setImmediate(resolve));
  return {element, mutations, context};
}

function button(instance, kind, profile) {
  const found = instance.element('current').querySelectorAll('.' + kind)
    .find(item => item.dataset.profile === profile);
  assert.ok(found, `${kind} button for ${profile} must exist`);
  return found;
}

(async () => {
  const saved = [route('binance', ['binance.com', 'binance.us']), route('polymarket', ['polymarket.com'])];
  const cancelled = await client(saved);
  button(cancelled, 'edit-route', 'binance').onclick();
  assert.equal(cancelled.element('profile').disabled, true);
  assert.equal(cancelled.element('domains').value, 'binance.com\nbinance.us');
  cancelled.element('country-search').value = 'Japan';
  cancelled.element('country-search').oninput();
  assert.equal(cancelled.element('country').value, 'Japan');
  cancelled.element('cancel-edit').onclick();
  assert.equal(cancelled.element('country-search').value, '', 'cancel clears the edit country filter');
  assert.equal(cancelled.element('country').value, 'Hong Kong', 'cancel restores the saved country, not the filtered option');
  assert.equal(cancelled.element('profile').value, 'polymarket');
  assert.equal(cancelled.element('domains').value, 'polymarket.com');
  assert.equal(cancelled.element('profile').disabled, false);
  assert.equal(cancelled.mutations.length, 0, 'cancelling never saves a route');

  const edited = await client(saved);
  button(edited, 'edit-route', 'binance').onclick();
  edited.element('country').value = 'Japan';
  edited.element('domains').value = 'binance.com\nnew.example';
  await edited.element('apply').onclick();
  assert.deepEqual(edited.mutations, [{path: 'api/apply', payload: {
    profile: 'binance', country: 'Japan', domains: ['binance.com', 'new.example'],
    include_ip_sb: false, protocol: 'udp',
  }}], 'save targets the locked edited profile and complete domain list');
  assert.equal(edited.element('country-search').value, '');
  assert.equal(edited.element('country').value, 'Japan');
  assert.equal(edited.element('profile').disabled, false);

  const deleted = await client([saved[0]]);
  button(deleted, 'edit-route', 'binance').onclick();
  await button(deleted, 'delete-route', 'binance').onclick();
  assert.equal(deleted.element('profile').value, 'custom');
  assert.equal(deleted.element('domains').value, '', 'last-service deletion clears stale editable domains');
  assert.equal(deleted.element('profile').disabled, false);
  assert.equal(deleted.element('current').querySelectorAll('.edit-route').length, 0);

  const toggled = await client(saved);
  const pause = button(toggled, 'toggle-route', 'binance');
  assert.equal(pause.dataset.enabled, 'true', 'legacy routes default to enabled');
  assert.match(pause.textContent, /暂停/);
  await pause.onclick();
  assert.deepEqual(toggled.mutations[0], {path: 'api/routes/toggle', payload: {profile: 'binance', enabled: false}});
  const resume = button(toggled, 'toggle-route', 'binance');
  assert.equal(resume.dataset.enabled, 'false');
  assert.match(resume.textContent, /启用/);
  assert.equal(button(toggled, 'toggle-route', 'polymarket').dataset.enabled, 'true');
  await resume.onclick();
  assert.deepEqual(toggled.mutations[1], {path: 'api/routes/toggle', payload: {profile: 'binance', enabled: true}});
  assert.equal(button(toggled, 'toggle-route', 'binance').dataset.enabled, 'true');

  const paused = await client([{...saved[0], enabled: false}]);
  assert.equal(button(paused, 'toggle-route', 'binance').dataset.enabled, 'false', 'all-paused services remain visible');
  button(paused, 'edit-route', 'binance').onclick();
  paused.element('domains').value = 'edited.binance.com';
  await paused.element('apply').onclick();
  assert.equal(button(paused, 'toggle-route', 'binance').dataset.enabled, 'false', 'editing a paused route must not resume it');

  const previewed = await client(saved);
  previewed.element('preview-domain').value = 'api.binance.com';
  if (previewed.element('preview-domain').oninput) previewed.element('preview-domain').oninput();
  assert.equal(previewed.mutations.length, 0, 'typing a domain does not submit or modify routes');
  await previewed.element('preview-route').onclick();
  assert.deepEqual(previewed.mutations, [{path: 'api/routes/preview', payload: {domain: 'api.binance.com'}}]);
  const previewOutput = previewed.element('preview-result').innerHTML + previewed.element('preview-result').textContent;
  assert.match(previewOutput, /api\.binance\.com/);
  assert.match(previewOutput, /nord-ui/);
  assert.equal(button(previewed, 'toggle-route', 'binance').dataset.enabled, 'true');
  console.log('PASS: edit/cancel/delete, isolated pause/resume, paused editing, and read-only route preview');
})().catch(error => {console.error(error); process.exitCode = 1;});

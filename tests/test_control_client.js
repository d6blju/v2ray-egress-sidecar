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
    this.children = [...html.matchAll(/<button\b([^>]*)>/g)].map(match => {
      const child = new Element('');
      child.className = (match[1].match(/class="([^"]*)"/) || [])[1] || '';
      child.dataset.profile = (match[1].match(/data-profile="([^"]*)"/) || [])[1];
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
  let current = {active: routes.length > 0, routes: clone(routes), ...(routes.at(-1) || {})};
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
        const selected = {...payload, proxy: '127.0.0.1:18892'};
        current = {active: true, ...selected,
          routes: [...current.routes.filter(saved => saved.profile !== selected.profile), selected]};
        return response({ok: true, state: clone(current), exit_check: {ok: true}, ip_check: {ok: true}});
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
  console.log('PASS: edit prefill, cancel country-filter restoration, selected-service save, last deletion clears form');
})().catch(error => {console.error(error); process.exitCode = 1;});

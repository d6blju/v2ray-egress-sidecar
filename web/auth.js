/* Session and CSRF handling shared by the dashboard, login and routing pages. */
(() => {
  const base = location.pathname.replace(/[^/]*$/, '');
  let session = null, pending = null;
  const endpoint = path => base + path;
  async function raw(path, options = {}) {
    const response = await fetch(endpoint(path), {credentials: 'same-origin', cache: 'no-store', ...options});
    const data = await response.json();
    if (!response.ok || !data.ok) throw new Error(data.error || `HTTP ${response.status}`);
    return data;
  }
  function widget() {
    let element = document.getElementById('sidecar-account');
    if (!element) {
      element = document.createElement('div');
      element.id = 'sidecar-account';
      element.style.cssText = 'display:flex;gap:10px;align-items:center;flex-wrap:wrap;margin-top:10px;font-size:13px';
      (document.querySelector('header .wrap') || document.body).appendChild(element);
    }
    element.replaceChildren();
    if (session?.authenticated) {
      const name = document.createElement('span');
      name.textContent = `已登录：${session.username}`;
      const logout = document.createElement('button');
      logout.textContent = '退出登录';
      logout.type = 'button';
      logout.onclick = async () => {
        logout.disabled = true;
        try {
          await raw('api/logout', {method: 'POST', headers: {'Content-Type': 'application/json', 'X-CSRF-Token': session.csrf_token}, body: '{}'});
          session = null;
          location.replace(endpoint('login.html'));
        } catch (error) { logout.disabled = false; name.textContent = String(error); }
      };
      element.append(name, logout);
    }
  }
  async function ready() {
    if (session?.authenticated) return session;
    if (!pending) pending = raw('api/session', {headers: {'X-Sidecar-Session': '1'}}).then(data => {
      session = data;
      widget();
      if (!data.authenticated && !location.pathname.endsWith('/login.html')) {
        location.replace(endpoint('login.html'));
        throw new Error('请登录');
      }
      return data;
    }).finally(() => { pending = null; });
    return pending;
  }
  async function request(path, options = {}) {
    const current = await ready();
    if (!current.authenticated) throw new Error('请登录');
    const headers = new Headers(options.headers || {});
    headers.delete('X-Sidecar-Admin');
    if (options.method && !['GET', 'HEAD'].includes(options.method.toUpperCase())) {
      headers.set('X-CSRF-Token', current.csrf_token);
    }
    const response = await fetch(endpoint(path), {credentials: 'same-origin', cache: 'no-store', ...options, headers});
    if (response.status === 401) {
      session = null;
      location.replace(endpoint('login.html'));
    }
    // Mutations are never replayed automatically after a network/auth failure.
    return response;
  }
  async function login(username, password) {
    const prelogin = await ready();
    session = await raw('api/login', {method: 'POST', headers: {'Content-Type': 'application/json', 'X-CSRF-Token': prelogin.csrf_token}, body: JSON.stringify({username, password})});
    location.replace(endpoint('control.html'));
  }
  window.SidecarAuth = {ready, request, login};
})();

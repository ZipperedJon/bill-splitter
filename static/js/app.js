// Shell, session and hash router.

import { api, ApiError, setUnauthorizedHandler } from './api.js';
import { $, add, clear, h, initials, mount, toast } from './util.js';
import { renderSignIn, renderSetup, renderForcedPasswordChange } from './views/auth.js';
import { renderDashboard } from './views/dashboard.js';
import { renderGroupList, renderGroup } from './views/groups.js';
import { renderBillEditor } from './views/bill.js';
import { renderAdmin } from './views/admin.js';
import { renderAccount } from './views/account.js';

export const state = {
  user: null,
  defaults: { currency: 'USD', tax_percent: '0', tip_percent: '18', tip_base: 'pre_tax' },
  version: '',
  pendingApprovals: 0,
  updateAvailable: false,
  bootstrap: null,
};

// --- theme -------------------------------------------------------------------

const THEMES = ['auto', 'light', 'dark'];
const THEME_ICON = { auto: '◐', light: '☀', dark: '☾' };

function applyTheme(theme) {
  document.documentElement.dataset.theme = theme;
  const button = $('#theme-toggle');
  if (button) {
    button.textContent = THEME_ICON[theme];
    button.title = `Theme: ${theme} (click to change)`;
  }
}

function initTheme() {
  let theme = 'auto';
  try {
    theme = localStorage.getItem('billsplit.theme') || 'auto';
  } catch { /* private browsing */ }
  if (!THEMES.includes(theme)) theme = 'auto';
  applyTheme(theme);

  $('#theme-toggle').addEventListener('click', () => {
    const next = THEMES[(THEMES.indexOf(document.documentElement.dataset.theme) + 1) % THEMES.length];
    applyTheme(next);
    try {
      localStorage.setItem('billsplit.theme', next);
    } catch { /* ignore */ }
  });
}

// --- chrome ------------------------------------------------------------------

function renderChrome() {
  const topbar = $('#topbar');
  const footer = $('#footer');

  if (!state.user) {
    topbar.hidden = true;
    footer.hidden = true;
    return;
  }
  topbar.hidden = false;
  footer.hidden = false;

  const path = currentPath();
  const nav = clear($('#nav'));
  const links = [
    ['#/', 'Home', path === '/' || path === ''],
    ['#/groups', 'Groups', path === '/groups' || path.startsWith('/g/')],
  ];
  if (state.user.is_admin) links.push(['#/admin', 'Admin', path.startsWith('/admin')]);

  for (const [href, label, active] of links) {
    const link = h('a', { href, 'aria-current': active ? 'page' : null }, label);
    if (label === 'Admin' && state.pendingApprovals > 0) {
      add(link, h('span.badge.count', {}, String(state.pendingApprovals)));
    }
    if (label === 'Admin' && state.updateAvailable && !state.pendingApprovals) {
      add(link, h('span.badge.accent', {}, 'update'));
    }
    add(nav, link);
  }

  const name = state.user.display_name || state.user.username;
  const menu = clear($('#user-menu'));
  const button = h('button.menu-btn', { type: 'button', 'aria-haspopup': 'true', 'aria-expanded': 'false' },
    h('span.avatar', {}, initials(name)),
    h('span.hide-sm', {}, name),
  );
  add(menu, button);

  let panel = null;
  const closePanel = () => {
    if (panel) { panel.remove(); panel = null; }
    button.setAttribute('aria-expanded', 'false');
    document.removeEventListener('click', onOutside, true);
  };
  const onOutside = (event) => { if (!menu.contains(event.target)) closePanel(); };

  button.addEventListener('click', () => {
    if (panel) return closePanel();
    panel = h('div.menu-panel', {},
      h('div.menu-head', {},
        h('strong', {}, name),
        h('small', {}, `@${state.user.username}${state.user.is_admin ? ' · admin' : ''}`)),
      h('a', { href: '#/account', onclick: closePanel }, 'Account & password'),
      state.user.is_admin && h('a', { href: '#/admin', onclick: closePanel }, 'Admin panel'),
      h('button', {
        type: 'button',
        onclick: async () => {
          closePanel();
          try { await api.logout(); } catch { /* leaving anyway */ }
          state.user = null;
          location.hash = '#/';
          await refreshSession();
          route();
        },
      }, 'Sign out'),
    );
    add(menu, panel);
    button.setAttribute('aria-expanded', 'true');
    setTimeout(() => document.addEventListener('click', onOutside, true), 0);
  });

  mount(footer, 
    h('span', {}, `Bill Splitter v${state.version || '?'}`),
    ' · ',
    h('a', { href: '#/account' }, 'account'),
    state.user.is_admin ? [' · ', h('a', { href: '#/admin?tab=updates' }, 'updates')] : null,
  );
}

// --- session -----------------------------------------------------------------

export async function refreshSession() {
  try {
    const me = await api.me();
    state.user = me.user;
    if (me.user) {
      state.defaults = me.defaults || state.defaults;
      state.version = me.version || '';
      state.pendingApprovals = me.pending_approvals || 0;
      state.updateAvailable = Boolean(me.update_available);
    }
  } catch {
    state.user = null;
  }
  if (!state.user) {
    try {
      state.bootstrap = await api.bootstrap();
      state.version = state.bootstrap.version;
    } catch {
      state.bootstrap = null;
    }
  }
  renderChrome();
  return state.user;
}

// --- router ------------------------------------------------------------------

function currentPath() {
  const raw = (location.hash || '#/').slice(1);
  return raw.split('?')[0] || '/';
}

function currentQuery() {
  const raw = (location.hash || '').split('?')[1] || '';
  return new URLSearchParams(raw);
}

export function go(hash) {
  if (location.hash === hash) route();
  else location.hash = hash;
}

const ROUTES = [
  [/^\/?$/, () => renderDashboard()],
  [/^\/groups$/, () => renderGroupList()],
  [/^\/g\/(\d+)\/new$/, (m) => renderBillEditor({ groupId: Number(m[1]) })],
  [/^\/g\/(\d+)$/, (m, query) => renderGroup(Number(m[1]), query.get('tab') || 'bills')],
  [/^\/b\/(\d+)$/, (m) => renderBillEditor({ billId: Number(m[1]) })],
  [/^\/admin$/, (m, query) => renderAdmin(query.get('tab') || 'users')],
  [/^\/account$/, () => renderAccount()],
];

let routing = false;

export async function route() {
  if (routing) return;
  routing = true;

  const view = $('#view');
  const path = currentPath();
  const query = currentQuery();
  renderChrome();

  try {
    if (!state.user) {
      view.className = 'view narrow';
      if (state.bootstrap && state.bootstrap.needs_setup) renderSetup(view);
      else renderSignIn(view);
      return;
    }

    // An admin-reset account gets one screen and one screen only.
    if (state.user.must_change_password) {
      view.className = 'view narrow';
      renderForcedPasswordChange(view);
      return;
    }

    for (const [pattern, handler] of ROUTES) {
      const match = path.match(pattern);
      if (match) {
        view.className = 'view';
        clear(view);
        await handler(match, query);
        return;
      }
    }

    view.className = 'view narrow';
    mount(view, 
      h('div.card.card-pad.center', {},
        h('h2', {}, 'Page not found'),
        h('p.dim.small', {}, 'That link does not go anywhere.'),
        h('a.btn.btn-primary', { href: '#/' }, 'Back to home')),
    );
  } catch (error) {
    if (error instanceof ApiError && error.status === 401) return;
    console.error(error);
    mount(view, 
      h('div.card.card-pad', {},
        h('h2', {}, 'Something went wrong'),
        h('p.dim', {}, error.message || String(error)),
        h('button.btn', { type: 'button', onclick: () => route() }, 'Try again')),
    );
  } finally {
    routing = false;
    window.scrollTo(0, 0);
  }
}

// --- boot --------------------------------------------------------------------

setUnauthorizedHandler(() => {
  if (state.user) {
    state.user = null;
    toast('Your session expired. Please sign in again.');
    refreshSession().then(route);
  }
});

window.addEventListener('hashchange', route);

// Keep the badge counts and the update flag honest without a page reload.
setInterval(() => {
  if (state.user && document.visibilityState === 'visible') refreshSession();
}, 120000);

async function boot() {
  initTheme();
  await refreshSession();
  await route();
  $('#boot').remove();
}

boot();

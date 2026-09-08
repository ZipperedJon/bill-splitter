// Small DOM + formatting helpers. No framework: `h()` builds real elements and
// every bit of user text goes in as textContent, so there is no HTML injection
// path anywhere in the UI.

/** h('div.card', {onclick}, 'text', childEl) -> HTMLElement */
export function h(spec, props, ...children) {
  const [tag, ...classes] = String(spec).split('.');
  const el = document.createElement(tag || 'div');
  if (classes.length) el.className = classes.join(' ');

  if (props && (typeof props !== 'object' || props instanceof Node || Array.isArray(props))) {
    children.unshift(props);
    props = null;
  }
  for (const [key, value] of Object.entries(props || {})) {
    if (value == null || value === false) continue;
    if (key === 'class') el.className = el.className ? `${el.className} ${value}` : value;
    else if (key === 'style' && typeof value === 'object') Object.assign(el.style, value);
    else if (key === 'dataset') Object.assign(el.dataset, value);
    else if (key.startsWith('on') && typeof value === 'function') {
      el.addEventListener(key.slice(2).toLowerCase(), value);
    } else if (key === 'html') el.innerHTML = value;   // only ever used for inline <svg> literals
    else if (key in el && key !== 'list' && key !== 'form') el[key] = value;
    else el.setAttribute(key, value === true ? '' : value);
  }
  append(el, children);
  return el;
}

export function append(el, children) {
  for (const child of children.flat(4)) {
    if (child == null || child === false || child === '') continue;
    el.append(child instanceof Node ? child : document.createTextNode(String(child)));
  }
  return el;
}

export function clear(el) {
  while (el.firstChild) el.removeChild(el.firstChild);
  return el;
}

// Always use these instead of the DOM's own el.append(...): the native one
// stringifies `false` and `null`, so a `cond && h(...)` child that is falsy
// renders the literal text "false" onto the page.
/** Replace an element's contents. */
export function mount(el, ...children) {
  clear(el);
  return append(el, children);
}

/** Add to an element's contents. */
export function add(el, ...children) {
  return append(el, children);
}

export const $ = (sel, root = document) => root.querySelector(sel);
export const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));

// --- money ------------------------------------------------------------------

const fmtCache = new Map();

function formatter(currency) {
  const code = (currency || 'USD').toUpperCase();
  if (!fmtCache.has(code)) {
    let f;
    try {
      f = new Intl.NumberFormat(undefined, { style: 'currency', currency: code });
    } catch {
      // Unknown code (someone typed 'BEER'): fall back to plain numbers.
      f = new Intl.NumberFormat(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 });
    }
    fmtCache.set(code, f);
  }
  return fmtCache.get(code);
}

/** 1234 -> "$12.34" */
export function money(cents, currency = 'USD') {
  return formatter(currency).format((Number(cents) || 0) / 100);
}

/** Absolute value, for "owes $12.34" phrasing where the sign is in the words. */
export function moneyAbs(cents, currency = 'USD') {
  return money(Math.abs(Number(cents) || 0), currency);
}

/** 1234 -> "12.34", for putting cents back into a text input. */
export function centsToInput(cents) {
  const n = Number(cents) || 0;
  return (n / 100).toFixed(2);
}

export function pctLabel(value) {
  const n = Number(value) || 0;
  return `${Number.isInteger(n) ? n : Number(n.toFixed(4))}%`;
}

// --- dates ------------------------------------------------------------------

export function today() {
  const d = new Date();
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')}`;
}

/** '2026-09-05' -> 'Sep 5, 2026' (parsed as local, not UTC, so no off-by-one day) */
export function niceDate(iso) {
  if (!iso) return '';
  const [y, m, d] = String(iso).split('-').map(Number);
  if (!y || !m || !d) return iso;
  return new Date(y, m - 1, d).toLocaleDateString(undefined, {
    month: 'short', day: 'numeric', year: 'numeric',
  });
}

export function niceTime(epochSeconds) {
  if (!epochSeconds) return '';
  return new Date(Number(epochSeconds) * 1000).toLocaleString(undefined, {
    month: 'short', day: 'numeric', hour: 'numeric', minute: '2-digit',
  });
}

export function relTime(epochSeconds) {
  if (!epochSeconds) return 'never';
  const seconds = Math.floor(Date.now() / 1000 - Number(epochSeconds));
  if (seconds < 60) return 'just now';
  const steps = [[60, 'min'], [24, 'hr'], [7, 'day'], [4.35, 'week'], [12, 'month']];
  let value = seconds / 60;
  let unit = 'min';
  for (let i = 0; i < steps.length; i++) {
    unit = steps[i][1];
    if (value < steps[i][0] || i === steps.length - 1) break;
    value /= steps[i + 1] ? steps[i + 1][0] : 1;
  }
  const n = Math.floor(value);
  return `${n} ${unit}${n === 1 ? '' : 's'} ago`;
}

// --- misc -------------------------------------------------------------------

export function initials(name) {
  const parts = String(name || '?').trim().split(/[\s._-]+/).filter(Boolean);
  if (!parts.length) return '?';
  return (parts.length === 1 ? parts[0].slice(0, 2) : parts[0][0] + parts[1][0]).toUpperCase();
}

const CATEGORY_ICONS = {
  restaurant: '🍽️', groceries: '🛒', lodging: '🏠', transport: '🚗', flights: '✈️',
  activities: '🎟️', drinks: '🍸', utilities: '💡', rent: '🔑', shopping: '🛍️',
  household: '🧻', other: '🧾',
};

export function categoryIcon(icon) {
  return CATEGORY_ICONS[icon] || CATEGORY_ICONS.other;
}

export function debounce(fn, ms = 250) {
  let timer;
  const wrapped = (...args) => {
    clearTimeout(timer);
    timer = setTimeout(() => fn(...args), ms);
  };
  wrapped.cancel = () => clearTimeout(timer);
  return wrapped;
}

export function toast(message, kind = '') {
  const box = document.getElementById('toasts');
  if (!box) return;
  const el = h(`div.toast${kind ? '.' + kind : ''}`, {}, message);
  box.append(el);
  setTimeout(() => {
    el.style.transition = 'opacity .25s';
    el.style.opacity = '0';
    setTimeout(() => el.remove(), 250);
  }, kind === 'err' ? 6000 : 3200);
}

/** Promise-based modal. Resolves with the dialog's result, or null if dismissed. */
export function modal({ title, body, actions, onMount }) {
  return new Promise((resolve) => {
    const root = document.getElementById('modal-root');
    let done = false;

    const close = (value) => {
      if (done) return;
      done = true;
      document.removeEventListener('keydown', onKey);
      overlay.remove();
      resolve(value ?? null);
    };
    const onKey = (event) => {
      if (event.key === 'Escape') close(null);
    };

    const panel = h('div.modal', { role: 'dialog', 'aria-modal': 'true', 'aria-label': title },
      h('div.card-head', {}, h('h3', {}, title),
        h('button.icon-btn.right', { type: 'button', onclick: () => close(null), 'aria-label': 'Close' }, '×')),
      h('div.card-body', {}, ...(Array.isArray(body) ? body : [body])),
    );
    if (actions) {
      panel.append(h('div.card-foot', {}, h('div.row', {},
        h('div.grow'), ...actions(close))));
    }

    const overlay = h('div.overlay', {
      onclick: (event) => { if (event.target === overlay) close(null); },
    }, panel);

    root.append(overlay);
    document.addEventListener('keydown', onKey);
    if (onMount) onMount(panel, close);
    const focusable = panel.querySelector('input, select, textarea, button.btn-primary, button');
    if (focusable) focusable.focus();
  });
}

export function confirmDialog({ title, message, confirmLabel = 'Confirm', danger = false, extra }) {
  return modal({
    title,
    body: [h('p', { style: { margin: 0 } }, message), extra].filter(Boolean),
    actions: (close) => [
      h('button.btn', { type: 'button', onclick: () => close(false) }, 'Cancel'),
      h(`button.btn.${danger ? 'btn-danger' : 'btn-primary'}`,
        { type: 'button', onclick: () => close(true) }, confirmLabel),
    ],
  }).then((v) => v === true);
}

/** Wraps a click handler so the button shows a spinner and cannot double-fire. */
export function busy(button, fn) {
  return async (...args) => {
    if (button.disabled) return;
    const original = button.textContent;
    button.disabled = true;
    clear(button).append(h('span.spin-inline'));
    try {
      return await fn(...args);
    } finally {
      button.disabled = false;
      clear(button).append(document.createTextNode(original));
    }
  };
}

/**
 * Hand something to the OS share sheet, falling back to the clipboard.
 *
 * On a phone this is what puts Messages, WhatsApp and Mail in front of the
 * person instead of making them copy a URL out of a text field. Returns what
 * actually happened so the caller can say the right thing:
 *   'shared'    the sheet took it
 *   'cancelled' the sheet opened and they backed out - say nothing
 *   'copied'    no sheet available, so the link is on the clipboard
 *   'failed'    neither worked; the caller should tell them to select it
 *
 * navigator.share needs a secure context, so it is missing over plain http
 * (a LAN address) even on a phone that supports it - hence the fallback
 * mattering as much as the sheet.
 */
export async function shareOrCopy({ title, text, url }) {
  if (navigator.share) {
    try {
      await navigator.share({ title, text, url });
      return 'shared';
    } catch (error) {
      if (error && error.name === 'AbortError') return 'cancelled';
      // Anything else (no target chosen, permission, an odd in-app browser)
      // falls through to the clipboard rather than dead-ending.
    }
  }
  try {
    await navigator.clipboard.writeText(url);
    return 'copied';
  } catch {
    return 'failed';
  }
}

/** True when the OS share sheet is actually reachable from here. */
export function canShareNatively() {
  return Boolean(navigator.share);
}

/**
 * Put text on the clipboard. Returns whether it worked.
 *
 * Only ever call this from a click handler: browsers require a user gesture
 * (and a focused document) for a clipboard write, so doing it when a panel
 * merely renders would be both blocked and rude.
 */
export async function copyText(text) {
  try {
    await navigator.clipboard.writeText(text);
    return true;
  } catch {
    return false;
  }
}

export function shareIcon() {
  return h('span', {
    style: { display: 'inline-flex', width: '15px', height: '15px' },
    html: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" '
      + 'stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">'
      + '<path d="M12 16V3M12 3 7 8M12 3l5 5"/>'
      + '<path d="M4 14v5a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2v-5"/></svg>',
  });
}

export function emptyState(icon, title, note, action) {
  return h('div.empty', {}, h('div.big', {}, icon), h('h3', {}, title),
    note && h('p.small', {}, note), action);
}

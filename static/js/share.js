// The guest-facing share page: who are you, then what did you have.
//
// Three steps, one screen each, because this gets opened on a phone at a
// restaurant table by someone who has never seen the app before:
//   1. pick your name (or add it)
//   2. tick your items
//   3. see what you owe
//
// Deliberately has no dependency on app.js: there is no session here, and a
// 401 must never bounce a guest to a sign-in screen.

import {
  $, add, clear, confirmDialog, h, initials, modal, money, mount,
  niceDate, toast,
} from './util.js';

const TOKEN = decodeURIComponent(location.pathname.replace(/^\/s\//, '').replace(/\/$/, ''));
const STORE_KEY = `billsplit.share.${TOKEN}`;

const state = {
  data: null,
  me: null,          // { party, name }
  selected: new Set(),
  saving: false,
  dirty: false,
};

// --- identity memory --------------------------------------------------------

function remember(identity) {
  try {
    localStorage.setItem(STORE_KEY, JSON.stringify(identity));
  } catch { /* private browsing; we just re-ask */ }
}

function recall() {
  try {
    const raw = localStorage.getItem(STORE_KEY);
    return raw ? JSON.parse(raw) : null;
  } catch {
    return null;
  }
}

function forget() {
  try {
    localStorage.removeItem(STORE_KEY);
  } catch { /* ignore */ }
}

// --- api --------------------------------------------------------------------

async function call(path, options = {}) {
  const response = await fetch(path, {
    ...options,
    headers: options.body ? { 'Content-Type': 'application/json' } : {},
  });
  const text = await response.text();
  let payload = null;
  if (text) {
    try {
      payload = JSON.parse(text);
    } catch {
      payload = { detail: text.slice(0, 200) };
    }
  }
  if (!response.ok) {
    const detail = payload && payload.detail;
    const error = new Error(typeof detail === 'string' && detail ? detail : 'Something went wrong.');
    error.status = response.status;
    throw error;
  }
  return payload;
}

const api = {
  load: () => call(`/api/share/${encodeURIComponent(TOKEN)}`),
  join: (name) => call(`/api/share/${encodeURIComponent(TOKEN)}/join`, {
    method: 'POST', body: JSON.stringify({ name }),
  }),
  saveClaims: (party, itemIds) => call(`/api/share/${encodeURIComponent(TOKEN)}/claims`, {
    method: 'POST', body: JSON.stringify({ party, item_ids: itemIds }),
  }),
};

// --- chrome -----------------------------------------------------------------

function header(data) {
  const bill = data.bill;
  return h('div.center', { style: { margin: '22px 0 18px' } },
    h('div', { style: { fontSize: '1.9rem', marginBottom: '6px' } }, '🧾'),
    h('h1', {}, bill.title || 'Bill'),
    h('p.dim.small', { style: { marginTop: '6px' } },
      [data.group_name, niceDate(bill.bill_date), bill.category_name].filter(Boolean).join(' · ')),
    h('div.strong', { style: { marginTop: '8px', fontSize: '1.25rem' } },
      money(data.totals.total_cents, bill.currency),
      h('span.faint.small', { style: { fontWeight: '400' } }, ' total')),
    bill.notes
      ? h('p.small.dim', { style: { marginTop: '10px', textAlign: 'left' } }, bill.notes)
      : null,
  );
}

function receipt(data) {
  if (!data.items.length) return null;
  return h('div.card', {},
    h('div.card-head', {}, h('h3', {}, 'The receipt'),
      h('span.small.faint.right', {}, money(data.totals.subtotal_cents, data.bill.currency))),
    h('div.list', {}, ...data.items.map((item) => h('div.item', { style: { cursor: 'default' } },
      h('span.grow', {},
        h('div.title', {}, item.label),
        h('div.meta', {}, item.claimed_by.length
          ? `${item.claimed_by.length} ${item.claimed_by.length === 1 ? 'person' : 'people'}`
          : 'nobody yet')),
      h('span.money', {}, money(item.amount_cents, data.bill.currency))))),
    data.charges.length ? h('div.card-body.tight', {},
      ...data.charges.map((charge) => h('div.totals-line', {},
        h('span.dim.small', {}, charge.label),
        h('span.v.small', {}, money(charge.cents, data.bill.currency))))) : null,
  );
}

// --- step 1: who are you ----------------------------------------------------

function renderIdentity() {
  const data = state.data;
  const view = $('#view');
  const canJoin = data.share.allow_join && !data.share.closed;

  const nameInput = h('input', {
    type: 'text', placeholder: 'Your name', maxlength: 60, autocomplete: 'name',
  });

  const joinNow = async () => {
    const name = nameInput.value.trim();
    if (!name) return nameInput.focus();
    try {
      const result = await api.join(name);
      if (result.existing) {
        toast(`${result.name} was already on the bill — continuing as them.`);
      }
      await pickIdentity({ party: result.party, name: result.name });
    } catch (error) {
      toast(error.message, 'err');
    }
  };

  mount(view,
    header(data),

    data.share.closed
      ? h('div.notice.warn', { style: { marginBottom: '14px' } },
          'This bill is closed for changes. You can look, but not pick.')
      : null,

    h('div.card', {},
      h('div.card-head', {}, h('h2', {}, 'Which one are you?')),
      data.people.length
        ? h('div.list', {}, ...data.people.map((person) => h('button.item', {
            type: 'button',
            onclick: () => chooseExisting(person),
          },
            h('span.avatar', {}, initials(person.name)),
            h('span.grow', {},
              h('div.title', {}, person.name),
              h('div.meta', {}, person.claimed_items
                ? `already picked ${person.claimed_items} item${person.claimed_items === 1 ? '' : 's'}`
                : 'has not picked yet')),
            h('span.faint', {}, '›'))))
        : h('div.card-body.small.dim', {}, 'Nobody has been added to this bill yet.'),
    ),

    canJoin
      ? h('div.card', { style: { marginTop: '14px' } },
          h('div.card-head', {}, h('h3', {}, "I'm not on the list")),
          h('div.card-body.stack', {},
            h('div.field', {}, h('label', {}, 'Add yourself'), nameInput,
              h('span.hint', {}, 'Everyone on the bill will see this name.')),
            h('button.btn.btn-primary.btn-block', { type: 'button', onclick: joinNow },
              'Add me to this bill')))
      : h('p.small.faint.center', { style: { marginTop: '14px' } },
          data.share.closed
            ? null
            : 'Adding new people is turned off for this link. Ask whoever sent it to add you.'),

    receipt(data),
  );

  nameInput.addEventListener('keydown', (event) => {
    if (event.key === 'Enter') {
      event.preventDefault();
      joinNow();
    }
  });
}

async function chooseExisting(person) {
  // Anyone with the link can pick any name, so make taking over someone
  // else's selections a deliberate act rather than a stray tap.
  if (person.claimed_items > 0) {
    const ok = await confirmDialog({
      title: `Continue as ${person.name}?`,
      message: `${person.name} has already picked ${person.claimed_items} `
        + `item${person.claimed_items === 1 ? '' : 's'}. Only carry on if that is you — `
        + 'you will be editing their selections.',
      confirmLabel: `Yes, I'm ${person.name}`,
    });
    if (!ok) return;
  }
  await pickIdentity({ party: person.party, name: person.name });
}

async function pickIdentity(identity) {
  state.me = identity;
  remember(identity);
  await refresh();
  renderPicker();
}

// --- step 2: what did you have ---------------------------------------------

function renderPicker() {
  const data = state.data;
  const view = $('#view');
  const me = state.me;
  const currency = data.bill.currency;
  const readOnly = data.share.closed;

  const mine = data.people.find((p) => p.party === me.party);
  if (!mine) {
    // Removed from the bill while we were away.
    forget();
    state.me = null;
    toast('You are no longer on this bill. Pick again.');
    return renderIdentity();
  }

  state.selected = new Set(
    data.items.filter((item) => item.claimed_by.includes(me.party)).map((item) => item.id),
  );

  const totalBox = h('div.card.card-body');
  const drawTotal = () => {
    const chosen = data.items.filter((item) => state.selected.has(item.id));
    // Sharers on an item split it; show that honestly rather than the full price.
    const raw = chosen.reduce((sum, item) => {
      const others = item.claimed_by.filter((p) => p !== me.party).length;
      return sum + item.amount_cents / (others + 1);
    }, 0);
    mount(totalBox,
      h('div.spread', {},
        h('span', {}, h('div.label', {}, 'Your items'),
          h('div.faint.tiny', {}, `${chosen.length} of ${data.items.length}`)),
        h('span.strong', { style: { fontSize: '1.35rem' } }, money(Math.round(raw), currency))),
      h('p.tiny.faint', { style: { margin: '8px 0 0' } },
        'Before tax, tip and fees are shared out. Your final figure is on the next screen.'),
    );
  };

  const itemList = h('div.list');
  const drawItems = () => {
    mount(itemList, ...data.items.map((item) => {
      const on = state.selected.has(item.id);
      const others = item.claimed_by.filter((p) => p !== me.party);
      const otherNames = others
        .map((party) => (data.people.find((p) => p.party === party) || {}).name)
        .filter(Boolean);

      return h('button.item', {
        type: 'button',
        disabled: readOnly,
        style: on ? { background: 'var(--accent-soft)' } : {},
        onclick: () => {
          if (readOnly) return;
          on ? state.selected.delete(item.id) : state.selected.add(item.id);
          state.dirty = true;
          drawItems();
          drawTotal();
        },
      },
        h('span.cat-icon', {
          style: on
            ? { background: 'var(--accent)', color: 'var(--accent-text)' }
            : {},
        }, on ? '✓' : ''),
        h('span.grow', {},
          h('div.title', {}, item.label),
          h('div.meta', {}, otherNames.length
            ? `shared with ${otherNames.join(', ')}`
            : on ? 'just you' : 'tap to add')),
        h('span.money', {}, money(item.amount_cents, currency)),
      );
    }));
  };

  drawItems();
  drawTotal();

  const saveButton = h('button.btn.btn-primary.btn-block', { type: 'button' }, 'Save my picks');
  saveButton.addEventListener('click', async () => {
    if (state.saving) return;
    state.saving = true;
    saveButton.disabled = true;
    const label = saveButton.textContent;
    saveButton.textContent = 'Saving…';
    try {
      state.data = await api.saveClaims(me.party, [...state.selected]);
      state.dirty = false;
      renderDone();
    } catch (error) {
      toast(error.message, 'err');
      if (error.status === 400) {
        await refresh();
        renderPicker();
      }
    } finally {
      state.saving = false;
      saveButton.disabled = false;
      saveButton.textContent = label;
    }
  });

  mount(view,
    header(data),

    h('div.card', { style: { marginBottom: '14px' } },
      h('div.card-body', {}, h('div.row', {},
        h('span.avatar', {}, initials(me.name)),
        h('span.grow', {},
          h('div.strong', {}, me.name),
          h('div.faint.tiny', {}, 'that’s you')),
        h('button.btn.btn-sm', {
          type: 'button',
          onclick: async () => {
            if (state.dirty && !await confirmDialog({
              title: 'Discard your picks?',
              message: 'You have unsaved selections. Switching name loses them.',
              confirmLabel: 'Switch anyway', danger: true,
            })) return;
            forget();
            state.me = null;
            await refresh();
            renderIdentity();
          },
        }, 'Not me'))),
    ),

    readOnly
      ? h('div.notice.warn', { style: { marginBottom: '14px' } },
          'This bill is closed for changes.')
      : null,

    h('div.card', { style: { marginBottom: '14px' } },
      h('div.card-head', {}, h('h2', {}, 'What did you have?'),
        h('span.small.faint.right', {}, 'tap each one')),
      data.items.length
        ? itemList
        : h('div.card-body.small.dim', {},
            data.itemized
              ? 'No items have been entered yet. Check back once the receipt is typed in.'
              : 'This bill is split evenly, so there is nothing to tick — you are on it.'),
    ),

    data.items.length ? h('div', { style: { marginBottom: '14px' } }, totalBox) : null,

    readOnly ? null : saveButton,

    !data.items.length && !readOnly
      ? h('button.btn.btn-block', { type: 'button', onclick: renderDone }, 'See what I owe')
      : null,
  );
}

// --- step 3: what you owe ---------------------------------------------------

function renderDone() {
  const data = state.data;
  const view = $('#view');
  const me = state.me;
  const currency = data.bill.currency;
  const mine = data.people.find((p) => p.party === me.party) || { owed_cents: 0, name: me.name };

  mount(view,
    header(data),

    h('div.card', { style: { marginBottom: '14px' } },
      h('div.card-body.center', {},
        h('div.label', {}, `${mine.name}, you owe`),
        h('div', { style: { fontSize: '2.4rem', fontWeight: '680', margin: '6px 0' } },
          money(mine.owed_cents, currency)),
        h('p.small.dim', { style: { margin: 0 } },
          'Your share of the items you picked, plus your share of tax, tip and any fees.'),
        mine.paid_cents
          ? h('p.small.pos', { style: { margin: '8px 0 0' } },
              `You already paid ${money(mine.paid_cents, currency)}.`)
          : null),
      h('div.card-foot', {}, h('div.row', {},
        h('button.btn.btn-sm', {
          type: 'button',
          onclick: async () => { await refresh(); renderPicker(); },
        }, 'Change my picks'),
        h('div.grow'),
        h('button.btn.btn-sm', {
          type: 'button',
          onclick: async () => { await refresh(); renderDone(); toast('Refreshed.'); },
        }, 'Refresh'))),
    ),

    h('div.card', { style: { marginBottom: '14px' } },
      h('div.card-head', {}, h('h3', {}, 'Everyone'),
        h('span.small.faint.right', {}, money(data.totals.total_cents, currency))),
      h('div.list', {}, ...data.people.map((person) => h('div.item', {
        style: { cursor: 'default', ...(person.party === me.party ? { background: 'var(--accent-soft)' } : {}) },
      },
        h('span.avatar', {}, initials(person.name)),
        h('span.grow', {},
          h('div.title', {}, person.name,
            person.party === me.party
              ? h('span.badge.accent', { style: { marginLeft: '7px' } }, 'you')
              : null),
          h('div.meta', {}, person.claimed_items
            ? `${person.claimed_items} item${person.claimed_items === 1 ? '' : 's'}`
            : data.itemized ? 'nothing picked yet' : 'even share')),
        h('span.money.strong', {}, money(person.owed_cents, currency))))),
    ),

    data.warnings.length
      ? h('div.stack-sm', {}, ...data.warnings.map((w) => h('div.notice.warn', {}, w)))
      : null,

    receipt(data),

    h('p.tiny.faint.center', { style: { marginTop: '18px' } },
      'Anyone with this link can see and change picks. Nothing here needs an account.'),
  );
}

// --- plumbing ---------------------------------------------------------------

async function refresh() {
  state.data = await api.load();
  return state.data;
}

function fatal(message, detail) {
  mount($('#view'),
    h('div.center', { style: { margin: '40px 0 18px' } },
      h('div', { style: { fontSize: '2rem', marginBottom: '10px' } }, '🔗'),
      h('h1', {}, message),
      detail ? h('p.dim.small', { style: { marginTop: '8px' } }, detail) : null),
  );
}

async function boot() {
  if (!TOKEN) {
    fatal('No link', 'This page needs a share link.');
    return;
  }
  try {
    await refresh();
  } catch (error) {
    fatal(
      error.status === 410 ? 'This link has expired' : 'This link is not valid',
      error.message,
    );
    return;
  }

  const saved = recall();
  if (saved && state.data.people.some((p) => p.party === saved.party)) {
    // Keep the stored name in step with a rename by whoever owns the bill.
    const current = state.data.people.find((p) => p.party === saved.party);
    state.me = { party: saved.party, name: current.name };
    remember(state.me);
    renderPicker();
  } else {
    if (saved) forget();
    renderIdentity();
  }
}

boot().finally(() => {
  const splash = document.getElementById('boot');
  if (splash) splash.remove();
});

// Coming back to the tab after a while: pick up other people's picks.
document.addEventListener('visibilitychange', async () => {
  if (document.visibilityState !== 'visible' || !state.data || state.dirty || state.saving) return;
  try {
    await refresh();
    if (state.me) renderPicker();
  } catch { /* offline; leave what is on screen */ }
});

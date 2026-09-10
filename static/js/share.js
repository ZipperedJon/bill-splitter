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
  $, add, clear, confirmDialog, h, initials, modal, money, mount, portionsCost,
  niceDate, toast,
} from './util.js';

/** "Aaron" / "Aaron and Ben" / "Aaron, Ben and Cam". */
function listNames(names) {
  if (names.length <= 1) return names[0] || '';
  return `${names.slice(0, -1).join(', ')} and ${names[names.length - 1]}`;
}

const TOKEN = decodeURIComponent(location.pathname.replace(/^\/s\//, '').replace(/\/$/, ''));
const STORE_KEY = `billsplit.share.${TOKEN}`;

const state = {
  data: null,
  me: null,               // { party, name }
  taken: new Map(),       // item id -> how many portions I took
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
  saveClaims: (party, portions) => call(`/api/share/${encodeURIComponent(TOKEN)}/claims`, {
    method: 'POST', body: JSON.stringify({ party, portions }),
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
  const currency = data.bill.currency;
  return h('div.card', {},
    h('div.card-head', {}, h('h3', {}, 'The receipt'),
      h('span.small.faint.right', {}, money(data.totals.subtotal_cents, currency))),
    h('div.list', {}, ...data.items.map((item) => h('div.item', { style: { cursor: 'default' } },
      h('span.grow', {},
        h('div.title', {}, item.label,
          item.portions > 1
            ? h('span.faint.small', {}, ` · ${item.portions} portions`)
            : null),
        ...item.sub_items.map((sub) => h('div.meta', {},
          `↳ ${sub.label}`,
          sub.amount_cents ? ` ${money(sub.amount_cents, currency)}` : '')),
        h('div.meta', { class: item.claimed_by.length ? null : 'unclaimed' },
          item.claimed_by.length
            ? `${item.claimed_by.length} ${item.claimed_by.length === 1 ? 'person' : 'people'}`
            : 'nobody has picked this')),
      h('span.money', {}, money(item.line_total_cents, currency))))),
    data.charges.length ? h('div.card-body.tight', {},
      ...data.charges.map((charge) => h('div.totals-line', {},
        h('span.dim.small', {}, charge.label),
        h('span.v.small', {}, money(charge.cents, currency))))) : null,
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

  // party -> how many portions they took. A plain whole item is just 1.
  state.taken = new Map(
    data.items
      .map((item) => [item.id, Number((item.claims || {})[me.party] || 0)])
      .filter(([, units]) => units > 0),
  );

  const nameOf = (party) => (data.people.find((p) => p.party === party) || {}).name;
  const notPaying = data.people.filter((p) => p.exempt).map((p) => p.name);

  const totalBox = h('div.card.card-body');
  const drawTotal = () => {
    let raw = 0;
    let count = 0;
    const headcount = Math.max(1, data.people.length);
    // Whether anything nobody ticks lands on the table or is left unassigned.
    // Following the bill's own setting is what stops this estimate quoting a
    // different figure from the one the server puts on the next screen.
    const shareOutTheRest = data.bill.unclaimed_mode === 'even';

    for (const item of data.items) {
      const mine = state.taken.get(item.id) || 0;
      const others = Object.entries(item.claims || {})
        .filter(([party]) => party !== me.party)
        .reduce((sum, units) => sum + Number(units[1] || 0), 0);

      if (item.portions > 1) {
        // Priced per portion, matching the server's arithmetic exactly: what
        // you took costs what it costs.
        if (mine > 0) { raw += portionsCost(item.line_total_cents, item.portions, mine); count += 1; }
        const spare = Math.max(0, item.portions - mine - others);
        if (spare > 0 && shareOutTheRest) {
          const claimedCost = portionsCost(item.line_total_cents, item.portions, mine + others);
          raw += (item.line_total_cents - claimedCost) / headcount;
        }
        continue;
      }

      if (mine <= 0) {
        if (shareOutTheRest && others === 0) raw += item.line_total_cents / headcount;
        continue;
      }
      count += 1;
      // A shared whole line splits between everyone who ticked it.
      raw += item.line_total_cents / (mine + others);
    }
    mount(totalBox,
      h('div.spread', {},
        h('span', {}, h('div.label', {}, 'Your items'),
          h('div.faint.tiny', {}, `${count} of ${data.items.length}`)),
        h('span.strong', { style: { fontSize: '1.35rem' } }, money(Math.round(raw), currency))),
      h('p.tiny.faint', { style: { margin: '8px 0 0' } },
        'Before tax, tip and fees are shared out'
        // Covering somebody moves the final figure too, so name it here rather
        // than letting the next screen come as a surprise.
        + (notPaying.length
          ? `, and before ${listNames(notPaying)}’s share is covered`
          : '')
        + '. Your final figure is on the next screen.'),
    );
  };

  const itemList = h('div.list');
  const drawItems = () => {
    mount(itemList, ...data.items.map((item) => {
      const mine = state.taken.get(item.id) || 0;
      const on = mine > 0;
      const divided = item.portions > 1;
      const otherNames = Object.keys(item.claims || {})
        .filter((party) => party !== me.party)
        .map(nameOf)
        .filter(Boolean);

      const set = (units) => {
        const clamped = Math.max(0, Math.min(item.portions, units));
        if (clamped === 0) state.taken.delete(item.id);
        else state.taken.set(item.id, clamped);
        state.dirty = true;
        drawItems();
        drawTotal();
      };

      const subLines = item.sub_items.map((sub) => h('div.meta', {},
        `↳ ${sub.label}`,
        sub.amount_cents ? ` ${money(sub.amount_cents, currency)}` : ''));

      // A divided line is priced per portion, so that is the number to show -
      // "$6.00 each" answers "what do I owe if I had one" immediately.
      const perPortion = divided
        ? Math.round(item.line_total_cents / item.portions)
        : item.line_total_cents;
      const evenly = divided && item.line_total_cents % item.portions === 0;
      const takenByAll = Object.values(item.claims || {})
        .reduce((sum, n) => sum + Number(n || 0), 0);
      const spare = divided ? Math.max(0, item.portions - takenByAll) : 0;

      const detail = h('span.grow', {},
        h('div.title', {}, item.label,
          divided
            ? h('span.faint.small', {},
                ` · ${evenly ? '' : '≈'}${money(perPortion, currency)} each`)
            : null),
        ...subLines,
        h('div.meta', {},
          divided
            ? (on
                ? `you took ${mine} of ${item.portions}`
                  + (spare ? ` · ${spare} still spare` : '')
                : `${item.portions} portions${spare ? `, ${spare} spare` : ''} — tap + for yours`)
            : otherNames.length
              ? `shared with ${otherNames.join(', ')}`
              : on ? 'just you' : 'tap to add'),
      );

      // A divided line needs a stepper, so the row is a div with its own
      // buttons rather than one big tappable button.
      if (divided) {
        return h('div.item', { style: on ? { background: 'var(--accent-soft)' } : {} },
          detail,
          h('span.stack-sm', { style: { textAlign: 'right' } },
            h('span.money', {}, money(item.line_total_cents, currency)),
            h('span.portion-pick', { class: on ? 'on' : '' },
              h('button.icon-btn', {
                type: 'button', 'aria-label': 'One fewer',
                disabled: readOnly || mine <= 0, onclick: () => set(mine - 1),
              }, '−'),
              h('span.portion-count', {}, String(mine)),
              h('button.icon-btn', {
                type: 'button', 'aria-label': 'One more',
                disabled: readOnly || mine >= item.portions, onclick: () => set(mine + 1),
              }, '+'))),
        );
      }

      return h('button.item', {
        type: 'button',
        disabled: readOnly,
        style: on ? { background: 'var(--accent-soft)' } : {},
        onclick: () => set(on ? 0 : 1),
      },
        h('span.cat-icon', {
          style: on ? { background: 'var(--accent)', color: 'var(--accent-text)' } : {},
        }, on ? '✓' : ''),
        detail,
        h('span.money', {}, money(item.line_total_cents, currency)),
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
      state.data = await api.saveClaims(me.party, Object.fromEntries(state.taken));
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
          h('div.meta', {}, person.exempt
            ? 'not paying — covered by everyone else'
            : person.claimed_items
              ? `${person.claimed_items} item${person.claimed_items === 1 ? '' : 's'}`
              : data.itemized ? 'nothing picked yet' : 'even share')),
        h('span.money.strong', {}, money(person.owed_cents, currency)))),
        // Without this the list plainly does not add up to the total, and the
        // reason it does not is exactly what people need to see.
        data.totals.unassigned_cents
          ? h('div.item.unclaimed-row', { style: { cursor: 'default' } },
              h('span.avatar', { style: { background: 'var(--warn)' } }, '?'),
              h('span.grow', {},
                h('div.title', {}, 'Nobody has claimed'),
                h('div.meta', {}, 'tap “Change my picks” if something was yours')),
              h('span.money.strong', {}, money(data.totals.unassigned_cents, currency)))
          : null),
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

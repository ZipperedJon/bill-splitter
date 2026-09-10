// Group list, and the group detail screen (bills / balances / people / settings).

import { api } from '../api.js';
import {
  $, add, categoryIcon, centsToInput, clear, confirmDialog, emptyState, h, initials,
  modal, money, moneyAbs, mount, niceDate, toast,
} from '../util.js';
import { go, route, state } from '../app.js';

// --- list --------------------------------------------------------------------

export async function renderGroupList() {
  const view = $('#view');
  const [{ groups }, { users }] = await Promise.all([api.groups(true), api.directory()]);
  const active = groups.filter((g) => !g.archived);
  const archived = groups.filter((g) => g.archived);

  mount(view, 
    h('div.page-head', {},
      h('div.grow', {}, h('h1', {}, 'Groups'),
        h('div.sub', {}, 'A group is a trip, a household, or any set of people you split with.')),
      h('button.btn.btn-primary', { type: 'button', onclick: () => newGroupDialog(users) }, '+ New group'),
    ),
    active.length
      ? h('div.stack', {}, ...active.map(groupCard))
      : h('div.card', {}, emptyState('👥', 'No groups yet',
          'Create one, add the people you split with, then start adding bills.',
          h('button.btn.btn-primary', {
            type: 'button', style: { marginTop: '12px' },
            onclick: () => newGroupDialog(users),
          }, 'Create a group'))),
    archived.length ? h('div', { style: { marginTop: '22px' } },
      h('h3.section-title', {}, 'Archived'),
      h('div.stack', {}, ...archived.map(groupCard))) : null,
  );
}

function groupCard(group) {
  const balance = group.my_balance_cents;
  return h('a.card.item', { href: `#/g/${group.id}`, style: { padding: '15px 16px' } },
    h('span.grow', {},
      h('div.title', {}, group.name, group.archived ? h('span.badge', { style: { marginLeft: '7px' } }, 'archived') : null),
      h('div.meta', {},
        `${group.member_count + group.guest_count} people · ${group.bill_count} bill${group.bill_count === 1 ? '' : 's'}`,
        group.last_bill_date ? ` · last ${niceDate(group.last_bill_date)}` : ''),
      group.description ? h('div.meta.truncate', {}, group.description) : null),
    h('span.stack-sm', { style: { textAlign: 'right' } },
      h('span.money.strong', {}, money(group.total_spend_cents, group.currency)),
      balance === 0
        ? h('span.badge', {}, 'settled')
        : h('span.badge', { class: balance > 0 ? 'pos' : 'neg' },
            `${balance > 0 ? 'owed' : 'you owe'} ${moneyAbs(balance, group.currency)}`)),
  );
}

async function newGroupDialog(users) {
  const picked = new Set();
  const guests = [];

  const memberBox = h('div.row');
  const guestBox = h('div.row');

  const drawMembers = () => {
    mount(memberBox, ...users.filter((u) => !u.is_me).map((user) => h('button.chip', {
      type: 'button',
      'aria-pressed': picked.has(user.id) ? 'true' : 'false',
      onclick: (event) => {
        picked.has(user.id) ? picked.delete(user.id) : picked.add(user.id);
        event.currentTarget.setAttribute('aria-pressed', picked.has(user.id) ? 'true' : 'false');
      },
    }, user.name)));
    if (users.filter((u) => !u.is_me).length === 0) {
      add(memberBox, h('span.small.faint', {}, 'No other approved accounts yet — you can add guests instead.'));
    }
  };
  const drawGuests = () => {
    mount(guestBox, ...guests.map((name, index) => h('button.chip', {
      type: 'button', 'aria-pressed': 'true',
      onclick: () => { guests.splice(index, 1); drawGuests(); },
    }, name, h('span.x', {}, '×'))));
  };
  drawMembers();
  drawGuests();

  const nameInput = h('input', { type: 'text', placeholder: 'Vegas 2026', required: true, maxlength: 80 });
  const descInput = h('input', { type: 'text', placeholder: 'Optional', maxlength: 200 });
  const currencyInput = h('input', { type: 'text', value: state.defaults.currency || 'USD', maxlength: 8, style: { maxWidth: '110px' } });
  const guestInput = h('input', { type: 'text', placeholder: 'Name', maxlength: 80 });

  const addGuest = () => {
    const name = guestInput.value.trim();
    if (!name) return;
    guests.push(name);
    guestInput.value = '';
    drawGuests();
  };

  const result = await modal({
    title: 'New group',
    body: [
      h('div.field', {}, h('label', {}, 'Group name'), nameInput),
      h('div.field', {}, h('label', {}, 'Description'), descInput),
      h('div.field', {}, h('label', {}, 'Currency'), currencyInput,
        h('span.hint', {}, 'Three-letter code, e.g. USD, EUR, GBP.')),
      h('div.field', {}, h('label', {}, 'Members'), memberBox,
        h('span.hint', {}, 'You are always included as the owner.')),
      h('div.field', {}, h('label', {}, 'Guests (no account needed)'),
        h('div.row-tight', {}, guestInput,
          h('button.btn.btn-sm', { type: 'button', onclick: addGuest }, 'Add')),
        guestBox),
    ],
    onMount: () => {
      guestInput.addEventListener('keydown', (event) => {
        if (event.key === 'Enter') { event.preventDefault(); addGuest(); }
      });
    },
    actions: (close) => [
      h('button.btn', { type: 'button', onclick: () => close(null) }, 'Cancel'),
      h('button.btn.btn-primary', {
        type: 'button',
        onclick: () => {
          if (!nameInput.value.trim()) return nameInput.focus();
          close({
            name: nameInput.value.trim(),
            description: descInput.value.trim(),
            currency: currencyInput.value.trim().toUpperCase() || 'USD',
            member_ids: [...picked],
            guest_names: guests,
          });
        },
      }, 'Create group'),
    ],
  });

  if (!result) return;
  try {
    const created = await api.createGroup(result);
    toast('Group created.', 'ok');
    go(`#/g/${created.group.id}`);
  } catch (error) {
    toast(error.message, 'err');
  }
}

// --- detail ------------------------------------------------------------------

export async function renderGroup(groupId, tab = 'bills') {
  const view = $('#view');
  const data = await api.group(groupId);
  const group = data.group;
  const canManage = data.my_role === 'owner' || data.my_role === 'admin' || state.user.is_admin;

  const reload = () => route();

  mount(view, 
    h('div.page-head', {},
      h('div.grow', {},
        h('div.row-tight', { style: { marginBottom: '2px' } },
          h('a.small.dim', { href: '#/groups' }, '← Groups')),
        h('h1', {}, group.name,
          group.archived ? h('span.badge', { style: { marginLeft: '8px' } }, 'archived') : null),
        h('div.sub', {},
          group.description || `${data.parties.length} people · ${data.bills.length} bill${data.bills.length === 1 ? '' : 's'}`)),
      h('a.btn.btn-primary', { href: `#/g/${groupId}/new` }, '+ Add bill'),
    ),

    h('div.grid-3', { style: { marginBottom: '16px' } },
      h('div.tile', {}, h('div.label', {}, 'Total spend'),
        h('div.value', {}, money(data.total_spend_cents, group.currency))),
      h('div.tile', {}, h('div.label', {}, 'Outstanding'),
        h('div.value', { class: data.outstanding_cents ? 'neg' : 'pos' },
          money(data.outstanding_cents, group.currency)),
        h('div.note', {}, data.outstanding_cents ? 'still to be settled' : 'everyone is square')),
      myBalanceTile(data, group),
    ),

    h('div.tabs', { role: 'tablist' },
      ...[['bills', `Bills (${data.bills.length})`], ['balances', 'Balances'],
          ['people', `People (${data.parties.length})`], ['settings', 'Settings']]
        .map(([key, label]) => h('button', {
          type: 'button', role: 'tab', 'aria-selected': tab === key ? 'true' : 'false',
          onclick: () => go(`#/g/${groupId}?tab=${key}`),
        }, label))),

    tab === 'balances' ? balancesTab(data, group, reload)
      : tab === 'people' ? peopleTab(data, group, canManage, reload)
      : tab === 'settings' ? settingsTab(data, group, canManage, reload)
      : billsTab(data, group),
  );
}

function myBalanceTile(data, group) {
  const mine = data.balances.find((b) => b.party === data.my_party);
  const balance = mine ? mine.balance_cents : 0;
  return h('div.tile', {},
    h('div.label', {}, 'Your position'),
    h('div.value', { class: balance > 0 ? 'pos' : balance < 0 ? 'neg' : '' },
      balance === 0 ? money(0, group.currency)
        : (balance > 0 ? '+' : '−') + moneyAbs(balance, group.currency)),
    h('div.note', {}, balance === 0 ? 'all settled' : balance > 0 ? 'you are owed' : 'you owe'),
  );
}

// --- bills tab ---------------------------------------------------------------

function billsTab(data, group) {
  if (!data.bills.length) {
    return h('div.card', {}, emptyState('🧾', 'No bills yet',
      'Add the first one — a dinner, a hotel night, a tank of gas.',
      h('a.btn.btn-primary', { href: `#/g/${group.id}/new`, style: { marginTop: '12px' } }, 'Add a bill')));
  }

  const search = h('input', { type: 'search', placeholder: 'Search bills…', style: { maxWidth: '240px' } });
  const list = h('div.list');

  const draw = () => {
    const needle = search.value.trim().toLowerCase();
    const rows = data.bills.filter((b) => !needle || b.title.toLowerCase().includes(needle));
    clear(list);
    if (!rows.length) {
      add(list, h('div.empty.small', {}, 'No bills match that.'));
      return;
    }
    for (const bill of rows) {
      add(list, h('a.item', { href: `#/b/${bill.id}` },
        h('span.cat-icon', {}, categoryIcon(bill.category_icon)),
        h('span.grow', {},
          h('div.title.truncate', {}, bill.title,
            bill.warnings.length ? h('span.badge.warn', { style: { marginLeft: '7px' } }, '!') : null),
          h('div.meta', {},
            [niceDate(bill.bill_date), bill.category_name, `${bill.participant_count} people`,
             bill.split_mode === 'itemized' ? 'itemized' : bill.split_mode === 'shares' ? 'by shares' : null]
              .filter(Boolean).join(' · '))),
        h('span.stack-sm', { style: { textAlign: 'right' } },
          h('span.money.strong', {}, money(bill.total_cents, bill.currency)),
          // Money on this bill that is on nobody's total: the bill is not
          // finished, and that is worth seeing without opening it.
          bill.unassigned_cents
            ? h('span.badge.warn', {}, `${money(bill.unassigned_cents, bill.currency)} unclaimed`)
            : null,
          bill.unpaid_cents === 0
            ? h('span.badge.pos', {}, 'paid')
            : bill.paid_total_cents === 0
              ? h('span.badge', {}, 'unpaid')
              : h('span.badge.warn', {}, `${money(bill.unpaid_cents, bill.currency)} left`))));
    }
  };
  search.addEventListener('input', draw);
  draw();

  return h('div.stack', {},
    h('div.row', {}, search, h('div.grow'),
      h('a.btn.btn-sm', { href: `/api/groups/${group.id}/export.csv` }, 'Export CSV')),
    h('div.card', {}, list),
  );
}

// --- balances tab ------------------------------------------------------------

function balancesTab(data, group, reload) {
  const settled = data.balances.every((b) => b.balance_cents === 0);

  return h('div.stack', {},
    h('div.card', {},
      h('div.card-head', {}, h('h2', {}, 'Who owes what'),
        h('span.small.faint.right', {}, 'positive = they are owed')),
      h('div.list', {}, ...data.balances.map((row) => h('div.item', { style: { cursor: 'default' } },
        h('span.avatar', {}, initials(row.name)),
        h('span.grow', {},
          h('div.title', {}, row.name,
            row.party === data.my_party ? h('span.badge.accent', { style: { marginLeft: '7px' } }, 'you') : null),
          h('div.meta', {}, row.kind === 'guest' ? 'guest' : 'member')),
        h('span.money.strong', { class: row.balance_cents > 0 ? 'pos' : row.balance_cents < 0 ? 'neg' : 'faint' },
          row.balance_cents === 0 ? money(0, group.currency)
            : (row.balance_cents > 0 ? '+' : '−') + moneyAbs(row.balance_cents, group.currency)),
      )))),

    h('div.card', {},
      h('div.card-head', {}, h('h2', {}, 'Settle up'),
        h('span.small.faint.right', {}, data.transfers.length
          ? `${data.transfers.length} transfer${data.transfers.length === 1 ? '' : 's'} clears everything`
          : '')),
      settled
        ? h('div.card-body', {}, h('div.notice.good', {}, 'Everyone is square. Nothing to pay.'))
        : h('div.list', {}, ...data.transfers.map((transfer) => h('div.item', { style: { cursor: 'default' } },
            h('span.grow', {},
              h('div.title', {}, `${transfer.from_name} → ${transfer.to_name}`),
              h('div.meta', {}, 'suggested transfer')),
            h('span.money.strong', {}, money(transfer.amount_cents, group.currency)),
            h('button.btn.btn-sm.btn-primary', {
              type: 'button',
              onclick: () => settleDialog(group, data, reload, transfer),
            }, 'Mark paid')))),
      h('div.card-foot', {}, h('div.row', {},
        h('button.btn.btn-sm', {
          type: 'button', onclick: () => settleDialog(group, data, reload, null),
        }, 'Record a payment'),
        h('div.grow'),
        h('a.btn.btn-sm', { href: `/api/groups/${group.id}/export.csv` }, 'Export CSV'))),
    ),

    data.settlements.length ? h('div.card', {},
      h('div.card-head', {}, h('h2', {}, 'Payment history')),
      h('div.list', {}, ...data.settlements.map((s) => h('div.item', { style: { cursor: 'default' } },
        h('span.cat-icon', {}, '💸'),
        h('span.grow', {},
          h('div.title', {}, `${s.from_name} paid ${s.to_name}`),
          h('div.meta', {}, [niceDate(s.paid_on), s.note].filter(Boolean).join(' · '))),
        h('span.money.strong', {}, money(s.amount_cents, group.currency)),
        h('button.btn.btn-sm.btn-ghost', {
          type: 'button', title: 'Undo this payment',
          onclick: async () => {
            if (!await confirmDialog({
              title: 'Undo payment',
              message: `Remove the ${money(s.amount_cents, group.currency)} payment from ${s.from_name} to ${s.to_name}? Balances go back to how they were.`,
              confirmLabel: 'Undo', danger: true,
            })) return;
            try {
              await api.unsettle(group.id, s.id);
              toast('Payment removed.');
              reload();
            } catch (error) { toast(error.message, 'err'); }
          },
        }, '×')))),
    ) : null,
  );
}

async function settleDialog(group, data, reload, prefill) {
  const options = data.parties.map((p) => h('option', { value: p.party }, p.name));
  const fromSelect = h('select', {}, ...options.map((o) => o.cloneNode(true)));
  const toSelect = h('select', {}, ...options.map((o) => o.cloneNode(true)));
  const amountInput = h('input', { type: 'text', inputmode: 'decimal', placeholder: '0.00' });
  const noteInput = h('input', { type: 'text', placeholder: 'Venmo, cash, …', maxlength: 200 });
  const dateInput = h('input', { type: 'date', value: new Date().toISOString().slice(0, 10) });

  if (prefill) {
    fromSelect.value = prefill.from_party;
    toSelect.value = prefill.to_party;
    amountInput.value = centsToInput(prefill.amount_cents);
  } else if (data.parties.length > 1) {
    fromSelect.value = data.my_party;
    toSelect.value = data.parties.find((p) => p.party !== data.my_party).party;
  }

  const result = await modal({
    title: 'Record a payment',
    body: [
      h('div.field', {}, h('label', {}, 'Who paid'), fromSelect),
      h('div.field', {}, h('label', {}, 'Who they paid'), toSelect),
      h('div.field', {}, h('label', {}, 'Amount'),
        h('div.money-input', {}, h('span.cur', {}, currencySymbol(group.currency)), amountInput)),
      h('div.field', {}, h('label', {}, 'Date'), dateInput),
      h('div.field', {}, h('label', {}, 'Note'), noteInput),
    ],
    actions: (close) => [
      h('button.btn', { type: 'button', onclick: () => close(null) }, 'Cancel'),
      h('button.btn.btn-primary', {
        type: 'button',
        onclick: () => close({
          from_party: fromSelect.value,
          to_party: toSelect.value,
          amount: amountInput.value,
          note: noteInput.value,
          paid_on: dateInput.value,
        }),
      }, 'Save payment'),
    ],
  });

  if (!result) return;
  try {
    await api.settle(group.id, result);
    toast('Payment recorded.', 'ok');
    reload();
  } catch (error) {
    toast(error.message, 'err');
  }
}

export function currencySymbol(code) {
  try {
    const parts = new Intl.NumberFormat(undefined, { style: 'currency', currency: (code || 'USD').toUpperCase() })
      .formatToParts(0);
    return (parts.find((p) => p.type === 'currency') || {}).value || '$';
  } catch {
    return '$';
  }
}

// --- people tab --------------------------------------------------------------

function peopleTab(data, group, canManage, reload) {
  const members = data.parties.filter((p) => p.kind === 'user');
  const guests = data.parties.filter((p) => p.kind === 'guest');

  return h('div.stack', {},
    h('div.card', {},
      h('div.card-head', {}, h('h2', {}, 'Members'),
        h('button.btn.btn-sm.right', { type: 'button', onclick: () => addMemberDialog(group, data, reload) }, '+ Add member')),
      h('div.list', {}, ...members.map((person) => h('div.item', { style: { cursor: 'default' } },
        h('span.avatar', {}, initials(person.name)),
        h('span.grow', {},
          h('div.title', {}, person.name,
            person.party === data.my_party ? h('span.badge.accent', { style: { marginLeft: '7px' } }, 'you') : null,
            person.role === 'owner' ? h('span.badge', { style: { marginLeft: '7px' } }, 'owner') : null),
          h('div.meta', {}, `@${person.username}`)),
        canManage && person.party !== data.my_party ? h('div.row-tight', {},
          h('button.btn.btn-sm', {
            type: 'button',
            onclick: async () => {
              try {
                await api.setMemberRole(group.id, person.user_id, person.role === 'owner' ? 'member' : 'owner');
                reload();
              } catch (error) { toast(error.message, 'err'); }
            },
          }, person.role === 'owner' ? 'Make member' : 'Make owner'),
          h('button.btn.btn-sm.btn-danger-quiet', {
            type: 'button',
            onclick: () => removePerson(group, reload, () => api.removeMember(group.id, person.user_id), person.name),
          }, 'Remove'),
        ) : null,
      )))),

    h('div.card', {},
      h('div.card-head', {}, h('h2', {}, 'Guests'),
        h('span.small.faint', {}, 'people without an account'),
        h('button.btn.btn-sm.right', { type: 'button', onclick: () => addGuestDialog(group, reload) }, '+ Add guest')),
      guests.length
        ? h('div.list', {}, ...guests.map((person) => h('div.item', { style: { cursor: 'default' } },
            h('span.avatar', { style: { background: 'var(--surface-3)', color: 'var(--text-dim)' } }, initials(person.name)),
            h('span.grow', {}, h('div.title', {}, person.name), h('div.meta', {}, 'guest')),
            h('div.row-tight', {},
              h('button.btn.btn-sm', {
                type: 'button',
                onclick: async () => {
                  const name = await promptDialog('Rename guest', 'Name', person.name);
                  if (!name) return;
                  try { await api.renameGuest(group.id, person.guest_id, name); reload(); }
                  catch (error) { toast(error.message, 'err'); }
                },
              }, 'Rename'),
              h('button.btn.btn-sm.btn-danger-quiet', {
                type: 'button',
                onclick: () => removePerson(group, reload, () => api.removeGuest(group.id, person.guest_id), person.name),
              }, 'Remove')),
          )))
        : h('div.card-body.small.dim', {},
            'No guests. Add one for anybody who is splitting costs but does not have a login.'),
    ),
  );
}

async function removePerson(group, reload, action, name) {
  if (!await confirmDialog({
    title: `Remove ${name}?`,
    message: `${name} will be taken out of this group. This is blocked if they appear on any bill.`,
    confirmLabel: 'Remove', danger: true,
  })) return;
  try {
    await action();
    toast(`${name} removed.`);
    reload();
  } catch (error) {
    toast(error.message, 'err');
  }
}

async function addMemberDialog(group, data, reload) {
  const { users } = await api.directory();
  const present = new Set(data.parties.filter((p) => p.kind === 'user').map((p) => p.user_id));
  const available = users.filter((u) => !present.has(u.id));

  if (!available.length) {
    toast('Every approved account is already in this group.');
    return;
  }
  const select = h('select', {}, ...available.map((u) => h('option', { value: String(u.id) }, u.name)));
  const chosen = await modal({
    title: 'Add member',
    body: [h('div.field', {}, h('label', {}, 'Account'), select),
      h('p.small.faint', { style: { margin: 0 } },
        'Only approved accounts appear here. For someone without a login, add a guest instead.')],
    actions: (close) => [
      h('button.btn', { type: 'button', onclick: () => close(null) }, 'Cancel'),
      h('button.btn.btn-primary', { type: 'button', onclick: () => close(Number(select.value)) }, 'Add'),
    ],
  });
  if (!chosen) return;
  try {
    await api.addMember(group.id, chosen);
    toast('Member added.', 'ok');
    reload();
  } catch (error) {
    toast(error.message, 'err');
  }
}

async function addGuestDialog(group, reload) {
  const name = await promptDialog('Add guest', 'Name', '', 'Someone splitting costs who has no account.');
  if (!name) return;
  try {
    await api.addGuest(group.id, name);
    toast('Guest added.', 'ok');
    reload();
  } catch (error) {
    toast(error.message, 'err');
  }
}

export async function promptDialog(title, label, value = '', hint = '') {
  const input = h('input', { type: 'text', value, maxlength: 120 });
  return modal({
    title,
    body: [h('div.field', {}, h('label', {}, label), input, hint && h('span.hint', {}, hint))],
    onMount: (panel, close) => {
      input.addEventListener('keydown', (event) => {
        if (event.key === 'Enter') { event.preventDefault(); close(input.value.trim() || null); }
      });
    },
    actions: (close) => [
      h('button.btn', { type: 'button', onclick: () => close(null) }, 'Cancel'),
      h('button.btn.btn-primary', { type: 'button', onclick: () => close(input.value.trim() || null) }, 'Save'),
    ],
  });
}

// --- settings tab ------------------------------------------------------------

function settingsTab(data, group, canManage, reload) {
  if (!canManage) {
    return h('div.card.card-body', {},
      h('div.notice', {}, 'Only a group owner or an admin can change these settings.'));
  }

  const nameInput = h('input', { type: 'text', value: group.name, maxlength: 80 });
  const descInput = h('input', { type: 'text', value: group.description, maxlength: 200 });
  const currencyInput = h('input', { type: 'text', value: group.currency, maxlength: 8, style: { maxWidth: '120px' } });

  return h('div.stack', {},
    h('div.card', {},
      h('div.card-head', {}, h('h2', {}, 'Group details')),
      h('div.card-body.stack', {},
        h('div.field', {}, h('label', {}, 'Name'), nameInput),
        h('div.field', {}, h('label', {}, 'Description'), descInput),
        h('div.field', {}, h('label', {}, 'Currency'), currencyInput,
          h('span.hint', {}, 'Applies to new bills; existing bills keep the currency they were saved with.')),
      ),
      h('div.card-foot', {}, h('div.row', {},
        h('div.grow'),
        h('button.btn.btn-primary', {
          type: 'button',
          onclick: async () => {
            try {
              await api.updateGroup(group.id, {
                name: nameInput.value.trim(),
                description: descInput.value.trim(),
                currency: currencyInput.value.trim().toUpperCase(),
              });
              toast('Saved.', 'ok');
              reload();
            } catch (error) { toast(error.message, 'err'); }
          },
        }, 'Save changes'))),
    ),

    h('div.card', {},
      h('div.card-head', {}, h('h3', {}, group.archived ? 'Restore group' : 'Archive group')),
      h('div.card-body', {},
        h('p.small.dim', { style: { margin: 0 } }, group.archived
          ? 'This group is archived and hidden from your main list. Restoring puts it back.'
          : 'Archiving hides the group without touching any bills or balances. You can restore it any time.')),
      h('div.card-foot', {}, h('div.row', {}, h('div.grow'),
        h('button.btn', {
          type: 'button',
          onclick: async () => {
            try {
              await api.updateGroup(group.id, { archived: !group.archived });
              toast(group.archived ? 'Group restored.' : 'Group archived.');
              reload();
            } catch (error) { toast(error.message, 'err'); }
          },
        }, group.archived ? 'Restore group' : 'Archive group'))),
    ),

    h('div.card', { style: { borderColor: 'color-mix(in srgb, var(--negative) 35%, var(--border))' } },
      h('div.card-head', {}, h('h3', { class: 'neg' }, 'Delete group')),
      h('div.card-body', {},
        h('p.small.dim', { style: { margin: 0 } },
          `Permanently deletes this group and all ${data.bills.length} of its bills, payments and balances. `
          + 'A database backup is taken first, but there is no undo in the app. Archiving is usually what you want.')),
      h('div.card-foot', {}, h('div.row', {}, h('div.grow'),
        h('button.btn.btn-danger', {
          type: 'button',
          onclick: async () => {
            const typed = await promptDialog('Delete group',
              `Type the group name to confirm: ${group.name}`, '',
              `This deletes ${data.bills.length} bill(s) for good.`);
            if (typed !== group.name) {
              if (typed !== null) toast('Name did not match — nothing deleted.');
              return;
            }
            try {
              await api.deleteGroup(group.id);
              toast('Group deleted.');
              go('#/groups');
            } catch (error) { toast(error.message, 'err'); }
          },
        }, 'Delete this group'))),
    ),
  );
}

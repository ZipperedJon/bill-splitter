// Home: what you owe, what you're owed, and the fastest route to a new bill.

import { api } from '../api.js';
import { $, categoryIcon, emptyState, h, money, moneyAbs, mount, niceDate } from '../util.js';
import { state } from '../app.js';

export async function renderDashboard() {
  const view = $('#view');
  const data = await api.dashboard();
  const currency = state.defaults.currency || 'USD';
  const name = (state.user.display_name || state.user.username).split(' ')[0];

  const net = data.net_cents;
  const netNote = net === 0
    ? 'All square across every group.'
    : net > 0 ? 'People owe you more than you owe.' : 'You owe more than you are owed.';

  mount(view, 
    h('div.page-head', {},
      h('div.grow', {},
        h('h1', {}, `Hey ${name}`),
        h('div.sub', {}, data.groups.length
          ? `${data.groups.length} active group${data.groups.length === 1 ? '' : 's'}`
          : 'Make a group to start splitting costs.')),
      data.groups.length
        ? h('a.btn.btn-primary', { href: `#/g/${data.groups[0].id}/new` }, '+ Add bill')
        : h('a.btn.btn-primary', { href: '#/groups' }, '+ New group'),
    ),

    state.user.is_admin && data.pending_approvals > 0 && h('div.notice.warn', { style: { marginBottom: '14px' } },
      `${data.pending_approvals} account request${data.pending_approvals === 1 ? '' : 's'} waiting for you. `,
      h('a', { href: '#/admin?tab=users' }, 'Review now')),

    state.user.is_admin && state.updateAvailable && h('div.notice.info', { style: { marginBottom: '14px' } },
      'A newer version is available on GitHub. ',
      h('a', { href: '#/admin?tab=updates' }, 'Open updates')),

    h('div.grid-3', { style: { marginBottom: '16px' } },
      tile('You are owed', data.owed_to_me_cents, currency, 'pos'),
      tile('You owe', data.i_owe_cents, currency, 'neg'),
      h('div.tile', {},
        h('div.label', {}, 'Net'),
        h('div.value', { class: net > 0 ? 'pos' : net < 0 ? 'neg' : '' },
          (net > 0 ? '+' : net < 0 ? '−' : '') + moneyAbs(net, currency)),
        h('div.note', {}, netNote)),
    ),

    data.groups.length ? h('div.stack', {},
      whoOwesWhat(data, currency),
      groupCards(data),
      recentBills(data),
      categoryBreakdown(data, currency),
    ) : h('div.card', {}, emptyState(
      '🧾',
      'No groups yet',
      'A group is a trip, a household, or just a set of friends you split with. Bills live inside it.',
      h('a.btn.btn-primary', { href: '#/groups', style: { marginTop: '12px' } }, 'Create your first group'),
    )),
  );
}

function tile(label, cents, currency, kind) {
  return h(`div.tile${cents ? '.' + kind : ''}`, {},
    h('div.label', {}, label),
    h('div.value', {}, money(cents, currency)),
  );
}

function whoOwesWhat(data, currency) {
  const mine = [];
  for (const group of data.groups) {
    for (const transfer of group.my_transfers) {
      mine.push({ ...transfer, group });
    }
  }
  if (!mine.length) return null;

  const myParty = `u:${state.user.id}`;
  return h('div.card', {},
    h('div.card-head', {}, h('h2', {}, 'Settle up')),
    h('div.list', {}, ...mine.map((t) => {
      const iPay = t.from_party === myParty;
      const other = iPay ? t.to_name : t.from_name;
      return h('a.item', { href: `#/g/${t.group.id}?tab=balances` },
        h('span.cat-icon', {}, iPay ? '↑' : '↓'),
        h('span.grow', {},
          h('div.title', {}, iPay ? `You pay ${other}` : `${other} pays you`),
          h('div.meta', {}, t.group.name)),
        h('span.money.strong', { class: iPay ? 'neg' : 'pos' },
          money(t.amount_cents, t.group.currency || currency)),
      );
    })),
  );
}

function groupCards(data) {
  return h('div.card', {},
    h('div.card-head', {}, h('h2', {}, 'Your groups'),
      h('a.btn.btn-sm.right', { href: '#/groups' }, 'All groups')),
    h('div.list', {}, ...data.groups.map((group) => {
      const balance = group.my_balance_cents;
      return h('a.item', { href: `#/g/${group.id}` },
        h('span.grow', {},
          h('div.title', {}, group.name),
          h('div.meta', {},
            `${group.bill_count} bill${group.bill_count === 1 ? '' : 's'} · ${money(group.total_spend_cents, group.currency)} total`)),
        balance === 0
          ? h('span.badge', {}, 'settled')
          : h('span.badge', { class: balance > 0 ? 'pos' : 'neg' },
              `${balance > 0 ? 'owed' : 'you owe'} ${moneyAbs(balance, group.currency)}`),
      );
    })),
  );
}

function recentBills(data) {
  if (!data.recent_bills.length) return null;
  return h('div.card', {},
    h('div.card-head', {}, h('h2', {}, 'Recent bills')),
    h('div.list', {}, ...data.recent_bills.map((bill) => h('a.item', { href: `#/b/${bill.id}` },
      h('span.cat-icon', {}, categoryIcon(bill.category_icon)),
      h('span.grow', {},
        h('div.title.truncate', {}, bill.title),
        h('div.meta', {}, `${niceDate(bill.bill_date)} · ${bill.group_name}`)),
      h('span.money.strong', {}, money(bill.total_cents, bill.currency)),
    ))),
  );
}

function categoryBreakdown(data, currency) {
  if (!data.spend_by_category.length) return null;
  const max = Math.max(...data.spend_by_category.map((c) => c.total_cents), 1);
  const total = data.spend_by_category.reduce((sum, c) => sum + c.total_cents, 0);

  return h('div.card', {},
    h('div.card-head', {}, h('h2', {}, 'Where it goes'),
      h('span.badge.right', {}, money(total, currency))),
    h('div.card-body.stack-sm', {}, ...data.spend_by_category.slice(0, 8).map((cat) => h('div', {},
      h('div.spread', { style: { marginBottom: '4px' } },
        h('span.small', {}, cat.category),
        h('span.small.dim.money', {}, money(cat.total_cents, currency))),
      h('div.bar', {}, h('span', { style: { width: `${Math.max(2, (cat.total_cents / max) * 100)}%` } })),
    ))),
  );
}

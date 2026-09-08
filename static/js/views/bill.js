// The bill editor. One screen for both new and existing bills, with a live
// breakdown that recomputes on the server so the numbers you see while typing
// are the same numbers that get saved.

import { api } from '../api.js';
import {
  $, add, centsToInput, clear, confirmDialog, h, initials, money, moneyAbs,
  debounce, mount, pctLabel, relTime, toast, today,
} from '../util.js';
import { go, state } from '../app.js';
import { currencySymbol, promptDialog } from './groups.js';

const SPLIT_MODES = [
  ['even', 'Split evenly', 'Everyone pays the same share.'],
  ['shares', 'By shares', 'Weight each person — a couple sharing a room counts as 2.'],
  ['itemized', 'Itemized', 'Enter each line and tick who is on it. Tax and tip follow what people ordered.'],
];

export async function renderBillEditor({ groupId, billId }) {
  const view = $('#view');
  const { categories } = await api.categories();

  let bill;
  let group;
  let parties;
  // The revision we loaded. Sent back on save so the server can refuse a write
  // that would clobber picks made through the share link meanwhile.
  let loadedRevision = null;

  if (billId) {
    const detail = await api.bill(billId);
    const full = await api.group(detail.bill.group_id);
    group = full.group;
    parties = full.parties;
    bill = fromApi(detail);
    loadedRevision = detail.bill.revision;
  } else {
    const full = await api.group(groupId);
    group = full.group;
    parties = full.parties;
    bill = blankBill(group, parties);
  }

  const currency = bill.currency || group.currency || 'USD';
  const symbol = currencySymbol(currency);

  // --- live preview ---------------------------------------------------------
  const previewBox = h('div.stack-sm');
  let lastGood = null;

  const runPreview = debounce(async () => {
    try {
      const result = await api.preview({ ...toApi(bill), group_id: group.id });
      lastGood = result;
      drawPreview(previewBox, result, parties, currency, bill);
    } catch (error) {
      drawPreview(previewBox, lastGood, parties, currency, bill, error.message);
    }
  }, 220);

  // Two kinds of update, kept apart on purpose: `preview` just recomputes the
  // totals (safe to call on every keystroke), while `rerender` rebuilds the
  // form. Rebuilding on keystroke would tear the focused input out from under
  // the person typing, so only structural changes - a mode switch, adding a
  // row, toggling who is on the bill - get to do that.
  const bodyBox = h('div.stack');
  const fx = {
    preview: () => runPreview(),
    rerender: (focusKey) => { redrawBody(focusKey); runPreview(); },
  };

  const shareBox = h('div.stack');

  // Rebuilding the form throws away the DOM, which drops the scroll position
  // and whatever had focus. Both are restored here so adding a row or flipping
  // tax to a percentage does not fling you back to the top of the page.
  // `focusKey` names the input that should end up focused - the label of the
  // item you just added, the amount field you just revealed - matched against
  // the data-focus attributes the cards set.
  const redrawBody = (focusKey) => {
    const scrollY = window.scrollY;
    mount(bodyBox,
      detailsCard(bill, categories, group, fx),
      peopleCard(bill, parties, fx),
      bill.split_mode === 'itemized'
        ? itemsCard(bill, parties, symbol, fx)
        : subtotalCard(bill, symbol, fx),
      chargesCard(bill, parties, symbol, fx),
      extrasCard(bill, symbol, fx),
      paidCard(bill, parties, symbol, currency, fx),
      billId ? shareBox : newBillShareHint(),
    );
    window.scrollTo({ top: scrollY, behavior: 'instant' });

    if (!focusKey) return;
    const target = bodyBox.querySelector(`[data-focus="${focusKey}"]`);
    if (!target) return;
    target.focus({ preventScroll: true });
    if (target.select) target.select();
    // Only nudge the page if the new field is actually out of view - a plain
    // focus() would have yanked it to the middle of the screen.
    const box = target.getBoundingClientRect();
    if (box.top < 70 || box.bottom > window.innerHeight - 20) {
      target.scrollIntoView({ block: 'center', behavior: 'smooth' });
    }
  };
  redrawBody();
  runPreview();
  if (billId) mountSharePanel(shareBox, billId, bill);

  const saveButton = h('button.btn.btn-primary', { type: 'button' }, billId ? 'Save changes' : 'Save bill');
  saveButton.addEventListener('click', async () => {
    if (!bill.title.trim()) return toast('Give the bill a title.', 'err');
    if (!bill.participants.length) return toast('Add at least one person.', 'err');
    saveButton.disabled = true;
    const label = saveButton.textContent;
    saveButton.textContent = 'Saving…';
    try {
      if (billId) {
        await api.updateBill(billId, { ...toApi(bill), expected_revision: loadedRevision });
        toast('Bill saved.', 'ok');
        go(`#/g/${group.id}`);
      } else {
        await api.createBill(group.id, toApi(bill));
        toast('Bill added.', 'ok');
        go(`#/g/${group.id}`);
      }
    } catch (error) {
      if (error.status === 409) {
        // Someone picked their items through the share link while this was
        // open. Offer the reload rather than letting them stomp it.
        const reload = await confirmDialog({
          title: 'This bill changed',
          message: error.message,
          confirmLabel: 'Reload the bill',
        });
        if (reload) return go(`#/b/${billId}`);
      } else {
        toast(error.message, 'err');
      }
    } finally {
      saveButton.disabled = false;
      saveButton.textContent = label;
    }
  });

  mount(view, 
    h('div.page-head', {},
      h('div.grow', {},
        h('div.row-tight', { style: { marginBottom: '2px' } },
          h('a.small.dim', { href: `#/g/${group.id}` }, `← ${group.name}`)),
        h('h1', {}, billId ? 'Edit bill' : 'New bill')),
      billId ? h('button.btn.btn-danger-quiet', {
        type: 'button',
        onclick: async () => {
          if (!await confirmDialog({
            title: 'Delete bill',
            message: `Delete "${bill.title}"? Balances in ${group.name} will be recalculated without it.`,
            confirmLabel: 'Delete', danger: true,
          })) return;
          try {
            await api.deleteBill(billId);
            toast('Bill deleted.');
            go(`#/g/${group.id}`);
          } catch (error) { toast(error.message, 'err'); }
        },
      }, 'Delete') : null,
      saveButton,
    ),
    h('div.editor', {},
      bodyBox,
      h('div.side', {}, h('div.card', {},
        h('div.card-head', {}, h('h2', {}, 'Breakdown')),
        h('div.card-body', {}, previewBox))),
    ),
  );
}

// --- model <-> api -----------------------------------------------------------

function blankBill(group, parties) {
  const me = `u:${state.user.id}`;
  const everyone = parties.map((p) => ({ party: p.party, weight: 1 }));
  return {
    title: '',
    category_id: null,
    notes: '',
    bill_date: today(),
    currency: group.currency || state.defaults.currency || 'USD',
    split_mode: 'even',
    subtotal: '',
    discount: { mode: 'none', percent: 0, amount: '', target: 'all' },
    tax: { mode: 'none', percent: Number(state.defaults.tax_percent) || 0, amount: '' },
    tip: {
      mode: 'none',
      percent: Number(state.defaults.tip_percent) || 18,
      amount: '',
      base: state.defaults.tip_base || 'pre_tax',
    },
    extras: [],
    items: [],
    participants: everyone,
    payments: parties.some((p) => p.party === me) ? [{ party: me, amount: '' }] : [],
  };
}

function fromApi(detail) {
  const b = detail.bill;
  return {
    title: b.title,
    category_id: b.category_id,
    notes: b.notes,
    bill_date: b.bill_date,
    currency: b.currency,
    split_mode: b.split_mode,
    subtotal: b.subtotal_cents ? centsToInput(b.subtotal_cents) : '',
    discount: {
      mode: b.discount_mode,
      percent: b.discount_percent,
      amount: b.discount_cents ? centsToInput(b.discount_cents) : '',
      target: b.discount_target || 'all',
    },
    tax: { mode: b.tax_mode, percent: b.tax_percent, amount: b.tax_cents ? centsToInput(b.tax_cents) : '' },
    tip: { mode: b.tip_mode, percent: b.tip_percent, amount: b.tip_cents ? centsToInput(b.tip_cents) : '', base: b.tip_base },
    extras: detail.extras.map((e) => ({
      label: e.label, mode: e.mode, percent: e.percent,
      amount: e.cents ? centsToInput(e.cents) : '', split: e.split,
    })),
    items: detail.items.map((i) => ({
      label: i.label,
      amount: i.amount_cents ? centsToInput(i.amount_cents) : '',
      portions: i.portions || 1,
      shares: { ...i.shares },
      sub_items: (i.sub_items || []).map((s) => ({
        label: s.label,
        amount: s.amount_cents ? centsToInput(s.amount_cents) : '',
      })),
    })),
    participants: detail.participants.map((p) => ({ party: p.party, weight: p.weight })),
    payments: detail.payments.map((p) => ({ party: p.party, amount: centsToInput(p.amount_cents) })),
  };
}

function toApi(bill) {
  return {
    title: bill.title,
    category_id: bill.category_id,
    notes: bill.notes,
    bill_date: bill.bill_date,
    currency: bill.currency,
    split_mode: bill.split_mode,
    subtotal: bill.subtotal || 0,
    discount: charge(bill.discount),
    tax: charge(bill.tax),
    tip: { ...charge(bill.tip), base: bill.tip.base },
    extras: bill.extras.map((e) => ({
      label: e.label, mode: e.mode, percent: Number(e.percent) || 0,
      amount: e.amount || 0, split: e.split,
    })),
    items: bill.items.map((i) => ({
      label: i.label,
      amount: i.amount || 0,
      portions: Math.max(1, Number(i.portions) || 1),
      shares: i.shares,
      sub_items: (i.sub_items || [])
        .filter((s) => (s.label || '').trim() || Number(s.amount))
        .map((s) => ({ label: s.label, amount: s.amount || 0 })),
    })),
    participants: bill.participants.map((p) => ({ party: p.party, weight: Number(p.weight) || 0 })),
    payments: bill.payments.filter((p) => p.amount !== '' && Number(p.amount) !== 0)
      .map((p) => ({ party: p.party, amount: p.amount })),
  };
}

function charge(value) {
  const out = { mode: value.mode, percent: Number(value.percent) || 0, amount: value.amount || 0 };
  if (value.target) out.target = value.target;
  return out;
}

// --- cards -------------------------------------------------------------------

function detailsCard(bill, categories, group, fx) {
  const titleInput = h('input', {
    type: 'text', value: bill.title, placeholder: 'Dinner at Lotus of Siam', maxlength: 120,
    oninput: (event) => { bill.title = event.target.value; },
  });
  const dateInput = h('input', {
    type: 'date', value: bill.bill_date,
    onchange: (event) => { bill.bill_date = event.target.value || today(); },
  });
  const categorySelect = h('select', {
    onchange: (event) => { bill.category_id = event.target.value ? Number(event.target.value) : null; },
  },
    h('option', { value: '' }, 'Uncategorised'),
    ...categories.map((c) => h('option', {
      value: String(c.id), selected: bill.category_id === c.id,
    }, c.name)),
  );
  const notesInput = h('textarea', {
    placeholder: 'Optional notes', maxlength: 2000,
    oninput: (event) => { bill.notes = event.target.value; },
  }, bill.notes);

  return h('div.card', {},
    h('div.card-body.stack', {},
      h('div.field', {}, h('label', {}, 'What was it?'), titleInput),
      h('div.grid-2', {},
        h('div.field', {}, h('label', {}, 'Date'), dateInput),
        h('div.field', {}, h('label', {}, 'Category'),
          h('div.row-tight', {}, categorySelect,
            h('button.icon-btn', {
              type: 'button', title: 'Add a category',
              onclick: async () => {
                const name = await promptDialog('New category', 'Name', '', 'e.g. Ski passes, Boat rental');
                if (!name) return;
                try {
                  const { category } = await api.createCategory({ name });
                  categories.push(category);
                  bill.category_id = category.id;
                  toast('Category added.', 'ok');
                  fx.rerender();
                } catch (error) { toast(error.message, 'err'); }
              },
            }, '+'))),
      ),
      h('div.field', {}, h('label', {}, 'Notes'), notesInput),
      h('div.field', {}, h('label', {}, 'How to split'),
        h('div.stack-sm', {},
          h('div.seg', {}, ...SPLIT_MODES.map(([key, label]) => h('button', {
            type: 'button', 'aria-pressed': bill.split_mode === key ? 'true' : 'false',
            onclick: () => {
              bill.split_mode = key;
              if (key === 'itemized' && !bill.items.length) {
                bill.items = [newItem()];
              }
              fx.rerender();
            },
          }, label))),
          h('span.hint', {}, (SPLIT_MODES.find((m) => m[0] === bill.split_mode) || [])[2]))),
    ),
  );
}

function peopleCard(bill, parties, fx) {
  const onBill = new Set(bill.participants.map((p) => p.party));

  return h('div.card', {},
    h('div.card-head', {}, h('h3', {}, 'Who is on this bill'),
      h('span.small.faint.right', {}, `${bill.participants.length} of ${parties.length}`)),
    h('div.card-body', {},
      h('div.row', { style: { marginBottom: bill.split_mode === 'shares' ? '12px' : '0' } },
        ...parties.map((person) => h('button.chip', {
          type: 'button', 'aria-pressed': onBill.has(person.party) ? 'true' : 'false',
          onclick: () => {
            if (onBill.has(person.party)) {
              bill.participants = bill.participants.filter((p) => p.party !== person.party);
              bill.payments = bill.payments.filter((p) => p.party !== person.party);
              for (const item of bill.items) delete item.shares[person.party];
              // A discount aimed at someone no longer on the bill would be
              // rejected on save, so put it back to everyone.
              if (bill.discount.target === person.party) bill.discount.target = 'all';
            } else {
              bill.participants.push({ party: person.party, weight: 1 });
            }
            fx.rerender();
          },
        }, person.name)),
        h('button.chip', {
          type: 'button',
          onclick: () => {
            bill.participants = parties.map((p) => ({
              party: p.party,
              weight: (bill.participants.find((x) => x.party === p.party) || {}).weight || 1,
            }));
            fx.rerender();
          },
        }, 'Everyone'),
      ),
      bill.split_mode === 'shares' ? h('div', {},
        h('div.section-title', {}, 'Shares'),
        ...bill.participants.map((participant) => {
          const person = parties.find((p) => p.party === participant.party) || { name: '?' };
          return h('div.person-row', {},
            h('div.row-tight', {}, h('span.avatar', {}, initials(person.name)), h('span', {}, person.name)),
            h('input.w', {
              type: 'number', min: '0', step: '0.5', value: String(participant.weight),
              oninput: (event) => {
                participant.weight = Number(event.target.value) || 0;
                fx.preview();
              },
            }),
            h('span.small.faint', {}, 'shares'),
          );
        }),
      ) : null,
      !bill.participants.length ? h('div.notice.warn', {}, 'Pick at least one person.') : null,
    ),
  );
}

function subtotalCard(bill, symbol, fx) {
  return h('div.card', {},
    h('div.card-body', {},
      h('div.field', {},
        h('label', {}, 'Subtotal, before tax and tip'),
        h('div.money-input', {}, h('span.cur', {}, symbol),
          h('input', {
            type: 'text', inputmode: 'decimal', value: bill.subtotal, placeholder: '0.00',
            oninput: (event) => { bill.subtotal = event.target.value; fx.preview(); },
          })),
        h('span.hint', {}, 'The amount on the receipt before extras. Add tax and tip below.')),

      // Per-item features only exist once there are items, and nothing up to
      // this point says so - which makes them look missing rather than
      // inapplicable. Point at the switch instead.
      h('div.notice', { style: { marginTop: '4px' } },
        'Want to enter each line, add modifications like "extra bacon", or '
        + 'divide one line into portions? Set ',
        h('strong', {}, 'How to split'), ' to ', h('strong', {}, 'Itemized'), ' above. ',
        h('a', {
          href: '#', onclick: (event) => {
            event.preventDefault();
            bill.split_mode = 'itemized';
            if (!bill.items.length) bill.items = [newItem()];
            fx.rerender('item-label-0');
          },
        }, 'Switch now')),
    ),
  );
}

function newItem() {
  return { label: '', amount: '', portions: 1, shares: {}, sub_items: [] };
}

function itemsCard(bill, parties, symbol, fx) {
  const rows = bill.items.map((item, index) => {
    const portions = Math.max(1, Number(item.portions) || 1);
    const divided = portions > 1;

    // Whole line: a chip toggles you on or off. Divided line: a stepper, since
    // "how many of the three beers" is the actual question.
    const shareControls = bill.participants.map((participant) => {
      const person = parties.find((p) => p.party === participant.party) || { name: '?' };
      const taken = Number(item.shares[participant.party] || 0);

      if (!divided) {
        return h('button.chip.btn-sm', {
          type: 'button', 'aria-pressed': taken > 0 ? 'true' : 'false',
          onclick: () => {
            if (taken > 0) delete item.shares[participant.party];
            else item.shares[participant.party] = 1;
            fx.rerender();
          },
        }, person.name);
      }

      const setTaken = (next) => {
        const clamped = Math.max(0, Math.min(portions, next));
        if (clamped === 0) delete item.shares[participant.party];
        else item.shares[participant.party] = clamped;
        fx.rerender();
      };
      return h('span.portion-pick', { class: taken > 0 ? 'on' : '' },
        h('button.icon-btn.btn-sm', {
          type: 'button', 'aria-label': `One fewer for ${person.name}`,
          disabled: taken <= 0, onclick: () => setTaken(taken - 1),
        }, '−'),
        h('span.portion-name', {}, person.name),
        h('span.portion-count', {}, String(taken)),
        h('button.icon-btn.btn-sm', {
          type: 'button', 'aria-label': `One more for ${person.name}`,
          disabled: taken >= portions, onclick: () => setTaken(taken + 1),
        }, '+'),
      );
    });

    const claimed = Object.values(item.shares).reduce((sum, n) => sum + (Number(n) || 0), 0);
    const subTotal = item.sub_items.reduce((sum, s) => sum + (Number(s.amount) || 0), 0);

    const subRows = item.sub_items.map((sub, subIndex) => h('div.sub-row', {},
      h('span.sub-tick', {}, '↳'),
      h('input', {
        type: 'text', value: sub.label, placeholder: 'Add bacon, oat milk, no onions…',
        maxlength: 120, dataset: { focus: `sub-label-${index}-${subIndex}` },
        oninput: (event) => { sub.label = event.target.value; },
      }),
      h('div.money-input', {}, h('span.cur', {}, symbol),
        h('input', {
          type: 'text', inputmode: 'decimal', value: sub.amount, placeholder: '0.00',
          dataset: { focus: `sub-amount-${index}-${subIndex}` },
          oninput: (event) => { sub.amount = event.target.value; fx.preview(); },
        })),
      h('button.icon-btn', {
        type: 'button', title: 'Remove this extra',
        onclick: () => { item.sub_items.splice(subIndex, 1); fx.rerender(); },
      }, '×'),
    ));

    return h('div.item-block', {},
      h('div.item-row', {},
        h('input', {
          type: 'text', value: item.label, placeholder: `Item ${index + 1}`, maxlength: 120,
          dataset: { focus: `item-label-${index}` },
          oninput: (event) => { item.label = event.target.value; },
        }),
        h('div.money-input', {}, h('span.cur', {}, symbol),
          h('input', {
            type: 'text', inputmode: 'decimal', value: item.amount, placeholder: '0.00',
            dataset: { focus: `item-amount-${index}` },
            oninput: (event) => { item.amount = event.target.value; fx.preview(); },
          })),
        h('button.icon-btn', {
          type: 'button', title: 'Remove item',
          onclick: () => { bill.items.splice(index, 1); fx.rerender(); },
        }, '×'),

        h('div.who', {},
          h('span.label-inline', {},
            divided
              ? `Portions taken (${claimed} of ${portions}):`
              : claimed ? 'Split between:' : 'Nobody picked — splits evenly:'),
          ...shareControls),
      ),

      subRows.length
        ? h('div.sub-list', {},
            ...subRows,
            h('div.sub-foot', {},
              h('span.tiny.faint', {},
                `Extras come with this item — whoever takes it pays for them too`
                + (subTotal ? ` (+${symbol}${subTotal.toFixed(2)})` : ''))))
        : null,

      h('div.row.item-actions', {},
        h('button.btn.btn-sm.btn-ghost', {
          type: 'button',
          onclick: () => {
            item.sub_items.push({ label: '', amount: '' });
            fx.rerender(`sub-label-${index}-${item.sub_items.length - 1}`);
          },
        }, '+ Extra / modification'),

        divided
          ? h('div.row-tight', {},
              h('span.tiny.faint', {}, 'Divided into'),
              h('input.portion-input', {
                type: 'number', min: '1', max: '99', value: String(portions),
                dataset: { focus: `item-portions-${index}` },
                oninput: (event) => {
                  const next = Math.max(1, Math.min(99, Number(event.target.value) || 1));
                  item.portions = next;
                  // Nobody can hold more portions than exist any more.
                  for (const key of Object.keys(item.shares)) {
                    item.shares[key] = Math.min(item.shares[key], next);
                  }
                  fx.preview();
                },
                onchange: () => fx.rerender(`item-portions-${index}`),
              }),
              h('span.tiny.faint', {}, 'portions'),
              h('button.btn.btn-sm.btn-ghost', {
                type: 'button',
                onclick: () => {
                  item.portions = 1;
                  for (const key of Object.keys(item.shares)) item.shares[key] = 1;
                  fx.rerender();
                },
              }, 'Undo divide'))
          : h('button.btn.btn-sm.btn-ghost', {
              type: 'button',
              title: 'Split this one line into parts people can claim separately',
              onclick: () => {
                item.portions = Math.max(2, bill.participants.length || 2);
                fx.rerender(`item-portions-${index}`);
              },
            }, '÷ Divide into portions'),
      ),
    );
  });

  const itemsTotal = bill.items.reduce(
    (sum, item) => sum + (Number(item.amount) || 0)
      + item.sub_items.reduce((s, sub) => s + (Number(sub.amount) || 0), 0),
    0,
  );

  return h('div.card', {},
    h('div.card-head', {}, h('h3', {}, 'Items'),
      h('span.small.faint.right.money', {}, `${symbol}${itemsTotal.toFixed(2)}`)),
    h('div.card-body', {},
      rows.length ? h('div', {}, ...rows) : h('p.small.dim', {}, 'No items yet.'),
      h('div.row', { style: { marginTop: '12px' } },
        h('button.btn.btn-sm', {
          type: 'button',
          onclick: () => {
            bill.items.push(newItem());
            // Land the cursor in the new row's name field rather than making
            // people hunt for it after the list reflows.
            fx.rerender(`item-label-${bill.items.length - 1}`);
          },
        }, '+ Add item'),
        h('span.small.faint', {}, 'Tax and tip get split in proportion to what each person ordered.')),
    ),
  );
}

function chargeRow({ key, label, hint, value, symbol, fx, extraControls }) {
  const focusKey = `charge-${key}`;
  const control = value.mode === 'percent'
    ? h('div.pct-input', {},
        h('input', {
          type: 'number', min: '0', step: '0.001', value: String(value.percent ?? ''), placeholder: '0',
          dataset: { focus: focusKey },
          oninput: (event) => { value.percent = event.target.value; fx.preview(); },
        }),
        h('span.sym', {}, '%'))
    : value.mode === 'amount'
      ? h('div.money-input', {},
          h('span.cur', {}, symbol),
          h('input', {
            type: 'text', inputmode: 'decimal', value: value.amount, placeholder: '0.00',
            dataset: { focus: focusKey },
            oninput: (event) => { value.amount = event.target.value; fx.preview(); },
          }))
      : h('div.small.faint', { style: { paddingTop: '9px' } }, 'Not applied');

  return h('div.field', {},
    h('label', {}, label),
    h('div.charge-row', {},
      control,
      h('div.seg', {}, ...[['none', 'Off'], ['percent', '%'], ['amount', symbol]].map(([mode, text]) =>
        h('button', {
          type: 'button', 'aria-pressed': value.mode === mode ? 'true' : 'false',
          // Switching mode swaps which input is on screen, so put the cursor in
          // it - you flipped to "%" because you were about to type a number.
          onclick: () => { value.mode = mode; fx.rerender(mode === 'none' ? null : focusKey); },
        }, text))),
    ),
    extraControls,
    hint && h('span.hint', {}, hint),
  );
}

function chargesCard(bill, parties, symbol, fx) {
  const onBill = bill.participants
    .map((p) => parties.find((x) => x.party === p.party))
    .filter(Boolean);
  const targetName = (onBill.find((p) => p.party === bill.discount.target) || {}).name;

  return h('div.card', {},
    h('div.card-head', {}, h('h3', {}, 'Tax, tip and discount')),
    h('div.card-body.stack', {},
      chargeRow({
        key: 'discount', label: 'Discount', value: bill.discount, symbol, fx,
        hint: bill.discount.target === 'all'
          ? 'Taken off the subtotal before tax, shared by everyone in proportion to their share.'
          : `Comes off ${targetName || 'that person'}'s share only`
            + (bill.discount.mode === 'percent' ? ' — the percentage is of their share.' : '.'),
        extraControls: bill.discount.mode === 'none' ? null
          : h('div.row-tight', { style: { marginTop: '6px' } },
              h('span.small.faint', {}, 'Applies to:'),
              h('select', {
                style: { maxWidth: '220px' },
                onchange: (event) => { bill.discount.target = event.target.value; fx.rerender(); },
              },
                h('option', { value: 'all', selected: bill.discount.target === 'all' },
                  'Everyone'),
                ...onBill.map((person) => h('option', {
                  value: person.party, selected: bill.discount.target === person.party,
                }, `${person.name} only`)))),
      }),
      chargeRow({
        key: 'tax', label: 'Tax', value: bill.tax, symbol, fx,
        hint: 'Percentage, or type the exact amount printed on the receipt.',
      }),
      chargeRow({
        key: 'tip', label: 'Tip', value: bill.tip, symbol, fx,
        hint: 'Split in proportion to each person\'s share, so it stays fair on an itemized bill.',
        extraControls: bill.tip.mode === 'percent' ? h('div.row-tight', { style: { marginTop: '6px' } },
          h('span.small.faint', {}, 'Percent of:'),
          h('div.seg', {}, ...[['pre_tax', 'Pre-tax'], ['post_tax', 'Post-tax']].map(([base, text]) =>
            h('button', {
              type: 'button', 'aria-pressed': bill.tip.base === base ? 'true' : 'false',
              onclick: () => { bill.tip.base = base; fx.rerender(); },
            }, text)))) : null,
      }),
      h('div.row', {}, h('span.small.faint', {}, 'Quick tip:'),
        ...[15, 18, 20, 22, 25].map((pct) => h('button.chip.btn-sm', {
          type: 'button',
          'aria-pressed': bill.tip.mode === 'percent' && Number(bill.tip.percent) === pct ? 'true' : 'false',
          onclick: () => { bill.tip.mode = 'percent'; bill.tip.percent = pct; fx.rerender('charge-tip'); },
        }, `${pct}%`))),
    ),
  );
}

function extrasCard(bill, symbol, fx) {
  return h('div.card', {},
    h('div.card-head', {}, h('h3', {}, 'Other charges'),
      h('span.small.faint.right', {}, 'delivery, resort fee, service charge, cleaning…')),
    h('div.card-body', {},
      bill.extras.length ? h('div', {}, ...bill.extras.map((extra, index) => h('div', {
        style: { padding: '10px 0', borderBottom: index === bill.extras.length - 1 ? '0' : '1px solid var(--border)' },
      },
        h('div.row', {},
          h('input', {
            type: 'text', value: extra.label, placeholder: 'Delivery fee', maxlength: 60,
            style: { flex: '1 1 130px', minWidth: '110px' },
            dataset: { focus: `extra-label-${index}` },
            oninput: (event) => { extra.label = event.target.value; },
          }),
          extra.mode === 'percent'
            ? h('div.pct-input', { style: { width: '96px' } },
                h('input', {
                  type: 'number', min: '0', step: '0.01', value: String(extra.percent ?? ''),
                  dataset: { focus: `extra-value-${index}` },
                  oninput: (event) => { extra.percent = event.target.value; fx.preview(); },
                }), h('span.sym', {}, '%'))
            : h('div.money-input', { style: { width: '110px' } },
                h('span.cur', {}, symbol),
                h('input', {
                  type: 'text', inputmode: 'decimal', value: extra.amount, placeholder: '0.00',
                  dataset: { focus: `extra-value-${index}` },
                  oninput: (event) => { extra.amount = event.target.value; fx.preview(); },
                })),
          h('div.seg', {}, ...[['amount', symbol], ['percent', '%']].map(([mode, text]) => h('button', {
            type: 'button', 'aria-pressed': extra.mode === mode ? 'true' : 'false',
            onclick: () => { extra.mode = mode; fx.rerender(`extra-value-${index}`); },
          }, text))),
          h('button.icon-btn', {
            type: 'button', title: 'Remove',
            onclick: () => { bill.extras.splice(index, 1); fx.rerender(); },
          }, '×')),
        h('div.row-tight', { style: { marginTop: '6px' } },
          h('span.small.faint', {}, 'Split:'),
          h('div.seg', {}, ...[['even', 'Evenly'], ['proportional', 'By share']].map(([split, text]) => h('button', {
            type: 'button', 'aria-pressed': extra.split === split ? 'true' : 'false',
            onclick: () => { extra.split = split; fx.rerender(); },
          }, text)))),
      ))) : h('p.small.dim', { style: { margin: 0 } }, 'None.'),
      h('div.row', { style: { marginTop: '12px' } },
        h('button.btn.btn-sm', {
          type: 'button',
          onclick: () => {
            bill.extras.push({ label: '', mode: 'amount', percent: 0, amount: '', split: 'even' });
            fx.rerender(`extra-label-${bill.extras.length - 1}`);
          },
        }, '+ Add charge')),
    ),
  );
}

function paidCard(bill, parties, symbol, currency, fx) {
  const rows = bill.participants.map((participant) => {
    const person = parties.find((p) => p.party === participant.party) || { name: '?' };
    const payment = bill.payments.find((p) => p.party === participant.party);
    return h('div.person-row', {},
      h('div.row-tight', {}, h('span.avatar', {}, initials(person.name)), h('span', {}, person.name)),
      h('div.money-input', { style: { width: '120px' } },
        h('span.cur', {}, symbol),
        h('input', {
          type: 'text', inputmode: 'decimal', placeholder: '0.00',
          value: payment ? payment.amount : '',
          oninput: (event) => {
            const raw = event.target.value;
            const existing = bill.payments.find((p) => p.party === participant.party);
            if (existing) existing.amount = raw;
            else bill.payments.push({ party: participant.party, amount: raw });
            fx.preview();
          },
        })),
      h('div'),
    );
  });

  const paid = bill.payments.reduce((sum, p) => sum + (Number(p.amount) || 0), 0);

  return h('div.card', {},
    h('div.card-head', {}, h('h3', {}, 'Who paid'),
      h('span.small.faint.right.money', {}, `${symbol}${paid.toFixed(2)} recorded`)),
    h('div.card-body', {},
      rows.length ? h('div', {}, ...rows) : h('p.small.dim', { style: { margin: 0 } }, 'Add people first.'),
      h('p.small.faint', { style: { margin: '10px 0 0' } },
        'Usually one person covers the whole thing. Leave the rest blank — the balances work it out.'),
    ),
  );
}

// --- share link --------------------------------------------------------------

function newBillShareHint() {
  return h('div.card', {},
    h('div.card-head', {}, h('h3', {}, 'Let people pick their own items')),
    h('div.card-body.small.dim', {},
      'Save the bill first, then you can generate a link to send round. '
      + 'Whoever opens it says who they are and ticks what they had.'),
  );
}

/** Owner-side controls, loaded into `box` once the bill exists. */
async function mountSharePanel(box, billId, bill) {
  let share = null;
  let poller = null;

  const load = async () => {
    try {
      share = (await api.share(billId)).share;
    } catch {
      share = { exists: false };
    }
    draw();
  };

  // While the link is open, keep the "who has picked" line current so the
  // owner can watch selections arrive from across the table.
  const startPolling = () => {
    stopPolling();
    poller = setInterval(async () => {
      // Stop once this editor is off-screen; the router replaced the view.
      if (!box.isConnected) return stopPolling();
      if (document.visibilityState !== 'visible') return;
      try {
        const next = (await api.share(billId)).share;
        const changed = next.exists && share && share.exists
          && (next.people_who_picked !== share.people_who_picked
            || next.people.length !== share.people.length
            || next.views !== share.views);
        share = next;
        if (changed) draw();
      } catch { /* transient; try again next tick */ }
    }, 8000);
  };
  const stopPolling = () => {
    if (poller) clearInterval(poller);
    poller = null;
  };

  const act = async (fn, message) => {
    try {
      share = (await fn()).share;
      if (message) toast(message, 'ok');
      draw();
    } catch (error) {
      toast(error.message, 'err');
    }
  };

  const draw = () => {
    if (!share || !share.exists) {
      mount(box, h('div.card', {},
        h('div.card-head', {}, h('h3', {}, 'Let people pick their own items')),
        h('div.card-body.stack', {},
          h('p.small.dim', { style: { margin: 0 } },
            'Generate a link to send round. Whoever opens it chooses their name '
            + 'from the people on this bill — or adds their own — then ticks what '
            + 'they had. No account needed.'),
          bill.split_mode !== 'itemized'
            ? h('div.notice', {},
                'Heads up: this bill is not itemized, so there is nothing for people '
                + 'to tick. They can still add themselves and see their share. Set '
                + '"How to split" to Itemized if you want them choosing items.')
            : null,
          h('button.btn.btn-primary', {
            type: 'button',
            onclick: () => act(() => api.createShare(billId, { allow_join: true }), 'Link created.'),
          }, 'Create a share link')),
      ));
      stopPolling();
      return;
    }

    const url = `${location.origin}${share.path}`;
    const urlField = h('input', { type: 'text', value: url, readonly: true, onclick: (e) => e.target.select() });
    const picked = share.people_who_picked;
    const total = share.people.length;

    mount(box, h('div.card', {},
      h('div.card-head', {},
        h('h3', {}, 'Share link'),
        share.closed
          ? h('span.badge.warn.right', {}, 'closed')
          : h('span.badge.pos.right', {}, 'open')),
      h('div.card-body.stack', {},
        h('div.field', {},
          h('label', {}, 'Send this to everyone on the bill'),
          h('div.row-tight', {}, urlField,
            h('button.btn.btn-sm', {
              type: 'button',
              onclick: async (event) => {
                const button = event.currentTarget;
                try {
                  await navigator.clipboard.writeText(url);
                  button.textContent = 'Copied';
                  setTimeout(() => { button.textContent = 'Copy'; }, 1600);
                } catch {
                  urlField.select();
                  toast('Press Ctrl+C to copy the selected link.');
                }
              },
            }, 'Copy')),
          h('span.hint', {}, 'Anyone with this link can pick and change items. Treat it like the receipt itself.')),

        h('div.notice', { class: picked ? 'good' : '' },
          share.item_count === 0
            ? 'No items on this bill yet, so there is nothing to tick.'
            : picked === 0
              ? `Nobody has picked yet — ${total} ${total === 1 ? 'person is' : 'people are'} on the bill.`
              : `${picked} of ${total} ${picked === 1 ? 'person has' : 'people have'} picked their items.`),

        share.people.length ? h('div.stack-sm', {},
          h('div.section-title', {}, 'Who has picked'),
          ...share.people.map((person) => h('div.spread', {},
            h('span.small', {}, person.name),
            person.claimed_items
              ? h('span.badge.pos', {}, `${person.claimed_items} item${person.claimed_items === 1 ? '' : 's'}`)
              : h('span.badge', {}, 'nothing yet'))),
        ) : null,

        h('label.check', {},
          h('input', {
            type: 'checkbox', checked: share.allow_join,
            onchange: (event) => act(
              () => api.updateShare(billId, { allow_join: event.target.checked }),
              event.target.checked ? 'People can add themselves.' : 'Adding new people turned off.',
            ),
          }),
          h('span', {}, h('strong', {}, 'Let people add their own name'),
            h('div.small.dim', {}, 'For anyone who is not already on the bill.'))),

        h('label.check', {},
          h('input', {
            type: 'checkbox', checked: share.closed,
            onchange: (event) => act(
              () => api.updateShare(billId, { closed: event.target.checked }),
              event.target.checked ? 'Link closed for changes.' : 'Link reopened.',
            ),
          }),
          h('span', {}, h('strong', {}, 'Close for changes'),
            h('div.small.dim', {}, 'People can still see the bill, but not change their picks. '
              + 'Do this once everyone has answered.'))),

        h('p.tiny.faint', { style: { margin: 0 } },
          share.views
            ? `Opened ${share.views} time${share.views === 1 ? '' : 's'}`
              + (share.last_seen_at ? `, last ${relTime(share.last_seen_at)}` : '')
            : 'Not opened yet.'),
      ),
      h('div.card-foot', {}, h('div.row', {},
        h('button.btn.btn-sm', {
          type: 'button',
          onclick: () => go(`#/b/${billId}`),
        }, 'Reload picks'),
        h('div.grow'),
        h('button.btn.btn-sm', {
          type: 'button',
          onclick: async () => {
            if (!await confirmDialog({
              title: 'Replace the link?',
              message: 'A new link is generated and the old one stops working. '
                + 'Anyone you already sent it to will need the new one. Picks already made are kept.',
              confirmLabel: 'Replace link', danger: true,
            })) return;
            act(() => api.rotateShare(billId), 'New link generated.');
          },
        }, 'Replace'),
        h('button.btn.btn-sm.btn-danger-quiet', {
          type: 'button',
          onclick: async () => {
            if (!await confirmDialog({
              title: 'Delete the link?',
              message: 'The link stops working for everyone. Items people already '
                + 'picked stay on the bill.',
              confirmLabel: 'Delete link', danger: true,
            })) return;
            act(() => api.revokeShare(billId), 'Link deleted.');
          },
        }, 'Delete'))),
    ));

    if (!share.closed) startPolling();
    else stopPolling();
  };

  mount(box, h('div.card.card-pad.center', {}, h('span.spin-inline')));
  await load();
}

// --- preview -----------------------------------------------------------------

function drawPreview(box, result, parties, currency, bill, errorMessage) {
  clear(box);
  if (errorMessage) add(box, h('div.notice.bad', {}, errorMessage));
  if (!result) {
    add(box, h('p.small.dim', {}, 'Fill in the bill to see the split.'));
    return;
  }

  const totals = result.totals;
  const lines = [];
  // Zero-value lines are simply left out; a receipt with "Tip $0.00" on it is noise.
  const line = (label, cents) => {
    if (!cents) return;
    lines.push(h('div.totals-line', {},
      h('span.dim', {}, label), h('span.v', {}, money(cents, currency))));
  };

  line('Subtotal', totals.subtotal_cents);
  if (totals.discount_cents) {
    lines.push(h('div.totals-line', {},
      h('span.dim', {}, `Discount${bill.discount.mode === 'percent' ? ` (${pctLabel(bill.discount.percent)})` : ''}`),
      h('span.v.pos', {}, `−${moneyAbs(totals.discount_cents, currency)}`)));
  }
  line(`Tax${bill.tax.mode === 'percent' ? ` (${pctLabel(bill.tax.percent)})` : ''}`, totals.tax_cents);
  line(
    `Tip${bill.tip.mode === 'percent' ? ` (${pctLabel(bill.tip.percent)} ${bill.tip.base === 'post_tax' ? 'post-tax' : 'pre-tax'})` : ''}`,
    totals.tip_cents,
  );
  for (const extra of totals.extras || []) {
    line(extra.label + (extra.mode === 'percent' ? ` (${pctLabel(extra.percent)})` : ''), extra.cents);
  }

  add(box, 
    ...lines,
    h('div.totals-line.total', {}, h('span', {}, 'Total'), h('span.v', {}, money(totals.total_cents, currency))),
    totals.paid_total_cents
      ? h('div.totals-line', {}, h('span.dim', {}, 'Paid so far'),
          h('span.v', {}, money(totals.paid_total_cents, currency)))
      : null,
    totals.unpaid_cents && totals.paid_total_cents
      ? h('div.totals-line', {}, h('span.dim', {}, 'Still unpaid'),
          h('span.v.neg', {}, money(totals.unpaid_cents, currency)))
      : null,
  );

  if (result.breakdown && result.breakdown.length) {
    add(box, h('div.section-title', { style: { marginTop: '16px' } }, 'Per person'));
    for (const line of result.breakdown) {
      add(box, h('div', { style: { padding: '7px 0', borderBottom: '1px solid var(--border)' } },
        h('div.spread', {},
          h('span.small.strong.truncate', {}, line.name),
          h('span.money.strong', {}, money(line.owed_cents, currency))),
        h('div.tiny.faint', {}, [
          line.base_cents ? `base ${money(line.base_cents, currency)}` : null,
          line.discount_cents ? `disc ${money(line.discount_cents, currency)}` : null,
          line.tax_cents ? `tax ${money(line.tax_cents, currency)}` : null,
          line.tip_cents ? `tip ${money(line.tip_cents, currency)}` : null,
          line.extras_cents ? `extras ${money(line.extras_cents, currency)}` : null,
        ].filter(Boolean).join(' · ') || 'nothing yet'),
        line.paid_cents ? h('div.tiny', { class: line.balance_cents >= 0 ? 'pos' : 'neg' },
          line.balance_cents === 0 ? 'square'
            : line.balance_cents > 0
              ? `paid ${money(line.paid_cents, currency)} — is owed ${moneyAbs(line.balance_cents, currency)}`
              : `paid ${money(line.paid_cents, currency)} — owes ${moneyAbs(line.balance_cents, currency)}`) : null,
      ));
    }
  }

  for (const warning of totals.warnings || []) {
    add(box, h('div.notice.warn', { style: { marginTop: '12px' } }, warning));
  }
}

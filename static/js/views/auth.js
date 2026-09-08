// Sign in, first-run setup, and the forced password change after an admin reset.

import { api } from '../api.js';
import { h, mount, toast } from '../util.js';
import { refreshSession, route, state } from '../app.js';

function shell(title, subtitle, ...children) {
  return h('div.stack', {},
    h('div.center', { style: { margin: '28px 0 22px' } },
      h('div', { style: { fontSize: '2rem', marginBottom: '8px' } }, '🧾'),
      h('h1', {}, title),
      subtitle && h('p.dim.small', { style: { marginTop: '6px' } }, subtitle)),
    ...children,
  );
}

function formCard(fields, submitLabel, onSubmit, footer) {
  const errorBox = h('div.notice.bad', { hidden: true });
  const submit = h('button.btn.btn-primary.btn-block', { type: 'submit' }, submitLabel);

  const form = h('form.stack', {
    onsubmit: async (event) => {
      event.preventDefault();
      errorBox.hidden = true;
      submit.disabled = true;
      const original = submit.textContent;
      submit.textContent = 'Working…';
      try {
        await onSubmit(new FormData(form));
      } catch (error) {
        mount(errorBox, error.message || 'Something went wrong.');
        errorBox.hidden = false;
      } finally {
        submit.disabled = false;
        submit.textContent = original;
      }
    },
  }, ...fields, errorBox, submit);

  return h('div.card.card-body', {}, form, footer);
}

function field(label, attrs, hint) {
  return h('div.field', {},
    h('label', { for: attrs.name }, label),
    h('input', { id: attrs.name, ...attrs }),
    hint && h('span.hint', {}, hint));
}

export function renderSignIn(view) {
  const canRegister = state.bootstrap ? state.bootstrap.registration_open : true;

  mount(view, shell(
    'Bill Splitter',
    'Sign in to split costs with your group.',
    formCard(
      [
        field('Username', { name: 'username', type: 'text', required: true, autocomplete: 'username', autocapitalize: 'none' }),
        field('Password', { name: 'password', type: 'password', required: true, autocomplete: 'current-password' }),
      ],
      'Sign in',
      async (data) => {
        await api.login({
          username: String(data.get('username') || '').trim(),
          password: String(data.get('password') || ''),
        });
        await refreshSession();
        await route();
      },
      canRegister
        ? h('p.center.small.dim', { style: { margin: '14px 0 0' } },
            'No account yet? ',
            h('a', { href: '#', onclick: (e) => { e.preventDefault(); renderRegister(view); } }, 'Request access'))
        : h('p.center.small.faint', { style: { margin: '14px 0 0' } },
            'Sign-ups are closed. Ask an admin to create your account.'),
    ),
  ));
}

function renderRegister(view) {
  mount(view, shell(
    'Request access',
    'An admin approves new accounts before the first sign-in.',
    formCard(
      [
        field('Username', { name: 'username', type: 'text', required: true, autocomplete: 'username', autocapitalize: 'none' },
          'Letters, numbers, dot, dash or underscore.'),
        field('Display name', { name: 'display_name', type: 'text', autocomplete: 'name', placeholder: 'Optional' }),
        field('Email', { name: 'email', type: 'email', autocomplete: 'email', placeholder: 'Optional' },
          'Only so an admin knows who you are; the app never sends mail.'),
        field('Password', { name: 'password', type: 'password', required: true, minlength: 8, autocomplete: 'new-password' },
          'At least 8 characters.'),
      ],
      'Request account',
      async (data) => {
        const result = await api.register({
          username: String(data.get('username') || '').trim(),
          display_name: String(data.get('display_name') || '').trim(),
          email: String(data.get('email') || '').trim(),
          password: String(data.get('password') || ''),
        });
        if (result.status === 'pending') {
          mount(view, shell(
            'Request sent',
            null,
            h('div.card.card-body.stack', {},
              h('div.notice.good', {}, result.message || 'An admin needs to approve your account.'),
              h('button.btn.btn-block', { type: 'button', onclick: () => renderSignIn(view) }, 'Back to sign in')),
          ));
        } else {
          await refreshSession();
          await route();
        }
      },
      h('p.center.small.dim', { style: { margin: '14px 0 0' } },
        h('a', { href: '#', onclick: (e) => { e.preventDefault(); renderSignIn(view); } }, 'Back to sign in')),
    ),
  ));
}

export function renderSetup(view) {
  mount(view, shell(
    'Welcome',
    'Nobody has claimed this install yet. The account you create now becomes the admin.',
    formCard(
      [
        h('div.notice.info', {},
          'As admin you approve who else can join, reset or remove accounts, and control updates.'),
        field('Username', { name: 'username', type: 'text', required: true, autocomplete: 'username', autocapitalize: 'none' }),
        field('Display name', { name: 'display_name', type: 'text', autocomplete: 'name', placeholder: 'Optional' }),
        field('Password', { name: 'password', type: 'password', required: true, minlength: 8, autocomplete: 'new-password' },
          'At least 8 characters. There is no password reset for the admin, so pick something you will not lose.'),
      ],
      'Create admin account',
      async (data) => {
        await api.register({
          username: String(data.get('username') || '').trim(),
          display_name: String(data.get('display_name') || '').trim(),
          password: String(data.get('password') || ''),
        });
        toast('Admin account created. Welcome!', 'ok');
        await refreshSession();
        await route();
      },
    ),
  ));
}

export function renderForcedPasswordChange(view) {
  mount(view, shell(
    'Set a new password',
    'An admin reset your account, so pick a password only you know.',
    formCard(
      [
        field('Temporary password', { name: 'current_password', type: 'password', required: true, autocomplete: 'current-password' },
          'The one the admin gave you.'),
        field('New password', { name: 'new_password', type: 'password', required: true, minlength: 8, autocomplete: 'new-password' },
          'At least 8 characters.'),
        field('Confirm new password', { name: 'confirm', type: 'password', required: true, minlength: 8, autocomplete: 'new-password' }),
      ],
      'Save password',
      async (data) => {
        const next = String(data.get('new_password') || '');
        if (next !== String(data.get('confirm') || '')) throw new Error('The two new passwords do not match.');
        await api.changePassword({
          current_password: String(data.get('current_password') || ''),
          new_password: next,
        });
        toast('Password updated.', 'ok');
        await refreshSession();
        await route();
      },
    ),
  ));
}

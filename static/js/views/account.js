// Your own profile and password.

import { api } from '../api.js';
import { $, confirmDialog, h, initials, mount, relTime, toast } from '../util.js';
import { refreshSession, route, state } from '../app.js';

export async function renderAccount() {
  const view = $('#view');
  const user = state.user;

  const displayName = h('input', { type: 'text', value: user.display_name, maxlength: 64 });
  const email = h('input', { type: 'email', value: user.email, maxlength: 200, placeholder: 'Optional' });
  const saveProfile = h('button.btn.btn-primary', { type: 'button' }, 'Save profile');

  saveProfile.addEventListener('click', async () => {
    saveProfile.disabled = true;
    try {
      await api.saveProfile({ display_name: displayName.value.trim(), email: email.value.trim() });
      toast('Profile saved.', 'ok');
      await refreshSession();
      route();
    } catch (error) {
      toast(error.message, 'err');
    } finally {
      saveProfile.disabled = false;
    }
  });

  const currentPassword = h('input', { type: 'password', autocomplete: 'current-password' });
  const newPassword = h('input', { type: 'password', autocomplete: 'new-password', minlength: 8 });
  const confirmPassword = h('input', { type: 'password', autocomplete: 'new-password', minlength: 8 });
  const savePassword = h('button.btn.btn-primary', { type: 'button' }, 'Change password');

  savePassword.addEventListener('click', async () => {
    if (newPassword.value !== confirmPassword.value) {
      return toast('The two new passwords do not match.', 'err');
    }
    if (newPassword.value.length < 8) {
      return toast('Use at least 8 characters.', 'err');
    }
    savePassword.disabled = true;
    try {
      await api.changePassword({
        current_password: currentPassword.value,
        new_password: newPassword.value,
      });
      currentPassword.value = newPassword.value = confirmPassword.value = '';
      toast('Password changed. Other devices were signed out.', 'ok');
    } catch (error) {
      toast(error.message, 'err');
    } finally {
      savePassword.disabled = false;
    }
  });

  mount(view, 
    h('div.page-head', {},
      h('div.grow', {},
        h('div.row-tight', { style: { marginBottom: '2px' } }, h('a.small.dim', { href: '#/' }, '← Home')),
        h('h1', {}, 'Account'))),

    h('div.stack', { style: { maxWidth: '560px' } },
      h('div.card', {},
        h('div.card-body', {}, h('div.row', {},
          h('span.avatar', { style: { width: '44px', height: '44px', fontSize: '1rem' } },
            initials(user.display_name || user.username)),
          h('div.grow', {},
            h('div.strong', {}, user.display_name || user.username),
            h('div.small.faint', {}, `@${user.username}`)),
          user.is_admin ? h('span.badge.accent', {}, 'admin') : null)),
      ),

      h('div.card', {},
        h('div.card-head', {}, h('h2', {}, 'Profile')),
        h('div.card-body.stack', {},
          h('div.field', {}, h('label', {}, 'Display name'), displayName,
            h('span.hint', {}, 'What other people in your groups see.')),
          h('div.field', {}, h('label', {}, 'Email'), email,
            h('span.hint', {}, 'Stored for reference only — the app never sends email.')),
          h('div.field', {}, h('label', {}, 'Username'),
            h('input', { type: 'text', value: user.username, disabled: true }),
            h('span.hint', {}, 'Usernames cannot be changed; ask an admin to make a new account if you need a different one.')),
        ),
        h('div.card-foot', {}, h('div.row', {}, h('div.grow'), saveProfile)),
      ),

      h('div.card', {},
        h('div.card-head', {}, h('h2', {}, 'Password')),
        h('div.card-body.stack', {},
          h('div.field', {}, h('label', {}, 'Current password'), currentPassword),
          h('div.field', {}, h('label', {}, 'New password'), newPassword,
            h('span.hint', {}, 'At least 8 characters.')),
          h('div.field', {}, h('label', {}, 'Confirm new password'), confirmPassword),
          h('p.small.faint', { style: { margin: 0 } },
            'Changing your password signs you out on every other device.'),
        ),
        h('div.card-foot', {}, h('div.row', {}, h('div.grow'), savePassword)),
      ),

      h('div.card', {},
        h('div.card-head', {}, h('h3', {}, 'Sessions')),
        h('div.card-body', {},
          h('p.small.dim', { style: { margin: '0 0 10px' } },
            user.last_login_at
              ? `Last sign-in ${relTime(user.last_login_at)}. Signing out everywhere ends every session, including this one.`
              : 'Signing out everywhere ends every session, including this one.'),
          h('button.btn', {
            type: 'button',
            onclick: async () => {
              if (!await confirmDialog({
                title: 'Sign out everywhere?',
                message: 'Every device including this one is signed out. You will need to sign in again.',
                confirmLabel: 'Sign out everywhere', danger: true,
              })) return;
              try {
                await api.logoutEverywhere();
              } catch { /* the session is gone either way */ }
              state.user = null;
              location.hash = '#/';
              await refreshSession();
              route();
            },
          }, 'Sign out everywhere')),
      ),
    ),
  );
}

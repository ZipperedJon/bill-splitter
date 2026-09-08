// Admin panel: who gets in, app defaults, updates, and an activity log.

import { api } from '../api.js';
import {
  $, add, confirmDialog, h, initials, modal, mount, niceTime, relTime, toast,
} from '../util.js';
import { go, refreshSession, route, state } from '../app.js';

const TABS = [
  ['users', 'Users'],
  ['settings', 'Settings'],
  ['updates', 'Updates'],
  ['activity', 'Activity'],
];

export async function renderAdmin(tab = 'users') {
  const view = $('#view');
  const reload = () => route();

  mount(view, 
    h('div.page-head', {},
      h('div.grow', {}, h('h1', {}, 'Admin'),
        h('div.sub', {}, 'You control who can sign in, the app defaults, and updates.'))),
    h('div.tabs', { role: 'tablist' }, ...TABS.map(([key, label]) => {
      const button = h('button', {
        type: 'button', role: 'tab', 'aria-selected': tab === key ? 'true' : 'false',
        onclick: () => go(`#/admin?tab=${key}`),
      }, label);
      if (key === 'users' && state.pendingApprovals > 0) {
        add(button, h('span.badge.count', { style: { marginLeft: '6px' } }, String(state.pendingApprovals)));
      }
      if (key === 'updates' && state.updateAvailable) {
        add(button, h('span.badge.accent', { style: { marginLeft: '6px' } }, '1'));
      }
      return button;
    })),
    h('div', { id: 'admin-body' }, h('div.card.card-pad.center', {}, h('span.spin-inline'))),
  );

  const body = $('#admin-body');
  if (tab === 'settings') mount(body, await settingsTab(reload));
  else if (tab === 'updates') mount(body, await updatesTab(reload));
  else if (tab === 'activity') mount(body, await activityTab());
  else mount(body, await usersTab(reload));
}

// --- users -------------------------------------------------------------------

const STATUS_BADGE = {
  active: ['pos', 'active'],
  pending: ['warn', 'pending'],
  suspended: ['neg', 'suspended'],
  rejected: ['', 'rejected'],
};

async function usersTab(reload) {
  const data = await api.users();
  const pending = data.users.filter((u) => u.status === 'pending');
  const others = data.users.filter((u) => u.status !== 'pending');

  return h('div.stack', {},
    pending.length ? h('div.card', {},
      h('div.card-head', {},
        h('h2', {}, 'Waiting for approval'),
        h('span.badge.count.right', {}, String(pending.length))),
      h('div.list', {}, ...pending.map((user) => h('div.item', { style: { cursor: 'default' } },
        h('span.avatar', {}, initials(user.display_name)),
        h('span.grow', {},
          h('div.title', {}, user.display_name, h('span.faint.small', { style: { marginLeft: '6px' } }, `@${user.username}`)),
          h('div.meta', {}, [user.email, `asked ${relTime(user.created_at)}`].filter(Boolean).join(' · '))),
        h('div.row-tight', {},
          h('button.btn.btn-sm.btn-primary', {
            type: 'button',
            onclick: () => act(reload, () => api.approveUser(user.id, {}), `${user.username} approved.`),
          }, 'Approve'),
          h('button.btn.btn-sm', {
            type: 'button',
            onclick: async () => {
              if (!await confirmDialog({
                title: `Decline ${user.username}?`,
                message: 'They will not be able to sign in. You can approve them later if you change your mind.',
                confirmLabel: 'Decline', danger: true,
              })) return;
              act(reload, () => api.rejectUser(user.id, {}), 'Request declined.');
            },
          }, 'Decline')),
      ))),
    ) : h('div.notice.good', {}, 'No account requests waiting.'),

    h('div.card', {},
      h('div.card-head', {},
        h('h2', {}, 'Accounts'),
        h('span.small.faint', {}, `${data.admin_count} admin${data.admin_count === 1 ? '' : 's'}`),
        h('button.btn.btn-sm.right', { type: 'button', onclick: () => createUserDialog(reload) }, '+ Create account')),
      h('div.list', {}, ...others.map((user) => userRow(user, data, reload))),
    ),
  );
}

function userRow(user, data, reload) {
  const [badgeClass, badgeText] = STATUS_BADGE[user.status] || ['', user.status];
  const isMe = user.id === state.user.id;

  return h('div.item', { style: { cursor: 'default', flexWrap: 'wrap' } },
    h('span.avatar', {}, initials(user.display_name)),
    h('span.grow', {},
      h('div.title', {}, user.display_name,
        h('span.faint.small', { style: { marginLeft: '6px' } }, `@${user.username}`),
        isMe ? h('span.badge.accent', { style: { marginLeft: '7px' } }, 'you') : null,
        user.is_admin ? h('span.badge.accent', { style: { marginLeft: '7px' } }, 'admin') : null),
      h('div.meta', {}, [
        user.email,
        `joined ${relTime(user.created_at)}`,
        user.last_login_at ? `last seen ${relTime(user.last_login_at)}` : 'never signed in',
        `${user.group_count} group${user.group_count === 1 ? '' : 's'}`,
        user.live_sessions ? `${user.live_sessions} active session${user.live_sessions === 1 ? '' : 's'}` : null,
      ].filter(Boolean).join(' · ')),
      user.note ? h('div.meta', {}, `note: ${user.note}`) : null),
    h('span.badge', { class: badgeClass }, badgeText),
    h('div.row-tight', {},
      user.status === 'active' ? h('button.btn.btn-sm', {
        type: 'button',
        onclick: () => resetPasswordDialog(user, reload),
      }, 'Reset password') : null,

      user.status === 'suspended' || user.status === 'rejected' ? h('button.btn.btn-sm', {
        type: 'button',
        onclick: () => act(reload, () => api.reactivateUser(user.id), `${user.username} reactivated.`),
      }, 'Reactivate') : null,

      user.status === 'active' && !isMe ? h('button.btn.btn-sm', {
        type: 'button',
        onclick: async () => {
          if (!await confirmDialog({
            title: `Suspend ${user.username}?`,
            message: 'They are signed out immediately and cannot sign in until you reactivate them. Their bills and balances stay untouched.',
            confirmLabel: 'Suspend', danger: true,
          })) return;
          act(reload, () => api.suspendUser(user.id, {}), `${user.username} suspended.`);
        },
      }, 'Suspend') : null,

      !isMe ? h('button.btn.btn-sm', {
        type: 'button',
        onclick: () => act(
          reload,
          () => api.setUserAdmin(user.id, !user.is_admin),
          user.is_admin ? `${user.username} is no longer an admin.` : `${user.username} is now an admin.`,
        ),
      }, user.is_admin ? 'Remove admin' : 'Make admin') : null,

      !isMe ? h('button.btn.btn-sm.btn-danger-quiet', {
        type: 'button',
        onclick: () => deleteUserDialog(user, reload),
      }, 'Delete') : null,
    ),
  );
}

async function act(reload, action, successMessage) {
  try {
    await action();
    if (successMessage) toast(successMessage, 'ok');
    await refreshSession();
    reload();
  } catch (error) {
    toast(error.message, 'err');
  }
}

function showSecret(title, note, secret) {
  return modal({
    title,
    body: [
      h('p.small.dim', { style: { margin: 0 } }, note),
      h('div.secret', {}, secret),
      h('div.row', {},
        h('button.btn.btn-sm', {
          type: 'button',
          onclick: async (event) => {
            try {
              await navigator.clipboard.writeText(secret);
              event.currentTarget.textContent = 'Copied';
            } catch {
              toast('Could not copy — select the text instead.');
            }
          },
        }, 'Copy')),
      h('div.notice.warn', {}, 'This is shown once. It is stored hashed, so it cannot be looked up again.'),
    ],
    actions: (close) => [h('button.btn.btn-primary', { type: 'button', onclick: () => close(true) }, 'Done')],
  });
}

async function resetPasswordDialog(user, reload) {
  if (!await confirmDialog({
    title: `Reset ${user.username}'s password?`,
    message: 'This signs them out everywhere and issues a one-time password. They must choose a new one at their next sign-in.',
    confirmLabel: 'Reset password',
  })) return;

  try {
    const result = await api.resetUserPassword(user.id);
    await showSecret(
      `Temporary password for @${result.username}`,
      result.message,
      result.temporary_password,
    );
    reload();
  } catch (error) {
    toast(error.message, 'err');
  }
}

async function createUserDialog(reload) {
  const username = h('input', { type: 'text', maxlength: 32, autocapitalize: 'none', placeholder: 'sam' });
  const displayName = h('input', { type: 'text', maxlength: 64, placeholder: 'Sam Rivera' });
  const email = h('input', { type: 'email', maxlength: 200, placeholder: 'Optional' });
  const isAdmin = h('input', { type: 'checkbox' });

  const payload = await modal({
    title: 'Create an account',
    body: [
      h('p.small.dim', { style: { margin: 0 } },
        'Skips the request-and-approve step. You get a one-time password to hand over.'),
      h('div.field', {}, h('label', {}, 'Username'), username),
      h('div.field', {}, h('label', {}, 'Display name'), displayName),
      h('div.field', {}, h('label', {}, 'Email'), email),
      h('label.check', {}, isAdmin, h('span', {}, 'Make this account an admin')),
    ],
    actions: (close) => [
      h('button.btn', { type: 'button', onclick: () => close(null) }, 'Cancel'),
      h('button.btn.btn-primary', {
        type: 'button',
        onclick: () => {
          if (!username.value.trim()) return username.focus();
          close({
            username: username.value.trim(),
            display_name: displayName.value.trim(),
            email: email.value.trim(),
            is_admin: isAdmin.checked,
          });
        },
      }, 'Create'),
    ],
  });

  if (!payload) return;
  try {
    const result = await api.createUser(payload);
    await showSecret(
      `Temporary password for @${result.user.username}`,
      'Give this to them privately. They will be asked to pick their own password at sign-in.',
      result.temporary_password,
    );
    await refreshSession();
    reload();
  } catch (error) {
    toast(error.message, 'err');
  }
}

async function deleteUserDialog(user, reload) {
  const keepHistory = h('input', { type: 'checkbox', checked: true });
  const confirmName = h('input', { type: 'text', placeholder: user.username, autocapitalize: 'none' });

  const result = await modal({
    title: `Delete @${user.username}?`,
    body: [
      h('div.notice.bad', {}, 'This removes the account for good. There is no undo in the app, though a database backup is taken first.'),
      h('label.check', {}, keepHistory,
        h('span', {}, h('strong', {}, 'Keep their share of past bills'),
          h('div.small.dim', {}, `Their name becomes "${user.display_name} (removed)" as a guest in each group, so old totals and balances stay correct. Recommended.`))),
      h('div.field', {}, h('label', {}, `Type ${user.username} to confirm`), confirmName),
    ],
    actions: (close) => [
      h('button.btn', { type: 'button', onclick: () => close(null) }, 'Cancel'),
      h('button.btn.btn-danger', {
        type: 'button',
        onclick: () => close({ ok: confirmName.value.trim() === user.username, keep: keepHistory.checked }),
      }, 'Delete account'),
    ],
  });

  if (!result) return;
  if (!result.ok) return toast('Username did not match — nothing deleted.', 'err');

  try {
    const outcome = await api.deleteUser(user.id, result.keep);
    toast(
      outcome.history_preserved_in_groups
        ? `Account deleted; history kept in ${outcome.history_preserved_in_groups} group(s).`
        : 'Account deleted.',
      'ok',
    );
    await refreshSession();
    reload();
  } catch (error) {
    toast(error.message, 'err');
  }
}

// --- settings ----------------------------------------------------------------

async function settingsTab(reload) {
  const { settings } = await api.settings();

  const currency = h('input', { type: 'text', value: settings.default_currency, maxlength: 8, style: { maxWidth: '120px' } });
  const taxPercent = h('input', { type: 'number', min: '0', max: '100', step: '0.001', value: settings.default_tax_percent, style: { maxWidth: '130px' } });
  const tipPercent = h('input', { type: 'number', min: '0', max: '100', step: '0.5', value: settings.default_tip_percent, style: { maxWidth: '130px' } });
  const tipBase = h('select', { style: { maxWidth: '200px' } },
    h('option', { value: 'pre_tax', selected: settings.tip_base === 'pre_tax' }, 'Pre-tax subtotal'),
    h('option', { value: 'post_tax', selected: settings.tip_base === 'post_tax' }, 'Subtotal plus tax'));
  const registrationOpen = h('input', { type: 'checkbox', checked: settings.registration_open === '1' });

  const save = h('button.btn.btn-primary', { type: 'button' }, 'Save settings');
  save.addEventListener('click', async () => {
    save.disabled = true;
    try {
      await api.saveSettings({
        default_currency: currency.value.trim().toUpperCase() || 'USD',
        default_tax_percent: taxPercent.value || '0',
        default_tip_percent: tipPercent.value || '0',
        tip_base: tipBase.value,
        registration_open: registrationOpen.checked,
      });
      toast('Settings saved.', 'ok');
      await refreshSession();
      reload();
    } catch (error) {
      toast(error.message, 'err');
    } finally {
      save.disabled = false;
    }
  });

  return h('div.stack', {},
    h('div.card', {},
      h('div.card-head', {}, h('h2', {}, 'Defaults for new bills')),
      h('div.card-body.stack', {},
        h('div.field', {}, h('label', {}, 'Currency'), currency,
          h('span.hint', {}, 'Three-letter code. Each group can override this.')),
        h('div.field', {}, h('label', {}, 'Default tax'),
          h('div.pct-input', { style: { maxWidth: '130px' } }, taxPercent, h('span.sym', {}, '%')),
          h('span.hint', {}, 'Your local sales tax, prefilled on new bills. Leave at 0 to always type it.')),
        h('div.field', {}, h('label', {}, 'Default tip'),
          h('div.pct-input', { style: { maxWidth: '130px' } }, tipPercent, h('span.sym', {}, '%'))),
        h('div.field', {}, h('label', {}, 'Calculate tip on'), tipBase,
          h('span.hint', {}, 'Most US restaurants tip on the pre-tax subtotal.')),
      ),
      h('div.card-foot', {}, h('div.row', {}, h('div.grow'), save)),
    ),

    h('div.card', {},
      h('div.card-head', {}, h('h2', {}, 'Access')),
      h('div.card-body', {},
        h('label.check', {}, registrationOpen,
          h('span', {}, h('strong', {}, 'Let people request an account'),
            h('div.small.dim', {}, 'Requests still wait for your approval. Turn this off to hide the sign-up form entirely; you can still create accounts by hand.')))),
    ),

    h('div.card', {},
      h('div.card-head', {}, h('h2', {}, 'Backup')),
      h('div.card-body', {},
        h('p.small.dim', { style: { margin: '0 0 10px' } },
          'A snapshot is taken automatically before every update and before deleting a user or group. '
          + 'Backups live in data/backups on the Pi; the last 15 are kept.'),
        h('button.btn', {
          type: 'button',
          onclick: async (event) => {
            const button = event.currentTarget;
            button.disabled = true;
            try {
              const result = await api.backup();
              toast(result.file ? `Backup saved: ${result.file}` : 'Nothing to back up yet.', 'ok');
            } catch (error) {
              toast(error.message, 'err');
            } finally {
              button.disabled = false;
            }
          },
        }, 'Back up now')),
    ),
  );
}

// --- updates -----------------------------------------------------------------

async function updatesTab(reload) {
  const { status, history } = await api.updateStatus();
  const box = h('div.stack');

  const repoInput = h('input', { type: 'text', value: status.repo, placeholder: 'yourname/bill-splitter' });
  const branchInput = h('input', { type: 'text', value: status.configured_branch || 'main', style: { maxWidth: '160px' } });
  const autoUpdate = h('input', { type: 'checkbox', checked: status.auto_update_enabled });
  const interval = h('input', {
    type: 'number', min: '5', max: '1440', step: '5',
    value: String(status.check_interval_minutes), style: { maxWidth: '120px' },
  });

  // "up to date" is only honest once we have actually asked GitHub. Before that
  // say so, rather than reassuring the admin about a check that never ran.
  const statusBadge = !status.repo
    ? h('span.badge.right', {}, 'not configured')
    : status.update_available
      ? h('span.badge.accent.right', {}, 'update available')
      : status.last_check
        ? h('span.badge.pos.right', {}, 'up to date')
        : h('span.badge.warn.right', {}, 'not checked yet');

  const statusCard = h('div.card', {},
    h('div.card-head', {}, h('h2', {}, 'This install'), statusBadge),
    h('div.card-body.stack-sm', {},
      row('Version', `v${status.version}`),
      row('Commit', status.current_commit_short || '—', true),
      row('Branch', status.branch || '—'),
      row('Repository', status.repo || 'not set yet'),
      row('Latest on GitHub', status.latest_known_commit_short
        ? `${status.latest_known_commit_short}${status.latest_known_message ? ` — ${status.latest_known_message}` : ''}`
        : 'not checked yet', true),
      row('Last checked', status.last_check || 'never'),
      row('Auto-update', status.auto_update_enabled
        ? `on, every ${status.check_interval_minutes} min`
        : `off (still checks every ${status.check_interval_minutes} min)`),
      row('Restart method', status.under_systemd
        ? `systemd (${status.service_name})`
        : 'manual — not running under systemd'),
    ),
    h('div.card-foot', {}, h('div.row', {},
      checkButton(reload),
      applyButton(status, reload),
      h('div.grow'))),
  );

  add(box, statusCard);

  if (!status.is_git_repo) {
    add(box, h('div.notice.bad', {},
      'This copy is not a git checkout, so it cannot update itself. Re-install with install.sh to enable updates.'));
  }
  if (status.working_tree_dirty.length) {
    add(box, h('div.notice.warn', {},
      `Local edits to ${status.working_tree_dirty.slice(0, 6).join(', ')}`
      + `${status.working_tree_dirty.length > 6 ? ` and ${status.working_tree_dirty.length - 6} more` : ''}. `
      + 'Updating is blocked so your changes are not overwritten. Commit them, or use Force update.'));
  }
  if (!status.under_systemd) {
    add(box, h('div.notice.info', {},
      'Without systemd the app cannot restart itself. After an update you will need to restart it manually to load the new code.'));
  }

  const saveConfig = h('button.btn.btn-primary', { type: 'button' }, 'Save update settings');
  saveConfig.addEventListener('click', async () => {
    saveConfig.disabled = true;
    try {
      await api.saveSettings({
        update_repo: repoInput.value.trim(),
        update_branch: branchInput.value.trim() || 'main',
        auto_update_enabled: autoUpdate.checked,
        update_check_interval_minutes: interval.value || '60',
      });
      toast('Update settings saved.', 'ok');
      reload();
    } catch (error) {
      toast(error.message, 'err');
    } finally {
      saveConfig.disabled = false;
    }
  });

  add(box, h('div.card', {},
    h('div.card-head', {}, h('h2', {}, 'Update source')),
    h('div.card-body.stack', {},
      h('div.field', {}, h('label', {}, 'GitHub repository'), repoInput,
        h('span.hint', {}, 'owner/name, or paste the full GitHub URL.')),
      h('div.field', {}, h('label', {}, 'Branch'), branchInput),
      h('label.check', {}, autoUpdate,
        h('span', {}, h('strong', {}, 'Install updates automatically'),
          h('div.small.dim', {}, 'When off, the app still checks and tells you here; you press the button.'))),
      h('div.field', {}, h('label', {}, 'Check every'),
        h('div.row-tight', {}, interval, h('span.small.dim', {}, 'minutes')),
        h('span.hint', {}, 'Between 5 and 1440. Hourly is plenty.')),
      h('div.field', {}, h('label', {}, 'Private repo access token'),
        h('div.row-tight', {},
          h('input', { type: 'password', id: 'update-token', placeholder: status.has_token ? '•••••••• (set)' : 'Only needed for a private repo' }),
          h('button.btn.btn-sm', {
            type: 'button',
            onclick: async () => {
              const input = $('#update-token');
              const value = input.value.trim();
              try {
                await api.setUpdateToken(value);
                input.value = '';
                toast(value ? 'Token saved.' : 'Token cleared.', 'ok');
                reload();
              } catch (error) { toast(error.message, 'err'); }
            },
          }, 'Save'),
          status.has_token ? h('button.btn.btn-sm.btn-danger-quiet', {
            type: 'button',
            onclick: async () => {
              try {
                await api.setUpdateToken('');
                toast('Token cleared.');
                reload();
              } catch (error) { toast(error.message, 'err'); }
            },
          }, 'Clear') : null),
        h('span.hint', {}, 'Write-only: it is never shown again. A public repo needs nothing here.')),
    ),
    h('div.card-foot', {}, h('div.row', {}, h('div.grow'), saveConfig)),
  ));

  if (history.length) {
    add(box, h('div.card', {},
      h('div.card-head', {}, h('h2', {}, 'Update history')),
      h('div.table-wrap', {}, h('table', {},
        h('thead', {}, h('tr', {},
          h('th', {}, 'When'), h('th', {}, 'Result'), h('th', {}, 'Change'),
          h('th', {}, 'By'), h('th', {}, 'Detail'))),
        h('tbody', {}, ...history.map((entry) => h('tr', {},
          h('td.nowrap.small', {}, niceTime(entry.started_at)),
          h('td', {}, h('span.badge', {
            class: entry.status === 'success' ? 'pos'
              : entry.status === 'failed' || entry.status === 'rolled-back' ? 'neg'
              : entry.status === 'running' ? 'warn' : '',
          }, entry.status)),
          h('td.mono.nowrap', {}, entry.from_commit
            ? `${entry.from_commit.slice(0, 8)}${entry.to_commit ? ` → ${entry.to_commit.slice(0, 8)}` : ''}`
            : '—'),
          h('td.small', {}, entry.actor_name || entry.trigger),
          h('td.small.dim', {}, entry.message || ''))))),
      ),
    ));
  }

  return box;

  function row(label, value, mono = false) {
    return h('div.spread', {},
      h('span.small.faint', {}, label),
      h('span', { class: `small${mono ? ' mono' : ''}`, style: { textAlign: 'right' } }, value));
  }
}

function checkButton(reload) {
  const button = h('button.btn', { type: 'button' }, 'Check for updates');
  button.addEventListener('click', async () => {
    button.disabled = true;
    const label = button.textContent;
    button.textContent = 'Checking…';
    try {
      const result = await api.updateCheck();
      const behind = result.check.commits_behind;
      if (result.status.update_available) {
        toast(behind
          ? `${behind} new commit${behind === 1 ? '' : 's'} available.`
          : 'An update is available.', 'ok');
        if (result.check.changelog && result.check.changelog.length) {
          await showChangelog(result.check);
        }
      } else {
        toast('Already up to date.', 'ok');
      }
      await refreshSession();
      reload();
    } catch (error) {
      toast(error.message, 'err');
    } finally {
      button.disabled = false;
      button.textContent = label;
    }
  });
  return button;
}

function showChangelog(check) {
  return modal({
    title: `${check.commits_behind} new commit${check.commits_behind === 1 ? '' : 's'}`,
    body: [
      h('div.list', {}, ...check.changelog.slice().reverse().map((commit) => h('div', {
        style: { padding: '9px 0' },
      },
        h('div.small.strong', {}, commit.message),
        h('div.tiny.faint', {}, `${commit.sha} · ${commit.author || 'unknown'}`)))),
    ],
    actions: (close) => [h('button.btn.btn-primary', { type: 'button', onclick: () => close(true) }, 'Close')],
  });
}

function applyButton(status, reload) {
  const dirty = status.working_tree_dirty.length > 0;
  const button = h('button.btn', {
    type: 'button',
    class: status.update_available ? 'btn btn-primary' : 'btn',
    disabled: !status.is_git_repo || !status.repo,
  }, dirty ? 'Force update' : 'Update now');

  button.addEventListener('click', async () => {
    if (!await confirmDialog({
      title: dirty ? 'Force update?' : 'Update now?',
      message: dirty
        ? 'This discards your local edits to tracked files and pulls the latest commit. The database is backed up first.'
        : 'Pulls the latest commit, installs any new dependencies, checks the new code imports, then restarts. '
          + 'The database is backed up first, and a version that fails to load is rolled back automatically.',
      confirmLabel: dirty ? 'Discard and update' : 'Update',
      danger: dirty,
    })) return;

    button.disabled = true;
    button.textContent = 'Updating…';
    try {
      const { result } = await api.updateApply(dirty);
      toast(result.message, 'ok');
      if (result.restarting) {
        mount($('#admin-body'), h('div.card.card-pad.center.stack', {},
          h('span.spin-inline'),
          h('h2', {}, 'Restarting'),
          h('p.small.dim', {}, `Now on ${result.to_commit.slice(0, 8)} (v${result.to_version}). This page reloads on its own.`)));
        waitForRestart();
        return;
      }
      reload();
    } catch (error) {
      toast(error.message, 'err');
      button.disabled = false;
      button.textContent = dirty ? 'Force update' : 'Update now';
      reload();
    }
  });
  return button;
}

function waitForRestart(attempt = 0) {
  setTimeout(async () => {
    try {
      const response = await fetch('/api/health', { cache: 'no-store' });
      if (response.ok) return location.reload();
    } catch { /* still down */ }
    if (attempt < 40) waitForRestart(attempt + 1);
    else toast('The app is taking a while to come back. Check the service on the Pi.', 'err');
  }, 1500);
}

// --- activity ----------------------------------------------------------------

async function activityTab() {
  const { entries } = await api.audit();
  if (!entries.length) {
    return h('div.card.card-pad.center.dim', {}, 'Nothing logged yet.');
  }
  return h('div.card', {},
    h('div.card-head', {}, h('h2', {}, 'Activity log'),
      h('span.small.faint.right', {}, `last ${entries.length} events`)),
    h('div.table-wrap', {}, h('table', {},
      h('thead', {}, h('tr', {},
        h('th', {}, 'When'), h('th', {}, 'Who'), h('th', {}, 'Action'), h('th', {}, 'Detail'))),
      h('tbody', {}, ...entries.map((entry) => h('tr', {},
        h('td.nowrap.small', {}, niceTime(entry.created_at)),
        h('td.small', {}, entry.actor_name || 'system'),
        h('td.small.mono', {}, entry.action),
        h('td.small.dim', {}, [entry.detail, entry.target].filter(Boolean).join(' · '))))))),
  );
}

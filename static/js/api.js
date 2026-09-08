// Thin fetch wrapper. Every call goes through here so error handling and the
// "your session expired" case are dealt with in exactly one place.

export class ApiError extends Error {
  constructor(message, status, body) {
    super(message);
    this.name = 'ApiError';
    this.status = status;
    this.body = body;
  }
}

let onUnauthorized = null;

/** app.js registers a callback so a 401 anywhere bounces back to the sign-in screen. */
export function setUnauthorizedHandler(fn) {
  onUnauthorized = fn;
}

async function request(method, path, body, { raw = false, quiet401 = false } = {}) {
  let response;
  try {
    response = await fetch(path, {
      method,
      headers: body === undefined ? {} : { 'Content-Type': 'application/json' },
      body: body === undefined ? undefined : JSON.stringify(body),
      credentials: 'same-origin',
    });
  } catch {
    throw new ApiError('Cannot reach the server. Is it still running?', 0, null);
  }

  if (response.status === 401 && !quiet401 && onUnauthorized) onUnauthorized();

  if (raw) {
    if (!response.ok) throw new ApiError('Request failed.', response.status, null);
    return response;
  }

  const text = await response.text();
  let payload = null;
  if (text) {
    try {
      payload = JSON.parse(text);
    } catch {
      payload = { detail: text.slice(0, 300) };
    }
  }

  if (!response.ok) {
    throw new ApiError(errorMessage(payload, response.status), response.status, payload);
  }
  return payload;
}

function errorMessage(payload, status) {
  const detail = payload && payload.detail;
  if (typeof detail === 'string' && detail) return detail;
  // FastAPI validation errors arrive as a list of {loc, msg}.
  if (Array.isArray(detail) && detail.length) {
    const first = detail[0];
    const field = Array.isArray(first.loc) ? first.loc[first.loc.length - 1] : '';
    return field ? `${field}: ${first.msg}` : first.msg;
  }
  if (status === 401) return 'Please sign in again.';
  if (status === 403) return 'You do not have permission to do that.';
  if (status === 404) return 'Not found.';
  return `Request failed (${status}).`;
}

const get = (path, opts) => request('GET', path, undefined, opts);
const post = (path, body, opts) => request('POST', path, body ?? {}, opts);
const put = (path, body) => request('PUT', path, body ?? {});
const del = (path) => request('DELETE', path, undefined);

export const api = {
  // session
  bootstrap: () => get('/api/bootstrap'),
  me: () => get('/api/me', { quiet401: true }),
  register: (data) => post('/api/auth/register', data),
  login: (data) => post('/api/auth/login', data),
  logout: () => post('/api/auth/logout'),
  logoutEverywhere: () => post('/api/auth/logout-everywhere'),
  changePassword: (data) => post('/api/auth/change-password', data),
  saveProfile: (data) => post('/api/auth/profile', data),

  // core data
  dashboard: () => get('/api/dashboard'),
  directory: () => get('/api/directory'),
  categories: () => get('/api/categories'),
  createCategory: (data) => post('/api/categories', data),

  groups: (includeArchived = false) => get(`/api/groups?include_archived=${includeArchived ? 'true' : 'false'}`),
  group: (id) => get(`/api/groups/${id}`),
  createGroup: (data) => post('/api/groups', data),
  updateGroup: (id, data) => request('PATCH', `/api/groups/${id}`, data),
  deleteGroup: (id) => del(`/api/groups/${id}`),
  addMember: (id, userId) => post(`/api/groups/${id}/members`, { user_id: userId }),
  removeMember: (id, userId) => del(`/api/groups/${id}/members/${userId}`),
  setMemberRole: (id, userId, role) => post(`/api/groups/${id}/members/${userId}/role`, { role }),
  addGuest: (id, name) => post(`/api/groups/${id}/guests`, { name }),
  renameGuest: (id, guestId, name) => request('PATCH', `/api/groups/${id}/guests/${guestId}`, { name }),
  removeGuest: (id, guestId) => del(`/api/groups/${id}/guests/${guestId}`),
  settle: (id, data) => post(`/api/groups/${id}/settlements`, data),
  unsettle: (id, settlementId) => del(`/api/groups/${id}/settlements/${settlementId}`),

  bills: (groupId, params = '') => get(`/api/groups/${groupId}/bills${params}`),
  bill: (id) => get(`/api/bills/${id}`),
  createBill: (groupId, data) => post(`/api/groups/${groupId}/bills`, data),
  updateBill: (id, data) => put(`/api/bills/${id}`, data),
  deleteBill: (id) => del(`/api/bills/${id}`),
  preview: (data) => post('/api/bills/preview', data),

  // admin
  users: () => get('/api/admin/users'),
  approveUser: (id, data) => post(`/api/admin/users/${id}/approve`, data),
  rejectUser: (id, data) => post(`/api/admin/users/${id}/reject`, data),
  suspendUser: (id, data) => post(`/api/admin/users/${id}/suspend`, data),
  reactivateUser: (id) => post(`/api/admin/users/${id}/reactivate`),
  resetUserPassword: (id) => post(`/api/admin/users/${id}/reset-password`),
  setUserAdmin: (id, isAdmin) => post(`/api/admin/users/${id}/admin`, { is_admin: isAdmin }),
  createUser: (data) => post('/api/admin/users', data),
  deleteUser: (id, keepHistory = true) => del(`/api/admin/users/${id}?keep_history=${keepHistory}`),
  settings: () => get('/api/admin/settings'),
  saveSettings: (data) => post('/api/admin/settings', data),
  audit: () => get('/api/admin/audit'),
  backup: () => post('/api/admin/backup'),

  // updates
  updateStatus: () => get('/api/system/update'),
  updateCheck: () => post('/api/system/update/check'),
  updateApply: (force = false) => post('/api/system/update/apply', { force }),
  setUpdateToken: (token) => post('/api/system/update/token', { token }),
};

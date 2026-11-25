/*
 * Small wrappers around the REST endpoints (account, users, ping).
 * The session cookie is sent automatically by the browser (same origin),
 * so no token handling is needed here.
 */

async function request(method, url, body) {
  const res = await fetch(url, {
    method,
    headers: body ? { "Content-Type": "application/json" } : {},
    body: body ? JSON.stringify(body) : undefined,
    cache: "no-store",
  });
  let data = null;
  try { data = await res.json(); } catch { /* empty body */ }
  if (!res.ok) {
    const error = new Error((data && data.detail) || `HTTP ${res.status}`);
    error.status = res.status;
    throw error;
  }
  return data;
}

export const api = {
  register: (username, displayName, password) =>
    request("POST", "/api/register", { username, display_name: displayName, password }),
  login: (username, password) => request("POST", "/api/login", { username, password }),
  logout: () => request("POST", "/api/logout"),
  me: () => request("GET", "/api/me"),
  users: () => request("GET", "/api/users"),
  /** Protocol 2 send: the JSON response is the ACK. */
  postMessage: (msg) => request("POST", "/api/messages", msg),
  ping: (t, rtt) => request("POST", "/api/ping", { t, rtt }),
  receipt: (to, type, upTo) => request("POST", "/api/receipts", { to, type, up_to: upTo }),
  typing: (to) => request("POST", "/api/typing", { to }),
  updateProfile: (displayName, avatarHue) =>
    request("PATCH", "/api/me", { display_name: displayName, avatar_hue: avatarHue }),
};

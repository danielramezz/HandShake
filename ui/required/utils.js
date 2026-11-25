/* Small DOM and formatting helpers used by app.js and dashboard.js. */

export const $ = (selector, root = document) => root.querySelector(selector);

/** Create an element: el("div", { class: "x", text: "hi" }, child1, child2) */
export function el(tag, props = {}, ...children) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(props)) {
    if (value === undefined || value === null || value === false) continue;
    if (key === "class") node.className = value;
    else if (key === "text") node.textContent = value;
    else if (key === "dataset") Object.assign(node.dataset, value);
    else if (key.startsWith("on")) node.addEventListener(key.slice(2), value);
    else node.setAttribute(key, value === true ? "" : value);
  }
  for (const child of children.flat()) {
    if (child !== null && child !== undefined && child !== false) node.append(child);
  }
  return node;
}

/** An <svg><use href="#icon"/></svg> for the icon sprite in index.html. */
export function icon(name, cls = "icon") {
  const ns = "http://www.w3.org/2000/svg";
  const svg = document.createElementNS(ns, "svg");
  svg.setAttribute("class", cls);
  svg.setAttribute("aria-hidden", "true");
  const use = document.createElementNS(ns, "use");
  use.setAttribute("href", `#i-${name}`);
  svg.append(use);
  return svg;
}

/** A stable colour per username (same person = same colour everywhere). */
export function avatarHue(username) {
  let hash = 0;
  for (const ch of username) hash = (hash * 31 + ch.charCodeAt(0)) >>> 0;
  return hash % 360;
}

export function initials(name) {
  // Words that start with a letter or digit only: "Omar (Alex)" -> "OA", not "O(".
  const parts = name.replace(/[^\p{L}\p{N}\s]/gu, "").trim().split(/\s+/);
  return ((parts[0]?.[0] || "") + (parts[1]?.[0] || "")).toUpperCase() || "?";
}

export function avatar(user, size = "") {
  const hue = Number.isInteger(user.avatar_hue) ? user.avatar_hue : avatarHue(user.username);
  return el("div", {
    class: `avatar ${size}`,
    style: `--hue:${hue}`,
    text: initials(user.display_name || user.username),
  });
}

export function formatTime(ms) {
  return new Date(ms).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
}

export function dayLabel(ms) {
  const d = new Date(ms);
  const today = new Date();
  const yesterday = new Date(Date.now() - 86400000);
  if (d.toDateString() === today.toDateString()) return "Today";
  if (d.toDateString() === yesterday.toDateString()) return "Yesterday";
  return d.toLocaleDateString([], { weekday: "short", day: "numeric", month: "short" });
}

/** Short time for the chat list: "14:05" today, otherwise "Mon" / "3 Nov". */
export function shortStamp(ms) {
  const label = dayLabel(ms);
  return label === "Today" ? formatTime(ms) : label;
}

/** Random unique id. crypto.randomUUID needs HTTPS, so fall back on plain HTTP. */
export function uuid() {
  if (globalThis.crypto?.randomUUID) return crypto.randomUUID();
  return "xxxxxxxx-xxxx-4xxx-yxxx-xxxxxxxxxxxx".replace(/[xy]/g, (c) => {
    const r = (Math.random() * 16) | 0;
    return (c === "x" ? r : (r & 0x3) | 0x8).toString(16);
  });
}

/** "online" / "last seen today at 14:05" / "last seen yesterday at 09:12" / "last seen 3 Nov" */
export function lastSeenText(user) {
  if (user.online) return "online";
  if (!user.last_seen) return "offline";
  const minutes = Math.round((Date.now() - user.last_seen) / 60000);
  if (minutes < 1) return "last seen just now";
  if (minutes < 60) return `last seen ${minutes} min ago`;
  const day = dayLabel(user.last_seen);
  if (day === "Today" || day === "Yesterday") return `last seen ${day.toLowerCase()} at ${formatTime(user.last_seen)}`;
  return `last seen ${day}`;
}

export function formatBytes(n) {
  if (n < 1024) return `${n} B`;
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KB`;
  return `${(n / 1024 / 1024).toFixed(2)} MB`;
}

export function formatDuration(seconds) {
  const h = Math.floor(seconds / 3600);
  const m = Math.floor((seconds % 3600) / 60);
  const s = seconds % 60;
  return h ? `${h}h ${m}m` : m ? `${m}m ${s}s` : `${s}s`;
}

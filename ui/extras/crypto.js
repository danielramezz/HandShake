/*
 * End-to-end encryption (additional feature #2): symmetric AES-256-GCM.
 *
 *   shared secret (typed on BOTH phones)
 *        |  PBKDF2-SHA256, 150 000 rounds, salt = "handshake-e2ee:alice:bob"
 *        v
 *   256-bit AES key   (never leaves the phone, never sent to the server)
 *        |  AES-GCM with a fresh random 12-byte IV per message
 *        v
 *   body sent on the wire = base64( IV || ciphertext || 16-byte auth tag )
 *
 * The server stores and forwards only that base64 text, so it cannot read
 * the message. GCM's authentication tag also detects any change to the
 * ciphertext: a wrong key or a tampered message fails to decrypt instead
 * of producing garbage. That is much stronger than the Internet checksum
 * from Lecture 6.
 *
 * Uses the browser's Web Crypto API, which only exists on secure origins
 * (HTTPS or localhost). That is why the server runs HTTPS.
 */

export const available = Boolean(globalThis.crypto && crypto.subtle);

const PBKDF2_ROUNDS = 150000;
const IV_BYTES = 12;
const text = new TextEncoder();
const keyCache = new Map();

function saltFor(me, peer) {
  // Same salt on both phones: the two usernames in alphabetical order.
  return text.encode("handshake-e2ee:" + [me, peer].sort().join(":"));
}

function toBase64(bytes) {
  let s = "";
  for (const b of bytes) s += String.fromCharCode(b);
  return btoa(s);
}

function fromBase64(b64) {
  return Uint8Array.from(atob(b64), (c) => c.charCodeAt(0));
}

/** Turn the shared secret into an AES key (cached, because PBKDF2 is slow on purpose). */
export async function deriveKey(secret, me, peer) {
  const cacheKey = `${[me, peer].sort().join(":")}|${secret}`;
  if (keyCache.has(cacheKey)) return keyCache.get(cacheKey);

  const baseKey = await crypto.subtle.importKey("raw", text.encode(secret), "PBKDF2", false, ["deriveKey"]);
  const key = await crypto.subtle.deriveKey(
    { name: "PBKDF2", salt: saltFor(me, peer), iterations: PBKDF2_ROUNDS, hash: "SHA-256" },
    baseKey,
    { name: "AES-GCM", length: 256 },
    false,                       // not extractable: the raw key can't be read out
    ["encrypt", "decrypt"],
  );
  keyCache.set(cacheKey, key);
  return key;
}

export async function encryptText(key, plaintext) {
  const iv = crypto.getRandomValues(new Uint8Array(IV_BYTES));
  const cipher = new Uint8Array(await crypto.subtle.encrypt({ name: "AES-GCM", iv }, key, text.encode(plaintext)));
  const packet = new Uint8Array(IV_BYTES + cipher.length);
  packet.set(iv, 0);
  packet.set(cipher, IV_BYTES);
  return toBase64(packet);
}

/** Throws if the key is wrong or the ciphertext was modified. */
export async function decryptText(key, b64) {
  const packet = fromBase64(b64);
  const iv = packet.slice(0, IV_BYTES);
  const plain = await crypto.subtle.decrypt({ name: "AES-GCM", iv }, key, packet.slice(IV_BYTES));
  return new TextDecoder().decode(plain);
}

/**
 * A short "safety code" both people can compare out loud.
 * Same secret on both phones -> same code.
 */
export async function safetyCode(secret, me, peer) {
  const data = new Uint8Array([...saltFor(me, peer), ...text.encode("|" + secret)]);
  const hash = new Uint8Array(await crypto.subtle.digest("SHA-256", data));
  const groups = [];
  for (let i = 0; i < 6; i += 2) groups.push(String(((hash[i] << 8) | hash[i + 1]) % 10000).padStart(4, "0"));
  return groups.join(" ");
}

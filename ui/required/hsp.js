/*
 * HSP/1.0 encoder/decoder for the browser.
 * A line-by-line twin of server/required/hsp.py; see that file for the format.
 *
 *   HSP/1.0 SEND\r\n
 *   to: bob\r\n
 *   content-length: 5\r\n
 *   \r\n
 *   hello
 */

export const VERSION = "HSP/1.0";
const CRLF = "\r\n";
const utf8 = new TextEncoder();

/** Number of UTF-8 bytes in a string (what content-length counts). */
const byteLength = (s) => utf8.encode(s).length;

/** Build one HSP frame. */
export function encode(command, headers = {}, body = "") {
  const lines = [`${VERSION} ${command}`];
  for (const [name, value] of Object.entries(headers)) {
    const v = String(value);
    if (v.includes("\r") || v.includes("\n")) throw new Error(`header ${name} contains a newline`);
    lines.push(`${name.toLowerCase()}: ${v}`);
  }
  lines.push(`content-length: ${byteLength(body)}`);
  return lines.join(CRLF) + CRLF + CRLF + body;
}

/** Parse one HSP frame into { command, headers, body }. Throws on bad syntax. */
export function decode(frame) {
  const split = frame.indexOf(CRLF + CRLF);
  if (split === -1) throw new Error("missing blank line after headers");
  const head = frame.slice(0, split);
  const body = frame.slice(split + 4);

  const lines = head.split(CRLF);
  const [version, command, ...extra] = lines[0].split(" ");
  if (version !== VERSION || !command || extra.length) throw new Error("bad start line");

  const headers = {};
  for (const line of lines.slice(1)) {
    const colon = line.indexOf(":");
    if (colon <= 0) throw new Error(`bad header line ${line}`);
    headers[line.slice(0, colon).trim().toLowerCase()] = line.slice(colon + 1).trim();
  }
  if (Number(headers["content-length"]) !== byteLength(body)) {
    throw new Error("content-length does not match body size");
  }
  return { command, headers, body };
}

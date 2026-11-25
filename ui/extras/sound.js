/*
 * A short "pop" for new messages, generated with the Web Audio API
 * (no sound file to download).
 *
 * Phones only allow audio after the user has touched the page once,
 * so the first tap anywhere "unlocks" the audio context.
 */

let ctx = null;

function unlock() {
  try {
    ctx = ctx || new (window.AudioContext || window.webkitAudioContext)();
    if (ctx.state === "suspended") ctx.resume();
  } catch { /* audio not supported: stay silent */ }
}
window.addEventListener("pointerdown", unlock, { once: true });
window.addEventListener("keydown", unlock, { once: true });

/** Two quick soft notes, like most chat apps. */
export function playPop() {
  if (!ctx || ctx.state !== "running") return;
  const now = ctx.currentTime;
  [[660, 0], [880, 0.09]].forEach(([freq, delay]) => {
    const osc = ctx.createOscillator();
    const gain = ctx.createGain();
    osc.type = "sine";
    osc.frequency.value = freq;
    gain.gain.setValueAtTime(0.0001, now + delay);
    gain.gain.exponentialRampToValueAtTime(0.18, now + delay + 0.015);
    gain.gain.exponentialRampToValueAtTime(0.0001, now + delay + 0.16);
    osc.connect(gain).connect(ctx.destination);
    osc.start(now + delay);
    osc.stop(now + delay + 0.18);
  });
}

/*
 * Password strength meter for the sign-up form (a hint for the user only:
 * the server's real rule is "at least 6 characters", see server/extras/auth.py).
 */

const LABELS = ["Too short", "Weak", "Fair", "Good", "Strong"];

/** Returns { score: 0-4, label } */
export function passwordStrength(password) {
  if (password.length < 6) return { score: 0, label: LABELS[0] };
  let score = 1;
  if (password.length >= 10) score++;
  if (/[a-z]/.test(password) && /[A-Z]/.test(password)) score++;
  if (/\d/.test(password) && /[^A-Za-z0-9]/.test(password)) score++;
  else if (/\d/.test(password) || /[^A-Za-z0-9]/.test(password)) score += 0.5;
  score = Math.min(4, Math.floor(score));
  return { score, label: LABELS[score] };
}

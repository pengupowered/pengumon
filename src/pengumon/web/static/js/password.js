// A password the page proposes.

// A password Pengumon proposes for the sign-in (the first-run question, Settings > Sign-in): 16 characters in four groups,
// from letters and digits that cannot be mistaken for one another (no 0/o, 1/l/i), about 79 bits. crypto.getRandomValues
// works over plain HTTP too (a LAN address is not a secure context).
const PASSWORD_LETTERS = 'abcdefghjkmnpqrstuvwxyz23456789';
export function suggestPassword() {
  const bytes = new Uint8Array(32);
  let out = '';
  while (out.length < 16) {
    crypto.getRandomValues(bytes);
    for (const b of bytes) {
      if (out.length < 16 && b < 248) out += PASSWORD_LETTERS[b % PASSWORD_LETTERS.length]; // 248 = 8 x 31: no bias
    }
  }
  return out.match(/.{4}/g).join('-');
}

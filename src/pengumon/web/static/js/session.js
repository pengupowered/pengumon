// A sign-in that ends while the page is open (signed out in another browser, signed out everywhere, expired): the
// server refuses the page's requests with homeguard's 401 (it carries `WWW-Authenticate: Bearer realm=...`) and closes
// the live socket with 1008. The page then goes to the sign-in page, once. Without a token nothing is ever refused, so
// nothing here happens; other 401s (none today) would not carry that header.
//
// Every fetch of the page passes through here (this module is imported first by main.js and wraps window.fetch).

let leaving = false;
let held = 0; // changes of the sign-in in flight (holdSignIn)

// Returns whether the page is going to the sign-in page (false while a sign-in change is held: the caller must then
// carry on, e.g. the live socket reconnects).
export function sessionEnded() {
  if (held) return false;
  if (leaving) return true;
  leaving = true;
  window.location.assign('/login');
  return true;
}

const refusedSignIn = (res) => res.status === 401 && /^Bearer realm=/i.test(res.headers.get('www-authenticate') || '');

const nativeFetch = window.fetch.bind(window);
window.fetch = async (...args) => {
  const res = await nativeFetch(...args);
  if (refusedSignIn(res)) sessionEnded();
  return res;
};

// While this browser sets or changes the password (the first-run question, Settings > Sign-in), the server switches to
// the new one a moment before the answer that carries this browser's new cookie arrives: a request the page makes in
// between is refused, and must not send the page to the sign-in page (the new password, still on screen, would be lost).
export async function holdSignIn(work) {
  held += 1;
  try { return await work(); } finally { held -= 1; }
}

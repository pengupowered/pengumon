// The sign-out dialog (homeguard's POST /logout). The `token` variant starts the app with an access token, and smoke.mjs
// signs in through the sign-in page before the first step. The header shows Sign out only for a browser signed in that
// way; "Sign out" ends this browser's sign-in and the server refuses the old cookie from then on; "Sign out everywhere"
// asks first, then ends a second browser's sign-in too (its requests are refused, its live connection is closed).

export const subject = "dialog:sign-out";
export const variants = ["token"];

// The page's session cookie. It is HttpOnly, so it is read through the browser, the way someone copying it out of a
// browser profile would get it.
const sessionCookieOf = async (p) => (await p.browserContext().cookies()).find((c) => c.name === "pg_session");
// What the server answers a request that carries only this cookie (sent from Node, not the page: a 401 is expected).
const statusWithCookie = async (base, value) => (await fetch(`${base}/api/status`, { headers: { Cookie: `pg_session=${value}` } })).status;
const atSignInPage = (p) => new URL(p.url()).pathname === "/login";
const openSignOut = async ({ click, until }) => {
  await click("#btnSignOut");
  await until(() => document.getElementById("signOutModal").classList.contains("active"));
};

export const steps = [
  ["without a token there is nothing to sign out of: no Sign out button", async ({ eq, page }) => {
    const session = await page.evaluate(async () => (await (await fetch("/api/info")).json()).session);
    eq("session", JSON.stringify(session), JSON.stringify({ sign_out: false, sign_out_everywhere: false }));
    eq("no Sign out button", await page.$eval("#btnSignOut", (b) => b.hidden), true);
  }, { variants: ["full"] }],

  ["signed in with the token: Sign out in the header and the phone menu, its dialog fits a phone", async (ctx) => {
    const { DESKTOP, click, eq, ok, page, phone, noSideScroll, shots, until } = ctx;
    await until(() => !document.getElementById("btnSignOut").hidden);
    for (const width of [1290, 1280, 900]) { // the header's tightest widths, with and without the button labels
      await page.setViewport({ width, height: 900 });
      ok(`the header fits at ${width} px`, await page.evaluate(() => {
        const r = document.getElementById("btnSignOut").getBoundingClientRect();
        return r.width > 0 && r.right <= document.documentElement.clientWidth && document.documentElement.scrollWidth <= document.documentElement.clientWidth;
      }));
    }
    await page.setViewport(DESKTOP);
    await openSignOut(ctx);
    ok("everywhere is offered", await page.$eval("[data-sign-out-everywhere]", (e) => !e.hidden));
    eq("its confirmation is not shown yet", await page.$eval("#signOutEverywhereConfirm", (e) => e.hidden), true);
    await shots("sign-out");
    await click("[data-action=closeModal][data-arg=signOutModal]");
    await phone(async () => {
      await click(".tb-more");
      const item = "#headerMenu [data-action=openModal][data-arg=signOutModal]";
      await until((s) => { const e = document.querySelector(s); return !!e && !e.hidden && e.getBoundingClientRect().width > 0; }, item);
      await click(item);
      await until(() => document.getElementById("signOutModal").classList.contains("active"));
      ok("the dialog does not scroll sideways", await noSideScroll("signOutModal"));
      ok("nor does the page", await page.evaluate(() => document.documentElement.scrollWidth <= document.documentElement.clientWidth));
      await click("[data-action=closeModal][data-arg=signOutModal]");
    });
  }],

  ["a request the server refuses for want of a sign-in sends the page to the sign-in page", async (ctx) => {
    const { base, expectRefusals, ok, page, layoutMounted } = ctx;
    expectRefusals(true);
    try {
      // without the cookie, as if the sign-in had ended: homeguard's 401 (WWW-Authenticate: Bearer)
      await Promise.all([page.waitForNavigation({ waitUntil: "load", timeout: 15000 }),
        page.evaluate(() => { fetch("/api/status", { credentials: "omit" }); })]);
      ok(`at the sign-in page (${page.url()})`, atSignInPage(page));
    } finally {
      expectRefusals(false);
    }
    await page.goto(base + "/", { waitUntil: "load" }); // the sign-in itself is still valid
    await layoutMounted();
    ok("back on the dashboard", !atSignInPage(page));
  }],

  ["Sign out: the sign-in page again, and the old cookie is refused", async (ctx) => {
    const { base, click, eq, ok, page, layoutMounted, signInWithToken } = ctx;
    const old = await sessionCookieOf(page);
    ok("signed in with an HttpOnly session cookie", !!old && old.httpOnly);
    ok("the cookie works before", (await statusWithCookie(base, old.value)) !== 401);
    await openSignOut(ctx);
    await Promise.all([page.waitForNavigation({ waitUntil: "load" }), click("#btnSignOutHere")]);
    ok(`at the sign-in page (${page.url()})`, atSignInPage(page));
    eq("the browser's cookie is gone", await sessionCookieOf(page), undefined);
    await page.goto(base + "/", { waitUntil: "load" });
    ok("the dashboard asks for the token again", atSignInPage(page));
    eq("the old cookie, sent again", await statusWithCookie(base, old.value), 401);
    // a copy of the old cookie put back into the browser: still the sign-in page
    const { name, value, domain, path, httpOnly, sameSite, expires } = old;
    await page.browserContext().setCookie({ name, value, domain, path, httpOnly, sameSite, expires });
    await page.goto(base + "/", { waitUntil: "load" });
    ok("a copy of the old cookie does not sign in", atSignInPage(page));
    await signInWithToken();
    await layoutMounted();
    const fresh = await sessionCookieOf(page);
    ok("signing in again gives a new cookie that works", fresh.value !== old.value && (await statusWithCookie(base, fresh.value)) !== 401);
  }],

  ["Sign out everywhere: asks first, then a second browser is signed out too", async (ctx) => {
    const { base, browser, click, eq, ok, page, layoutMounted, signInWithToken, until } = ctx;
    const other = await browser.createBrowserContext();
    try {
      const second = await other.newPage();
      await signInWithToken(second);
      await second.waitForFunction(() => /^Live/.test(document.getElementById("wsText")?.textContent ?? ""), { timeout: 15000 });
      const theirs = await sessionCookieOf(second);
      const mine = await sessionCookieOf(page);
      ok("the second browser is in", (await second.evaluate(async () => (await fetch("/api/status")).status)) !== 401);

      await openSignOut(ctx);
      await click("#btnSignOutEverywhere");
      await until(() => !document.getElementById("signOutEverywhereConfirm").hidden);
      await click("[data-action=cancelSignOutEverywhere]");
      await until(() => document.getElementById("signOutEverywhereConfirm").hidden);
      ok("Cancel signs nobody out", (await statusWithCookie(base, theirs.value)) !== 401);
      await click("#btnSignOutEverywhere");
      await until(() => !document.getElementById("signOutEverywhereConfirm").hidden);
      await Promise.all([page.waitForNavigation({ waitUntil: "load" }), click("#btnSignOutEverywhereConfirm")]);
      ok(`this browser is at the sign-in page (${page.url()})`, atSignInPage(page));
      eq("this browser's old cookie", await statusWithCookie(base, mine.value), 401);

      await second.waitForFunction(() => !/^Live/.test(document.getElementById("wsText")?.textContent ?? ""), { timeout: 15000 });
      eq("the second browser's requests are refused", await second.evaluate(async () => (await fetch("/api/status")).status), 401);
      await second.goto(base + "/", { waitUntil: "load" });
      ok("the second browser is asked for the token again", atSignInPage(second));
    } finally {
      await other.close();
    }
    await signInWithToken(); // signing in again works at once
    await layoutMounted();
  }],

  ["a page whose sign-in ends elsewhere goes to the sign-in page by itself, once", async (ctx) => {
    const { browser, eq, expectRefusals, ok, page, signInWithToken, sleep, until } = ctx;
    await until(() => /^Live/.test(document.getElementById("wsText")?.textContent ?? ""));
    const other = await browser.createBrowserContext();
    expectRefusals(true); // this page's requests are refused from the moment the other browser signs out everywhere
    try {
      const second = await other.newPage();
      await signInWithToken(second);
      // the page's modules (the dialog's own among them) are loaded once the layout is mounted
      await second.waitForFunction(() => document.querySelector("[data-panel=report]")?.dataset.layoutHidden !== undefined, { timeout: 15000 });
      await second.waitForFunction(() => !document.getElementById("btnSignOut")?.hidden, { timeout: 15000 })
        .catch((e) => { throw new Error(`the second browser shows no Sign out (${second.url()}): ${e.message}`); });
      await second.evaluate(() => document.getElementById("btnSignOut").click());
      await second.waitForFunction(() => document.getElementById("signOutModal").classList.contains("active"), { timeout: 15000 });
      await second.evaluate(() => document.getElementById("btnSignOutEverywhere").click());
      await second.waitForFunction(() => !document.getElementById("signOutEverywhereConfirm").hidden, { timeout: 15000 });
      const left = page.waitForNavigation({ waitUntil: "load", timeout: 15000 });
      await Promise.all([second.waitForNavigation({ waitUntil: "load" }), second.evaluate(() => document.getElementById("btnSignOutEverywhereConfirm").click())]);
      await left;
      ok(`this page went to the sign-in page on its own (${page.url()})`, atSignInPage(page));
      let moves = 0;
      const count = (f) => { if (f === page.mainFrame()) moves++; };
      page.on("framenavigated", count);
      await sleep(3000);
      page.off("framenavigated", count);
      eq("and stayed there (no loop)", moves, 0);
      ok("still the sign-in page", atSignInPage(page));
    } finally {
      await other.close();
      expectRefusals(false);
    }
    // The last step: the page is left at the sign-in page (signing in again would be a sixth sign-in this minute,
    // over the sign-in page's limit of five per address).
  }],
];

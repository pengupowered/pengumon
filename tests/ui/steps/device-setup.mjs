// The device setup dialog (one for every device).

export const subject = "dialog:device-setup";
export const variants = ["full"];

export const steps = [
  ["one setup dialog for every device: line and router sign-ins post through the delegated submit", async ({ LINE, ROUTER, click, eq, page, until, waitText }) => {
    for (const card of [LINE, ROUTER]) {
      await click(`${card} [data-action=openDeviceSetup]`);
      eq("setup open", await page.$eval("#deviceSetupModal", (m) => m.classList.contains("active")), true);
      await page.$eval("#setupPassword", (i) => { i.value = ""; });
      await page.type("#setupPassword", "pw");
      await page.evaluate(() => document.getElementById("btnSaveSetup").click());
      await waitText("#setupResult", /Success/);
      await until(() => !document.getElementById("deviceSetupModal").classList.contains("active"));
    }
  }],

  ["device setup dialog: credential sources, refused and unreachable results, the password is never echoed", async ({ ROUTER, click, eq, ok, page, shots, text, until, waitText }) => {
    await click(`${ROUTER} [data-action=openDeviceSetup]`);
    await until(() => document.getElementById("deviceSetupModal").classList.contains("active"));
    ok("the title names the device and the subtitle says what it is", /^Set up Example Router$/.test(await text("#setupTitle")) && /router/i.test(await text("#setupMeta")) && !/_/.test(await text("#setupMeta")));
    ok("sources note", /PENGUMON_DEVICE_[A-Z0-9_]+_PASSWORD/.test(await text("#setupSource")) && /secret file/.test(await text("#setupSource")));
    eq("password field", await page.$eval("#setupPassword", (i) => i.type), "password");
    await click("#btnShowPassword");
    eq("shown", await page.$eval("#setupPassword", (i) => i.type), "text");
    await click("#btnShowPassword");
    // The server's answers to the credentials call, played by the page: a refused sign-in, a silent device, and a saved one
    // that an environment variable outranks. The real call (success) was exercised by the step before.
    const answer = (body, status = 200) => page.evaluate((b, st) => {
      window.__realFetch = window.__realFetch || window.fetch;
      window.fetch = (url, opts) => (opts && opts.method === "POST" && String(url).endsWith("/credentials")
        ? Promise.resolve(new Response(JSON.stringify(b), { status: st })) : window.__realFetch(url, opts));
    }, body, status);
    const submit = async () => {
      await page.$eval("#setupPassword", (i) => { i.value = ""; });
      await page.type("#setupPassword", "hunter2-not-echoed");
      await page.evaluate(() => document.getElementById("btnSaveSetup").click());
    };
    await answer({ success: false, error_kind: "auth", snapshot: { status: "error", error_message: "Main router rejected the sign-in (HTTP 401)." } });
    await submit();
    await waitText("#setupResult", /Sign-in refused/);
    ok("the password is not echoed", !(await text("#deviceSetupModal")).includes("hunter2-not-echoed"));
    await shots("device-setup");
    await answer({ success: false, error_kind: "timeout", snapshot: { status: "error", error_message: "No answer from 192.0.2.10 after 10 s." } });
    await submit();
    await waitText("#setupResult", /Device unreachable/);
    await answer({ detail: "Too many requests; try again in 40s." }, 429); // the credentials route allows 5 a minute
    await submit();
    await waitText("#setupResult", /Too many tries/);
    ok("a rate limit is not a refused password", !/refused/.test(await text("#setupResult")));
    await answer({ detail: "The secrets file (secrets.json) is damaged, so it was not overwritten and nothing was saved." }, 500);
    await submit();
    await waitText("#setupResult", /secrets file .* is damaged/);
    ok("a server refusal is shown as it is, not as a refused sign-in", !/refused/.test(await text("#setupResult")));
    await answer({ success: true, overridden_by: "environment PENGUMON_DEVICE_ROUTER_PASSWORD", snapshot: null });
    await submit();
    await waitText("#setupResult", /Success.*ranks above/s);
    await until(() => !document.getElementById("deviceSetupModal").classList.contains("active"));
    await page.evaluate(() => { window.fetch = window.__realFetch; });
    await click(`${ROUTER} [data-action=openDeviceSetup]`);
    eq("hidden and empty on reopening", await page.$eval("#setupPassword", (i) => `${i.type}:${i.value}`), "password:");
    await click("[data-action=closeModal][data-arg=deviceSetupModal]");
  }],

  ["device setup: the router's fields are not offered the dashboard's own sign-in, and a typed password does not stay in the closed dialog", async ({ ROUTER, click, eq, page, until }) => {
    await click(`${ROUTER} [data-action=openDeviceSetup]`);
    await until(() => document.getElementById("deviceSetupModal").classList.contains("active"));
    // A saved dashboard sign-in (same address) is offered to a text field + current-password pair; "new-password" and "off" are not
    eq("user name: no autofill", await page.$eval("#setupUsername", (i) => i.autocomplete), "off");
    eq("password: a new password, never the saved one", await page.$eval("#setupPassword", (i) => i.autocomplete), "new-password");
    eq("privacy password: the same", await page.$eval("#setupPrivacy", (i) => i.autocomplete), "new-password");
    eq("the password managers that ignore autocomplete are told", await page.$$eval("#setupUsername, #setupPassword, #setupPrivacy", (els) => els.every((i) => i.hasAttribute("data-1p-ignore") && i.dataset.lpignore === "true")), true);
    await page.type("#setupPassword", "typed-not-sent");
    await click("[data-action=closeModal][data-arg=deviceSetupModal]");
    await until(() => !document.getElementById("deviceSetupModal").classList.contains("active"));
    eq("the password is gone from the closed dialog", await page.$eval("#setupPassword", (i) => `${i.type}:${i.value}`), "password:");
  }],

  ["an SNMPv3 device asks for its user, authentication and privacy passwords; an SNMPv2c one for its community string only", async ({ ROUTER, click, eq, ok, page, phone, noSideScroll, shots, stubFetch, text, until, waitText }) => {
    const devices = await page.evaluate(async () => (await (await fetch("/api/devices")).json()));
    const as = (signIn) => devices.map((d) => (d.id === "router" ? { ...d, driver: "snmp", sign_in: signIn } : d));
    const V3 = { username: "SNMPv3 user name", password: "Authentication password", privacy_password: "Privacy password" };
    // the credentials call answered by the page, keeping what was sent
    const capture = () => page.evaluate(() => {
      window.__realFetch = window.__realFetch || window.fetch;
      window.__sent = null;
      window.fetch = (url, opts) => {
        if (opts && opts.method === "POST" && String(url).endsWith("/credentials")) {
          window.__sent = JSON.parse(opts.body);
          return Promise.resolve(new Response(JSON.stringify({ success: true, snapshot: null }), { status: 200 }));
        }
        return window.__realFetch(url, opts);
      };
    });
    const open = async () => {
      await click(`${ROUTER} [data-action=openDeviceSetup]`);
      await until(() => document.getElementById("deviceSetupModal").classList.contains("active"));
    };
    const fill = (id, value) => page.$eval(id, (i, v) => { i.value = v; }, value);

    await stubFetch({ "GET /api/devices": { body: as(V3) } });
    await open();
    const shownField = (id) => page.$eval(id, (f) => !f.hidden && f.getClientRects().length > 0);
    eq("the user name field", await shownField("#setupUsernameField"), true);
    eq("the privacy password field", await shownField("#setupPrivacyField"), true);
    eq("the labels", [await text("#setupUsernameLabel"), await text("#setupPasswordLabel"), await text("#setupPrivacyLabel")].join("|"),
      "SNMPv3 user name|Authentication password|Privacy password");
    eq("the privacy password is hidden and required", await page.$eval("#setupPrivacy", (i) => `${i.type}:${i.required}:${i.value}`), "password:true:");
    ok("the sources name the privacy password's variable and file", /PENGUMON_DEVICE_ROUTER_PRIVACY_PASSWORD/.test(await text("#setupSource")) && /pengumon_router_privacy_password/.test(await text("#setupSource")));
    await click("#btnShowPrivacy");
    eq("shown on request", await page.$eval("#setupPrivacy", (i) => i.type), "text");
    eq("the other one stays hidden", await page.$eval("#setupPassword", (i) => i.type), "password");
    await click("#btnShowPrivacy");
    await shots("device-setup-snmpv3");
    await capture();
    await fill("#setupUsername", "monitor");
    await fill("#setupPassword", "auth-pass-not-echoed");
    await fill("#setupPrivacy", "priv-pass-not-echoed");
    await page.evaluate(() => document.getElementById("btnSaveSetup").click());
    await waitText("#setupResult", /Success/);
    eq("what was sent", JSON.stringify(await page.evaluate(() => window.__sent)),
      JSON.stringify({ username: "monitor", password: "auth-pass-not-echoed", privacy_password: "priv-pass-not-echoed" }));
    ok("no password is shown", !/not-echoed/.test(await text("#deviceSetupModal")));
    await until(() => !document.getElementById("deviceSetupModal").classList.contains("active"));
    await open();
    eq("both passwords are empty on reopening", await page.$eval("#setupPassword", (i) => i.value) + await page.$eval("#setupPrivacy", (i) => i.value), "");
    await click("[data-action=closeModal][data-arg=deviceSetupModal]");
    await phone(async () => {
      await open();
      ok("phone width: no sideways scroll", await noSideScroll("deviceSetupModal"));
      await click("[data-action=closeModal][data-arg=deviceSetupModal]");
    });

    await stubFetch({ "GET /api/devices": { body: as({ username: null, password: "Community string", privacy_password: null }) } });
    await open();
    eq("SNMPv2c: no user name field", await shownField("#setupUsernameField"), false);
    eq("SNMPv2c: no privacy password field", await shownField("#setupPrivacyField"), false);
    eq("SNMPv2c: the password is the community string", await text("#setupPasswordLabel"), "Community string");
    ok("SNMPv2c: the sources name no privacy password", !/PRIVACY/.test(await text("#setupSource")));
    await capture();
    await fill("#setupPassword", "community-not-echoed");
    await page.evaluate(() => document.getElementById("btnSaveSetup").click());
    await waitText("#setupResult", /Success/);
    ok("SNMPv2c: no privacy password is sent", !("privacy_password" in await page.evaluate(() => window.__sent)));
    await until(() => !document.getElementById("deviceSetupModal").classList.contains("active"));
    await page.evaluate(() => { window.fetch = window.__realFetch; });
    await stubFetch(null);
    await open();
    eq("a device without sign_in keeps the usual labels", [await text("#setupUsernameLabel"), await text("#setupPasswordLabel")].join("|"), "Username|Password");
    eq("and no privacy password", await shownField("#setupPrivacyField"), false);
    await click("[data-action=closeModal][data-arg=deviceSetupModal]");
  }],

  ["a device that needs no sign-in (UPnP) has no Setup button on its card; the others keep theirs", async ({ LINE, ROUTER, eq, page, stubFetch, until }) => {
    const devices = await page.evaluate(async () => (await (await fetch("/api/devices")).json()));
    await stubFetch({ "GET /api/devices": { body: devices.map((d) => (d.id === "router" ? { ...d, needs_sign_in: false } : d)) } });
    await until((r) => document.querySelector(`${r} [data-action=openDeviceSetup]`), ROUTER);
    const shown = (card) => page.$eval(`${card} [data-action=openDeviceSetup]`, (b) => b.getClientRects().length > 0);
    eq("the router's Setup button", await shown(ROUTER), false);
    eq("the modem's Setup button", await shown(LINE), true);
    await stubFetch(null);
  }],
];

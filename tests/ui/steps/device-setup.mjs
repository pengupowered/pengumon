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
    ok("sources note", /PENGUMON_DEVICE_[A-Z0-9_]+_PASSWORD/.test(await text("#setupSource")) && /secret file/.test(await text("#setupSource")));
    eq("password field", await page.$eval("#setupPassword", (i) => i.type), "password");
    await click("#btnShowPassword");
    eq("shown", await page.$eval("#setupPassword", (i) => i.type), "text");
    await click("#btnShowPassword");
    // The server's answers to the credentials call, played by the page: a refused sign-in, a silent device, and a saved one
    // that an environment variable outranks. The real call (success) was exercised by the step before.
    const answer = (body) => page.evaluate((b) => {
      window.__realFetch = window.__realFetch || window.fetch;
      window.fetch = (url, opts) => (opts && opts.method === "POST" && String(url).endsWith("/credentials")
        ? Promise.resolve(new Response(JSON.stringify(b), { status: 200 })) : window.__realFetch(url, opts));
    }, body);
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
    await answer({ success: true, overridden_by: "environment PENGUMON_DEVICE_ROUTER_PASSWORD", snapshot: null });
    await submit();
    await waitText("#setupResult", /Success.*ranks above/s);
    await until(() => !document.getElementById("deviceSetupModal").classList.contains("active"));
    await page.evaluate(() => { window.fetch = window.__realFetch; });
    await click(`${ROUTER} [data-action=openDeviceSetup]`);
    eq("hidden and empty on reopening", await page.$eval("#setupPassword", (i) => `${i.type}:${i.value}`), "password:");
    await click("[data-action=closeModal][data-arg=deviceSetupModal]");
  }],
];

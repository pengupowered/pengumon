// The alerts dialog (notifications, sound, alert kinds kept per browser).

export const subject = "dialog:alerts";
export const variants = ["full"];

export const steps = [
  ["alerts modal: sound toggle and tones", async ({ click, eq, ok, page, text, until }) => {
    await click("#btnNotify");
    await until(() => document.getElementById("alertsModal").classList.contains("active"));
    ok("permission badge", (await text("#notifStatusBadge")).trim().length > 0);
    await click("[data-action=toggleSoundAlerts]");
    await until(() => document.getElementById("btnToggleSound").checked);
    eq("stored", await page.evaluate(() => localStorage.getItem("pengumon_sound")), "true");
    eq("switch follows", await page.$eval("#btnToggleSound", (i) => i.checked), true);
    await until(() => /Audio/.test(document.getElementById("btnNotify").title));
    for (const tone of ["down", "degraded", "healthy"]) await click(`[data-action=playAlertSound][data-arg=${tone}]`);
    await click("[data-action=toggleSoundAlerts]");
    await until(() => !document.getElementById("btnToggleSound").checked);
    await click("[data-action=closeModal][data-arg=alertsModal]");
  }],

  ["alerts dialog: notification state, sound switch, alert kinds kept per browser, reduced-motion safe", async ({ click, eq, ok, page, shots, until, waitText }) => {
    await click("#btnNotify");
    await until(() => document.getElementById("alertsModal").classList.contains("active"));
    ok("a switch for the sound", await page.$eval("#btnToggleSound", (i) => i.getAttribute("role") === "switch"));
    eq("three kinds, all on", await page.$$eval("#alertsModal .opts input", (b) => b.map((x) => x.checked).join()), "true,true,true");
    await click("#alertDegraded");
    eq("stored off", await page.evaluate(() => localStorage.getItem("pengumon_alert_degraded")), "false");
    await shots("alerts");
    await page.reload({ waitUntil: "load" });
    await waitText("#wsText", /^Live/);
    await click("#btnNotify");
    eq("restored after a reload", await page.$$eval("#alertsModal .opts input", (b) => b.map((x) => x.checked).join()), "true,false,true");
    await click("#alertDegraded");
    eq("stored on", await page.evaluate(() => localStorage.getItem("pengumon_alert_degraded")), "true");
    await page.keyboard.press("Escape");
    await until(() => !document.getElementById("alertsModal").classList.contains("active"));
  }],

  ["alerts dialog: a slow earlier answer about the server channels never replaces a newer one", async ({ click, eq, page, until }) => {
    // the first opening's request is answered only after the second one's (a busy server answers out of order)
    await page.evaluate(() => {
      const real = window.fetch;
      window.__realFetch = real;
      window.__answerFirst = null;
      let calls = 0;
      const json = (body) => new Response(JSON.stringify(body), { status: 200, headers: { "Content-Type": "application/json" } });
      window.fetch = (url, opts) => {
        if (url !== "/api/notifications") return real(url, opts);
        calls += 1;
        if (calls === 1) return new Promise((resolve) => { window.__answerFirst = () => resolve(json({ channels: [] })); });
        return Promise.resolve(json({ channels: [{ name: "ntfy", kind: "ntfy", format: null, host: "ntfy.example.com" }] }));
      };
    });
    await click("#btnNotify");
    await until(() => document.getElementById("alertsModal").classList.contains("active") && window.__answerFirst);
    await page.keyboard.press("Escape");
    await until(() => !document.getElementById("alertsModal").classList.contains("active"));
    await click("#btnNotify");
    await until(() => /Sent by the server/.test(document.getElementById("alChannelsText").textContent));
    await page.evaluate(() => window.__answerFirst());
    await page.evaluate(() => new Promise((r) => setTimeout(r, 300)));  // the late answer has arrived and been read
    eq("still the newer answer", await page.$eval("#alChannelsText", (e) => /Sent by the server/.test(e.textContent)), true);
    eq("one channel", await page.$$eval("#alChannels li", (l) => l.length), 1);
    await page.evaluate(() => { window.fetch = window.__realFetch; });
    await page.keyboard.press("Escape");
    await until(() => !document.getElementById("alertsModal").classList.contains("active"));
  }],

  ["alerts dialog: server channels (none configured, then ntfy and webhooks) and the test button", async ({ click, eq, ok, page, until }) => {
    await click("#btnNotify");
    await until(() => document.getElementById("alertsModal").classList.contains("active"));
    await until(() => /No ntfy topic or webhook/.test(document.getElementById("alChannelsText").textContent));
    ok("no test button without a channel", await page.$eval("#btnServerTest", (b) => b.hidden));
    await page.keyboard.press("Escape");
    await until(() => !document.getElementById("alertsModal").classList.contains("active"));

    // the same dialog with channels: the server's answers stand in, nothing is sent anywhere
    await page.evaluate(() => {
      const real = window.fetch;
      window.__realFetch = real;
      window.fetch = (url, opts) => {
        const json = (body) => Promise.resolve(new Response(JSON.stringify(body), { status: 200, headers: { "Content-Type": "application/json" } }));
        if (url === "/api/notifications") return json({ channels: [{ name: "ntfy", kind: "ntfy", format: null, host: "ntfy.example.com" }, { name: "<b>hook</b>", kind: "webhook", format: "discord", host: "discord.example.com" }] });
        if (url === "/api/notifications/test") return json({ channels: [{ name: "ntfy", ok: true, detail: null }, { name: "<b>hook</b>", ok: false, detail: "HTTP 404" }] });
        return real(url, opts);
      };
    });
    await click("#btnNotify");
    await page.waitForFunction(() => /Sent by the server/.test(document.getElementById("alChannelsText").textContent), { timeout: 15000, polling: 100 })
      .catch(async (e) => {  // say what the dialog shows instead (a CI run can only be read through this message)
        const seen = await page.evaluate(() => ({ text: document.getElementById("alChannelsText").textContent,
          open: document.getElementById("alertsModal").classList.contains("active"), mocked: window.fetch !== window.__realFetch,
          items: document.querySelectorAll("#alChannels li").length }));
        throw new Error(`${e.message}; the dialog shows ${JSON.stringify(seen)}`);
      });
    eq("two channels", await page.$$eval("#alChannels li", (l) => l.length), 2);
    eq("channels listed as text", await page.$$eval("#alChannels li", (l) => l.map((x) => x.textContent).join("|")), "ntfy ntfy, ntfy.example.com|<b>hook</b> webhook, discord, discord.example.com");
    eq("nothing injected", await page.$$eval("#alChannels b", (b) => b.length), 2);
    await click("#btnServerTest");
    await until(() => /failed/.test(document.getElementById("alServerResult").textContent));
    eq("each channel's answer", await page.$eval("#alServerResult", (e) => e.textContent), "ntfy: delivered; <b>hook</b>: failed (HTTP 404)");
    await page.evaluate(() => { window.fetch = window.__realFetch; });
    await page.keyboard.press("Escape");
    await until(() => !document.getElementById("alertsModal").classList.contains("active"));
  }],
];

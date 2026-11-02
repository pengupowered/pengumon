// The trace (MTR) dialog.

export const subject = "dialog:trace";
export const variants = ["full"];

export const steps = [
  ["trace dialog opens and closes", async ({ click, eq, page }) => {
    await click("[data-action=openModal][data-arg=tracerouteModal]");
    eq("trace open", await page.$eval("#tracerouteModal", (m) => m.classList.contains("active")), true);
    await click("[data-action=closeModal][data-arg=tracerouteModal]");
  }],

  ["trace dialog: a real empty state, a trace lists hops, Monitor adds a hop as a target, a failure is shown", async ({ click, count, eq, ok, page, shots, shown, text, until, waitCount }) => {
    // The trace and the add-target calls are answered by the page itself (no traceroute in the container, and adding a
    // target would change the row counts the later steps check); everything else goes to the server.
    await page.evaluate(() => {
      window.__posted = [];
      window.__realFetch = window.fetch;
      window.fetch = (url, opts) => {
        const body = opts && opts.body ? JSON.parse(opts.body) : null;
        if (opts && opts.method === "POST" && String(url).endsWith("/api/mtr/trace")) {
          window.__posted.push(["trace", body]);
          if (body.target_host === "bad.example") return Promise.resolve(new Response(JSON.stringify({ detail: "Traceroute failed" }), { status: 500 }));
          return Promise.resolve(new Response(JSON.stringify([
            { hop_number: 1, ip: "192.0.2.10", rtt_ms: 0.6, label: "Hop 1: Local Router (192.0.2.10)" },
            { hop_number: 2, ip: "198.51.100.1", rtt_ms: 9.6, label: "Hop 2: ISP Node / CMTS (198.51.100.1)" },
            { hop_number: 3, ip: null, rtt_ms: null, label: "Request Timed Out" },
            { hop_number: 4, ip: body.target_host, rtt_ms: 11.6, label: "Transit Router" }]), { status: 200 }));
        }
        if (opts && opts.method === "POST" && String(url).endsWith("/api/targets")) { window.__posted.push(["target", body]); return Promise.resolve(new Response("{}", { status: 200 })); }
        return window.__realFetch(url, opts);
      };
    });
    await click("[data-action=openModal][data-arg=tracerouteModal]");
    ok("empty state", await shown("#tracerouteResults .trace-empty"));
    await shots("trace-empty");
    await page.$eval("#traceTargetInput", (i) => { i.value = "203.0.113.53"; });
    await page.keyboard.press("Tab"); // out of the field; Enter in the field below submits the form
    await page.focus("#traceTargetInput");
    await page.keyboard.press("Enter");
    await waitCount("#tracerouteResults tbody tr", 4);
    eq("monitor buttons: not the hop that never replied", await count("#tracerouteResults button[data-action=monitorHop]"), 3);
    ok("no reply is marked", /no reply/.test(await text("#tracerouteResults tbody tr:nth-child(3)")));
    await shots("trace");
    await click("#tracerouteResults button[data-arg='2']");
    await until(() => /is a target now/.test(document.getElementById("traceNote")?.textContent ?? ""));
    const posted = await page.evaluate(() => window.__posted.find((p) => p[0] === "target")[1]);
    eq("target address", posted.address, "198.51.100.1");
    eq("target category", posted.category, "isp");
    ok("hop 2 is marked", /monitored/.test(await text("#tracerouteResults tbody tr:nth-child(2)")));
    eq("suggested stages: router, then the traced host as the internet", await page.$$eval("#tracerouteResults select", (els) => els.map((e) => e.value).join()), "local,public");
    await page.select("#traceStage1", "gateway");
    await click("#tracerouteResults button[data-arg='1']");
    await until(() => window.__posted.filter((p) => p[0] === "target").length === 2);
    eq("the stage picked is the category sent", await page.evaluate(() => window.__posted.filter((p) => p[0] === "target")[1][1].category), "gateway");
    await page.$eval("#traceTargetInput", (i) => { i.value = "bad.example"; });
    await click("#btnRunTrace");
    await until(() => /The trace failed/.test(document.getElementById("tracerouteResults").textContent));
    await page.evaluate(() => { window.fetch = window.__realFetch; });
    await click("[data-action=closeModal][data-arg=tracerouteModal]");
  }],
];

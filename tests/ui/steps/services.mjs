// The host services card.

export const subject = "panel:services";
export const variants = ["full"];

export const steps = [
  ["host services card: an up and a down service, the Open link, copy, history, and no service incidents in the network log", async ({ click, count, eq, ok, outDir, page, path, text, until, waitCount, waitText }) => {
    await until(() => !document.getElementById("servicesCard").hidden);
    await waitCount("#servicesList .svc-row", 2);
    const row = (id) => `#servicesList .svc-row[data-service=${id}]`;
    await waitText(`${row("svc_up")} [data-role=status]`, /^Up$/);
    await waitText(`${row("svc_down")} [data-role=status]`, /^Down$/);
    ok("up latency shown", /\d+ ms/.test(await text(`${row("svc_up")} .svc-ms-col`)));
    ok("clear reason", /connection refused \(stopped or restarting\)/.test(await text(`${row("svc_down")} .svc-reason`)));
    ok("checked ago", /checked \d+s ago/.test(await text(`${row("svc_up")} .svc-facts`)));
    const link = await page.$eval(`${row("svc_up")} a.svc-open`, (a) => ({ href: a.href, target: a.target, rel: a.rel, tag: a.tagName }));
    eq("open href", link.href, "http://services.example.test:9000/app");
    eq("open target", link.target, "_blank");
    ok("rel has noopener", link.rel.split(/\s+/).includes("noopener"));
    eq("no link without open_url", await count(`${row("svc_down")} a`), 0);
    ok("check url is text, not a link", /^127\.0\.0\.1:\d+\/api\/health$/.test(await text(`${row("svc_up")} .svc-check-url`)));
    await click(`${row("svc_up")} [data-action=copyServiceUrl]`);
    ok("clipboard", /^127\.0\.0\.1:\d+\/api\/health$/.test(await page.evaluate(() => window.__clip())));
    eq("history closed", await count(`${row("svc_down")} .svc-history`), 0);
    await click(`${row("svc_down")} [data-action=toggleServiceHistory]`);
    await waitText(`${row("svc_down")} .svc-history`, /seeded service outage/);
    eq("services never reach the network log", await page.evaluate(() => [...document.querySelectorAll("#incidentsBody tr")].some((tr) => /not responding|seeded service outage/.test(tr.textContent))), false);
    const api = await page.evaluate(async () => (await (await fetch("/api/incidents?limit=500")).json()).filter((i) => i.kind === "service_down").length);
    eq("API network list has no service incidents", api, 0);
    const all = await page.evaluate(async () => (await (await fetch("/api/incidents?limit=500&scope=service")).json()).length);
    ok("scope=service lists them", all >= 1);
    await page.screenshot({ path: path.join(outDir, "services.png") });
  }],
];

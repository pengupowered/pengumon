// The add-target dialog. It runs last: the first step adds a real target (a sixth row in the hop matrix for the rest of the run), which
// the steps of other files do not expect.

export const subject = "dialog:add-target";
export const variants = ["full"];
export const runsLast = true;

export const steps = [
  ["add-target form posts, closes and the new target gets a row", async ({ click, eq, page, showCard, until, waitCount }) => {
    await showCard("matrix"); // not drawn while it is off screen
    await click("[data-action=openModal][data-arg=addTargetModal]");
    await page.type("#targetId", "smoke_extra");
    await page.type("#targetName", "Smoke extra");
    await page.type("#targetAddress", "127.0.0.1");
    await page.evaluate(() => document.querySelector("#addTargetForm button[type=submit]").click());
    await until(() => !document.getElementById("addTargetModal").classList.contains("active"));
    eq("form reset", await page.$eval("#targetId", (i) => i.value), "");
    await waitCount("#hopMatrixBody tr", 6);
  }],

  ["add target dialog: validation messages next to the fields, id filled from the name, server refusal shown, Cancel clears", async ({ click, count, eq, modalOpen, ok, page, shots, text, until }) => {
    await click("[data-action=openModal][data-arg=addTargetModal]");
    await page.evaluate(() => document.querySelector("#addTargetForm button[type=submit]").click());
    await until(() => document.querySelectorAll("#addTargetForm .field.invalid").length === 3);
    ok("name message", /Give it a name/.test(await text("#targetNameMsg")));
    eq("focus on the first bad field", await page.evaluate(() => document.activeElement.id), "targetName");
    eq("invalid flag", await page.$eval("#targetName", (i) => i.getAttribute("aria-invalid")), "true");
    await page.type("#targetName", "Game server (east)");
    eq("id from the name", await page.$eval("#targetId", (i) => i.value), "game_server_east");
    await page.type("#targetAddress", "bad host");
    await page.evaluate(() => document.getElementById("targetAddress").dispatchEvent(new Event("change", { bubbles: true })));
    ok("address message", /no spaces/.test(await text("#targetAddressMsg")));
    await shots("add-target");
    await page.$eval("#targetAddress", (i) => { i.value = ""; });
    await page.type("#targetAddress", "203.0.113.10");
    eq("only valid fields left", await count("#addTargetForm .field.invalid"), 0);
    await page.$eval("#targetId", (i) => { i.value = ""; });
    await page.type("#targetId", "local_router"); // taken by the seeded config: the server says so (played by the page: a real 400 counts as a failed request)
    await page.evaluate(() => {
      window.__realFetch = window.fetch;
      window.fetch = (url, opts) => (opts && opts.method === "POST" && String(url).endsWith("/api/targets")
        ? Promise.resolve(new Response(JSON.stringify({ detail: "Target ID already exists" }), { status: 400 })) : window.__realFetch(url, opts));
    });
    await page.evaluate(() => document.querySelector("#addTargetForm button[type=submit]").click());
    await until(() => /taken/.test(document.getElementById("targetIdMsg").textContent));
    ok("still open", await modalOpen("addTargetModal"));
    await page.evaluate(() => { window.fetch = window.__realFetch; });
    await click("[data-action=cancelAddTarget]");
    await until(() => !document.getElementById("addTargetModal").classList.contains("active"));
    eq("cleared", await page.$eval("#targetName", (i) => i.value), "");
    eq("no stale messages", await count("#addTargetForm .field.invalid"), 0);
  }],
];

// Headless verification of build/armory.html: run the real page in jsdom, drive the
// replay, and assert the DOM faithfully renders whatever timeline it was built with.
//
// Expectations are DERIVED FROM THE EMBEDDED EVENTS, not hardcoded. An earlier version
// pinned the scripted demo's specifics — 6 utterances, a 4.2s forge, an event at t=10 —
// and every one of those broke the moment the page was built from a real session, even
// though the page was rendering it correctly. A harness that only passes for one fixture
// is testing the fixture.
//
//   npm i jsdom && node scripts/verify_armory.js

const fs = require("fs");
const path = require("path");
const { JSDOM, VirtualConsole } = require("jsdom");

const PAGE = path.resolve(__dirname, "..", "build", "armory.html");
const html = fs.readFileSync(PAGE, "utf8");

const errors = [];
const dom = new JSDOM(`<!doctype html><html><head></head><body>${html}</body></html>`, {
  runScripts: "dangerously",
  pretendToBeVisual: true,
  virtualConsole: new VirtualConsole()
    .on("jsdomError", (e) => errors.push("jsdomError: " + e.message))
    .on("error", (m) => errors.push("console.error: " + m)),
  // Stub only what jsdom lacks — a canvas backend. matchMedia is deliberately left
  // absent: the page must survive without it, which is what its guard is for.
  beforeParse(win) {
    win.HTMLCanvasElement.prototype.getContext = () => ({
      setTransform() {}, clearRect() {}, fillRect() {}, globalAlpha: 1, fillStyle: "",
    });
  },
});

const { window } = dom;
const doc = window.document;

let failures = 0;
const t = (name, fn) => {
  try { fn(); console.log(`  ok   ${name}`); }
  catch (e) { console.log(`  FAIL ${name}\n         ${e.message}`); failures++; }
};
const assert = (cond, msg) => { if (!cond) throw new Error(msg); };
const text = (sel) => (doc.querySelector(sel)?.textContent || "").trim();

setTimeout(() => {
  const events = JSON.parse(doc.getElementById("timeline").textContent);
  const of = (type) => events.filter((e) => e.type === type);

  // The anvil shows one forge cycle — the most recent — not every attempt in the
  // session. A session can forge twice (the injection beat is its own cycle), so
  // counting forge_code events globally overstates what the panel should display.
  const cycleOf = (e) => e.forge_id || e.skill || "";
  const lastCycle = cycleOf(of("forge_code").slice(-1)[0] || {});
  const inLastCycle = (type) => of(type).filter((e) => cycleOf(e) === lastCycle);

  const expect = {
    transcripts: of("transcript").length,
    acted: of("action").length,
    denied: of("action_denied").length,
    attempts: inLastCycle("forge_code").length,
    failures: inLastCycle("temper_failed").length,
    totalAttempts: of("forge_code").length,
    totalFailures: of("temper_failed").length,
    registered: of("skill_registered")[0],
    end: events[events.length - 1].at,
  };
  expect.cards = expect.acted + expect.denied;

  console.log(`\n— page built from ${events.length} events —`);
  console.log(`  ${expect.transcripts} spoken · ${expect.acted} acted · ` +
              `${expect.denied} denied · ${expect.totalAttempts} generation attempts ` +
              `(${expect.attempts} in the visible cycle)\n`);

  t("no script errors on load", () => assert(!errors.length, errors.join(" | ")));

  t("all getElementById targets exist", () => {
    const ids = [...html.matchAll(/\$\("([a-z0-9-]+)"\)/g)].map((m) => m[1]);
    const missing = [...new Set(ids)].filter((id) => !doc.getElementById(id));
    assert(!missing.length, "missing ids: " + missing.join(", "));
  });

  // Drive to a point in time deterministically, without waiting on rAF.
  const scrub = doc.getElementById("scrub");
  const span = expect.end + 2.5;
  const seek = (seconds) => {
    scrub.value = String(Math.max(0, Math.min(100, (seconds / span) * 100)));
    scrub.dispatchEvent(new window.Event("input"));
  };
  seek(span);

  console.log("— after scrubbing to the end —");

  t("every spoken line reaches the ticker", () => {
    const said = doc.querySelectorAll(".said");
    assert(said.length === expect.transcripts,
           `${said.length} rendered, ${expect.transcripts} in the timeline`);
  });

  t("ignored chatter is shown but unmarked", () => {
    if (!expect.transcripts) return;
    const fired = doc.querySelectorAll(".said.fired").length;
    assert(fired < expect.transcripts,
           "every line marked as firing — the quiet ones are the point");
  });

  t("one card per action, acted or denied", () => {
    const cards = doc.querySelectorAll(".card");
    assert(cards.length === expect.cards,
           `${cards.length} cards, expected ${expect.cards}`);
  });

  t("denials carry the OUTSIDE YOUR MARK stamp", () => {
    const stamps = doc.querySelectorAll(".stamp");
    assert(stamps.length === expect.denied,
           `${stamps.length} stamps, ${expect.denied} denials`);
    if (expect.denied) assert(/Outside your mark/i.test(stamps[0].textContent));
  });

  t("a denial names the primitive that stopped it", () => {
    const withPrimitive = of("action_denied").filter((e) => e.primitive);
    if (!withPrimitive.length) return;
    const body = doc.querySelector("#feed").textContent;
    assert(withPrimitive.some((e) => body.includes(e.primitive)),
           "no blocking primitive shown on any denial card");
  });

  t("actions are attributed to whoever ran them", () => {
    const feed = doc.querySelector("#feed").textContent;
    for (const e of [...of("action"), ...of("action_denied")]) {
      assert(feed.includes(e.actor), `${e.actor} missing from the feed`);
    }
  });

  t("observed state is reported, not intent", () => {
    if (!of("action").some((e) => e.observed)) return;
    const dl = doc.querySelector(".observed");
    assert(dl && dl.textContent.trim(), "no observed state rendered");
  });

  t("the armory wall reflects the registered skill", () => {
    if (!expect.registered) return;
    const badge = doc.querySelector(".badge");
    assert(badge, "no badge rendered");
    assert(badge.textContent.includes(expect.registered.skill),
           `badge does not name ${expect.registered.skill}`);
    // State is written as well as coloured — identity is never colour-alone.
    assert(/hot|tempered|trusted/i.test(badge.textContent),
           "trust state not rendered as text");
  });

  t("north-star tile counts this session's actions", () => {
    const cells = [...doc.querySelectorAll(".ns-cell")];
    assert(cells.length === 3, `got ${cells.length} cells`);
    const actions = cells.find((c) => /Actions this session/.test(c.textContent));
    assert(actions.querySelector("b").textContent === String(expect.cards),
           `tile says ${actions.querySelector("b").textContent}, expected ${expect.cards}`);
    const zero = cells.find((c) => /Human-written/.test(c.textContent));
    assert(zero.querySelector("b").textContent === "0");
  });

  t("instruments derive from the session", () => {
    assert(text("#denial-count") === String(expect.denied),
           `denials ${text("#denial-count")}, expected ${expect.denied}`);

    // Forge success is a *tempering* rate, not a generation rate — a cycle refused
    // before it ever tempered (the injection beat) is neither a pass nor a failure, and
    // counting it as either would flatter or slander the forge. Re-derived here from the
    // log independently of the page's running state, so the two must agree.
    const trust = new Map();
    let tempered = 0;
    for (const e of events) {
      if (e.type === "skill_registered") trust.set(e.skill, e.trust);
      if (e.type === "skill_trust") {
        if (trust.get(e.skill) === "quarantined" && e.trust !== "quarantined") tempered++;
        trust.set(e.skill, e.trust);
      }
    }
    const runs = tempered + expect.totalFailures;
    if (runs) {
      const pct = `${Math.round((tempered / runs) * 100)}%`;
      assert(text("#heat-val") === pct,
             `forge success ${text("#heat-val")}, expected ${pct} from ` +
             `${tempered} tempered / ${runs} temper runs`);
    }
  });

  t("audit-style table has a row per action", () => {
    const rows = doc.querySelectorAll("#audit-body tr");
    assert(rows.length === expect.cards, `${rows.length} rows, expected ${expect.cards}`);
  });

  // --- mid-forge, located in the data rather than guessed ------------------

  if (expect.failures > 0 && expect.attempts > 1) {
    const failure = of("temper_failed")[0];
    const secondAttempt = of("forge_code")[1].at;

    console.log("\n— mid-forge, at the moment of the retry —");

    // Scrubbing can only reach a state that occupies its own instant. Against the
    // in-memory adapter a whole session runs in tens of milliseconds, so the failure and
    // the regeneration that answers it can land on the same timestamp — there is no
    // moment at which only the first has happened. That is a property of the session,
    // not a defect in the page, so say so rather than failing.
    if (secondAttempt <= failure.at) {
      console.log(`  skip the failure/retry states are indistinguishable in time ` +
                  `(both at ${failure.at}s — a sub-second session)`);
    } else {

    t("the failure is reported while it is happening", () => {
      seek(failure.at);
      const note = doc.querySelector(".fail-note");
      assert(note && /^Temper failed\./.test(note.textContent),
             note ? note.textContent : "no failure note");
      const stage = doc.querySelector('.stage[data-state="failed"]');
      assert(stage && stage.textContent === "tempering", "tempering not marked failed");
    });

    t("the reason survives regeneration", () => {
      seek(secondAttempt);
      const note = doc.querySelector(".fail-note");
      assert(note, "failure note vanished once attempt 2 arrived");
      assert(/Regenerated after attempt 1 failed/.test(note.textContent),
             note.textContent);
    });

    t("attempt tabs and a working diff are offered", () => {
      const tabs = [...doc.querySelectorAll(".attempt-tab")];
      assert(tabs.length === expect.attempts + 1,
             `${tabs.length} tabs for ${expect.attempts} attempts + diff`);
      tabs.find((b) => b.textContent === "diff").click();
      assert(doc.querySelectorAll(".ln.add").length > 0, "diff shows no additions");
      assert(doc.querySelectorAll(".ln.del").length > 0, "diff shows no deletions");
    });

    }

    // Independent of timing: by the end, both attempts and the reason must be there.
    t("both attempts and the failure reason survive to the end", () => {
      seek(span);
      const tabs = [...doc.querySelectorAll(".attempt-tab")];
      assert(tabs.length === expect.attempts + 1,
             `${tabs.length} tabs for ${expect.attempts} attempts + diff`);
      const note = doc.querySelector(".fail-note");
      assert(note && /Regenerated after attempt 1 failed/.test(note.textContent),
             note ? note.textContent : "no failure note at end of session");
    });
  }

  t("generated code is rendered as the focal element", () => {
    seek(span);
    assert(doc.querySelectorAll("pre.code .ln").length >= 3,
           "no generated code on screen");
  });

  t("no errors accumulated during the whole replay", () => {
    assert(!errors.length, errors.join(" | "));
  });

  console.log(failures ? `\n${failures} failed\n` : "\nall checks passed\n");
  dom.window.close();
  process.exit(failures ? 1 : 0);
}, 300);

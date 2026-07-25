// Headless verification of build/consult.html: run the real page in jsdom and assert the
// DOM faithfully renders whatever practice payload it was built with.
//
// Two rules this harness follows, both learned the hard way:
//
// 1. **Expectations come from the embedded payload, never hardcoded.** The Armory harness
//    pinned its fixture's specifics — 6 utterances, a forge at t=10 — and every one broke
//    the moment the page was built from a real session it was rendering correctly. A
//    harness that only passes for one fixture is testing the fixture.
//
// 2. **"No script errors" is not enough on its own.** A missing comma once turned a call
//    into a tagged template, killed the entire script, and the page rendered nothing while
//    an error check passed. So every check below asserts something is actually *on screen*.
//
// The page is run twice, because /me is a second code path: once signed out (fetch
// rejects — the state when the file is opened straight off disk) and once signed in.
//
// Verified by mutation: dropping the sign-out reveal, blanking a refusal's bound subject,
// and reintroducing the missing comma each make it fail.
//
//   npm i jsdom && node scripts/verify_consult.js
//
// This repo has no node_modules of its own; jsdom is borrowed from a sibling checkout:
//
//   NODE_PATH=../QuickSwap/frontend/node_modules node scripts/verify_consult.js

const fs = require("fs");
const path = require("path");
const { JSDOM, VirtualConsole } = require("jsdom");

const PAGE = path.resolve(__dirname, "..", "build", "consult.html");
const html = fs.readFileSync(PAGE, "utf8");

const ME = {
  identifier: "priya.rao@clinic.test",
  email: "priya.rao@clinic.test",
  name: "Dr Priya Rao",
  initials: "DP",
};

let failures = 0;
const t = (name, fn) => {
  try { fn(); console.log(`  ok   ${name}`); }
  catch (e) { console.log(`  FAIL ${name}\n         ${e.message}`); failures++; }
};
const assert = (cond, msg) => { if (!cond) throw new Error(msg); };

// --- a page instance -------------------------------------------------------

function render({ me }) {
  const errors = [];
  const dom = new JSDOM(
    `<!doctype html><html><head></head><body>${html}</body></html>`, {
      runScripts: "dangerously",
      pretendToBeVisual: true,
      virtualConsole: new VirtualConsole()
        .on("jsdomError", (e) => errors.push("jsdomError: " + e.message))
        .on("error", (m) => errors.push("console.error: " + m)),
      beforeParse(win) {
        // The only stub. matchMedia is deliberately left absent — the page must survive
        // without it, which is what its guard is for.
        win.fetch = (url) => {
          if (!String(url).endsWith("/me")) return Promise.reject(new Error("not stubbed"));
          if (!me) return Promise.reject(new TypeError("Failed to fetch"));
          return Promise.resolve({
            ok: true, status: 200, json: () => Promise.resolve(me),
          });
        };
      },
    });
  return { dom, doc: dom.window.document, window: dom.window, errors };
}

const settled = () => new Promise((r) => setTimeout(r, 120));

// --- checks ----------------------------------------------------------------

async function main() {
  const page = render({ me: null });
  const { doc, window, errors } = page;
  await settled();

  const DATA = JSON.parse(doc.getElementById("practice").textContent);
  const EVENTS = DATA.consultation;
  const of = (type) => EVENTS.filter((e) => e.type === type);
  const one = (type) => of(type)[0];

  const bound = one("patient_bound");
  const ended = one("consultation_ended");
  const written = of("action_written");
  const refused = of("action_refused");
  const text = (sel) => (doc.querySelector(sel)?.textContent || "").trim();

  console.log(`\n— page built from ${EVENTS.length} events, ` +
              `${DATA.patients.length} patients, ${DATA.schedule.length} appointments —`);
  console.log(`  ${of("said").length} spoken · ${of("note").length} note sections · ` +
              `${written.length} written · ${refused.length} refused\n`);

  console.log("— signed out (no server, or /me unreachable) —");

  t("no script errors on load", () => assert(!errors.length, errors.join(" | ")));

  t("every getElementById target exists", () => {
    const ids = [...html.matchAll(/\$\("([a-z0-9-]+)"\)/g)].map((m) => m[1]);
    const missing = [...new Set(ids)].filter((id) => !doc.getElementById(id));
    assert(!missing.length, "missing ids: " + missing.join(", "));
  });

  t("the header falls back to the baked-in clinician", () => {
    assert(text("#who b") === DATA.clinician.name,
           `header says "${text("#who b")}"`);
    assert(text("#avatar") === DATA.clinician.initials);
    assert(text("#who span").includes(DATA.today), "today's date missing from the header");
  });

  t("sign-out is offered even with no server behind the page", () => {
    // Regression. It used to hide itself until /me answered, so the published build —
    // a static file, no server — showed no way to sign out and looked like the feature
    // did not exist rather than being unreachable.
    assert(!doc.getElementById("signout").hidden, "no sign-out control");
    assert(!doc.getElementById("preview-chip").hidden,
           "no Preview chip to explain why this build cannot end a real session");
  });

  t("signing out in preview shows the flow and admits it is a preview", () => {
    doc.getElementById("signout").dispatchEvent(
      new window.MouseEvent("click", { bubbles: true, cancelable: true }));
    const veil = doc.querySelector(".signed-out");
    assert(veil, "sign-out did nothing");
    assert(/signed out/i.test(veil.textContent));
    // Claiming a server session was destroyed when there is no server would make the
    // demo a lie about the thing it is demonstrating.
    assert(/no server session to end/i.test(veil.textContent),
           "the preview claims to have ended a session it never had");
    veil.querySelector("button").click();
    assert(!doc.querySelector(".signed-out"), "cannot get back in");
  });

  // --- the consultation, which is the view the page opens on ---------------

  console.log("\n— the consultation —");

  t("the page opens on whatever is waiting for the doctor", () => {
    // Approval, because something is sitting there unsent. If the draft were gone the
    // consultation would be the right landing view instead.
    assert(doc.getElementById("view-approve").dataset.active === "true");
    assert(doc.getElementById("view-today").dataset.active === "false");
  });

  t("the consultation view renders", () => {
    doc.getElementById("tab-consult").click();
    assert(doc.getElementById("view-consult").dataset.active === "true");
  });

  t("the sticky patient bar names the bound patient", () => {
    const bar = doc.querySelector(".patient-bar");
    assert(bar, "no patient bar — the wrong-patient guard is not on screen");
    assert(bar.querySelector("h2").textContent === bound.patient.name,
           `bar says "${bar.querySelector("h2").textContent}", bound to ` +
           `${bound.patient.name}`);
    for (const v of [bound.patient.id, bound.patient.nhs]) {
      assert(bar.textContent.includes(v), `${v} missing from the patient bar`);
    }
  });

  t("the binding says where the identity came from", () => {
    const tag = doc.querySelector(".patient-bar .bound");
    assert(tag, "no binding tag");
    assert(tag.title === bound.via, `binding provenance missing (title="${tag.title}")`);
  });

  t("no other patient's name appears in the patient bar", () => {
    const bar = doc.querySelector(".patient-bar").textContent;
    const others = DATA.patients.filter((p) => p.id !== bound.patient.id)
                                .filter((p) => bar.includes(p.name));
    assert(!others.length, "patient bar also names " + others.map((p) => p.name).join(", "));
  });

  t("every spoken line reaches the transcript", () => {
    const said = doc.querySelectorAll("#view-consult .said");
    assert(said.length === of("said").length,
           `${said.length} rendered, ${of("said").length} in the log`);
  });

  t("both sides of the conversation are attributed", () => {
    const whos = new Set([...doc.querySelectorAll("#view-consult .said")]
      .map((n) => n.dataset.who));
    const expected = new Set(of("said").map((e) => e.who));
    assert(whos.size === expected.size, `${[...whos]} vs ${[...expected]}`);
  });

  t("every note section is captured", () => {
    const sections = [...doc.querySelectorAll(".note-section h4")].map((n) => n.textContent);
    const expected = of("note").map((e) => e.section);
    assert(sections.join("|") === expected.join("|"),
           `rendered ${sections.join(", ")}; log has ${expected.join(", ")}`);
  });

  t("one action card per write and per refusal", () => {
    const cards = doc.querySelectorAll(".action");
    assert(cards.length === written.length + refused.length,
           `${cards.length} cards, expected ${written.length + refused.length}`);
    assert(doc.querySelectorAll('.action[data-state="refused"]').length === refused.length);
    assert(doc.querySelectorAll('.action[data-state="written"]').length === written.length);
  });

  t("a write reports what was observed, not what was intended", () => {
    for (const a of written) {
      for (const o of a.observed) {
        assert(doc.querySelector("#view-consult").textContent.includes(o.what),
               `"${o.what}" never rendered`);
        if (o.detail) {
          assert(doc.querySelector("#view-consult").textContent.includes(o.detail),
                 `record id "${o.detail}" not shown — read-back is the claim`);
        }
      }
      assert(doc.querySelector(".confirmed").textContent.includes(a.confirmed_by),
             "no human named as confirming the write");
    }
  });

  // The governance beat. If this renders wrongly the demo's strongest moment is lost.
  t("a refusal contrasts the bound subject with the attempted one", () => {
    for (const r of refused) {
      const card = [...doc.querySelectorAll('.action[data-state="refused"]')]
        .find((c) => c.textContent.includes(r.attempted_subject));
      assert(card, `no refusal card names the attempted subject ${r.attempted_subject}`);
      assert(card.textContent.includes(r.bound_subject),
             "the refusal does not say what it is bound to, so the contrast is lost");
      assert(card.querySelector(".said-back").textContent === r.says,
             "the bot's spoken refusal is not shown");
      if (r.note) {
        assert(card.textContent.includes(r.note),
               "the 'permission is not relevance' note is missing");
      }
    }
  });

  t("the refusal is not styled as a completed action", () => {
    for (const c of doc.querySelectorAll('.action[data-state="refused"]')) {
      assert(/refused/i.test(c.querySelector(".state").textContent),
             `refusal labelled "${c.querySelector(".state").textContent}"`);
    }
  });

  t("the summary tallies match the log", () => {
    if (!ended) return;
    const tally = (kind) => text(`.tally[data-kind="${kind}"] b`);
    for (const [kind, expected] of [["written", ended.written], ["refused", ended.refused],
                                    ["reads", ended.reads],
                                    ["length", ended.duration_label]]) {
      assert(tally(kind) === String(expected),
             `${kind} tile says "${tally(kind)}", log says "${expected}"`);
    }
    assert(text(".card .prose").length > 0, "no summary prose");
  });

  t("the access log has a row per read and per written record", () => {
    const expected = of("accessed").length +
                     written.reduce((n, a) => n + a.observed.length, 0);
    const rows = doc.querySelectorAll("table.access tbody tr");
    assert(rows.length === expected, `${rows.length} rows, expected ${expected}`);
    const effects = new Set([...rows].map((r) => r.lastElementChild.textContent));
    assert(effects.has("write"), "no write recorded in the access log");
  });

  // --- the approval gate ---------------------------------------------------
  //
  // The property under test is not "a screen renders". It is that nothing can reach a
  // patient without a human act, that every claim is traceable, and that an untraceable
  // one is surfaced rather than buried.

  console.log("\n— the approval gate —");

  const draft = one("draft_ready");
  const letter = draft.letter;
  const allBlocks = [...letter.paragraphs, ...letter.todos, letter.closing];
  const claimed = (b) => !b.sources.length && b.kind !== "courtesy";
  const approve = () => doc.getElementById("tab-approve").click();
  const claimFor = (id) => doc.querySelector(`.claim[data-block="${id}"]`);

  t("the letter is a different document from the clinical note", () => {
    approve();
    const shown = doc.getElementById("view-approve").textContent;
    for (const n of of("note")) {
      assert(!shown.includes(n.text),
             `clinical note text "${n.section}" is in the patient's letter`);
    }
    assert(shown.includes(letter.paragraphs[1].text.slice(0, 40)),
           "the patient letter is not rendered");
  });

  t("every drafted block is on screen and clickable", () => {
    for (const b of allBlocks) {
      assert(claimFor(b.id), `block ${b.id} is missing from the letter`);
    }
  });

  t("a claim shows the exact utterance it came from", () => {
    const sourced = allBlocks.find((b) => b.sources.length);
    claimFor(sourced.id).click();
    const panel = doc.querySelector(".card .body").parentElement.textContent;
    const shown = doc.getElementById("view-approve").textContent;
    for (const id of sourced.sources) {
      const said = EVENTS.find((e) => e.type === "said" && e.id === id);
      assert(said, `source ${id} is not an utterance`);
      assert(shown.includes(said.text.slice(0, 40)),
             `provenance for ${sourced.id} does not show utterance ${id}`);
    }
  });

  t("provenance points at the patient's own words, not a paraphrase", () => {
    const p = letter.paragraphs.find((x) => x.sources.includes("u2"));
    claimFor(p.id).click();
    const said = EVENTS.find((e) => e.id === "u2");
    assert(said.who === "patient", "fixture drift: u2 is no longer the patient speaking");
    assert(doc.querySelector('.prov-said[data-who="patient"]'),
           "the patient's line is not attributed to the patient");
  });

  // The reason this screen exists. A sentence the bot wrote with nothing behind it must
  // be impossible to miss, not merely absent from the provenance panel.
  t("an unsupported claim is flagged in the letter itself", () => {
    const bad = allBlocks.filter(claimed);
    assert(bad.length, "fixture has no unsupported claim — the flag is untested");
    for (const b of bad) {
      assert(claimFor(b.id).dataset.unsourced === "true",
             `${b.id} makes a claim with no source and is not flagged`);
      assert(/not in the transcript/i.test(claimFor(b.id).textContent),
             `${b.id} carries no visible warning`);
    }
  });

  t("courtesy text is not flagged, so the warning keeps its meaning", () => {
    const polite = allBlocks.filter((b) => b.kind === "courtesy");
    assert(polite.length, "fixture has no courtesy block");
    for (const b of polite) {
      assert(claimFor(b.id).dataset.unsourced === "false",
             `"thank you for coming in" is flagged; a warning that fires on pleasantries ` +
             `is one a reviewer learns to ignore`);
    }
  });

  t("the count of unsupported claims is stated before approving", () => {
    const n = allBlocks.filter(claimed).length;
    assert(doc.getElementById("view-approve").textContent
             .includes(`${n} statement`), `the "${n} statements" warning is missing`);
  });

  t("approval is impossible until a human confirms they read it", () => {
    const btn = [...doc.querySelectorAll(".btn")].find((b) => /Approve/.test(b.textContent));
    assert(btn, "no approve button");
    assert(btn.disabled, "the letter can be sent without anyone confirming they read it");
  });

  t("the recipient is the address on the record, shown before sending", () => {
    const shown = doc.getElementById("view-approve").textContent;
    assert(shown.includes(draft.recipient.email), "recipient address not shown");
    assert(shown.includes(draft.recipient.verified_against),
           "the record the address came from is not shown");
  });

  t("the patient letter carries a not-monitored footer", () => {
    const shown = doc.getElementById("view-approve").textContent;
    assert(/not monitored/i.test(shown), "no unmonitored-mailbox notice");
    assert(shown.includes(DATA.surgery.phone), "no number to call instead");
    assert(/999|emergency/i.test(shown), "no emergency route");
  });

  t("editing a claim records the change without losing the original", () => {
    const target = allBlocks.find((b) => claimed(b));
    claimFor(target.id).click();          // select
    claimFor(target.id).click();          // second click opens the editor
    const area = claimFor(target.id).querySelector("textarea");
    assert(area, "a flagged claim cannot be edited — a reviewer who can only accept or " +
                 "reject will rubber-stamp");
    area.value = "Removed pending review.";
    area.dispatchEvent(new window.Event("blur"));
    assert(/Removed pending review/.test(claimFor(target.id).textContent));
    assert(/edited/i.test(claimFor(target.id).textContent), "the edit is not marked");
  });

  t("approval then holds, with a working cancel", () => {
    const box = doc.querySelector(".confirm input");
    box.checked = true;
    box.dispatchEvent(new window.Event("change"));

    const btn = [...doc.querySelectorAll(".btn")].find((b) => /Approve/.test(b.textContent));
    assert(!btn.disabled, "confirming did not enable approval");
    btn.click();

    const hold = doc.querySelector(".holding .count");
    assert(hold, "sent immediately — email cannot be unsent, so the hold is the only undo");
    assert(hold.textContent === `${draft.hold_seconds}s`,
           `countdown starts at ${hold.textContent}, not ${draft.hold_seconds}s`);

    const cancel = [...doc.querySelectorAll(".btn")].find((b) => /Cancel/.test(b.textContent));
    assert(cancel, "no way to stop it");
    cancel.click();
    assert(!doc.querySelector(".holding"), "cancel did not stop the send");
    assert([...doc.querySelectorAll(".btn")].some((b) => /Approve/.test(b.textContent)),
           "cancelling did not return to the draft");
  });

  t("nothing claims to have been sent while it is still a draft", () => {
    const shown = doc.getElementById("view-approve").textContent;
    assert(!/^Sent to/m.test(shown), "the UI says it sent something it did not send");
  });

  // --- the other two views -------------------------------------------------

  console.log("\n— the other views —");

  const clickTab = (id) => doc.getElementById(`tab-${id}`).click();

  t("Today lists every appointment with its patient", () => {
    clickTab("today");
    const rows = doc.querySelectorAll("#view-today .row-item");
    assert(rows.length === DATA.schedule.length,
           `${rows.length} rows, ${DATA.schedule.length} appointments`);
    const body = doc.getElementById("view-today").textContent;
    for (const slot of DATA.schedule) {
      const p = DATA.patients.find((x) => x.id === slot.patient);
      assert(body.includes(slot.time), `${slot.time} missing`);
      if (p) assert(body.includes(p.name), `${p.name} missing from the clinic list`);
    }
  });

  t("Patients shows the record and history for a patient with a CRM id", () => {
    clickTab("patients");
    const body = doc.getElementById("view-patients").textContent;
    for (const m of DATA.context.medications) {
      assert(body.includes(m.drug), `${m.drug} not shown`);
      assert(body.includes(m.dose), `${m.drug} has no dose`);
    }
    for (const a of DATA.context.allergies || []) {
      assert(body.includes(a), `allergy "${a}" not shown`);
    }
    assert(doc.querySelectorAll("#view-patients .entry").length === DATA.history.length,
           "history entries missing");
  });

  t("a patient with no CRM record gets the new-patient state, not an empty record", () => {
    const fresh = DATA.patients.find((p) => !p.crm_id);
    if (!fresh) { console.log("         (no new patient in this payload)"); return; }
    const row = [...doc.querySelectorAll("#view-patients .row-item")]
      .find((r) => r.textContent.includes(fresh.name));
    assert(row, `${fresh.name} not in the roster`);
    row.click();

    const body = doc.getElementById("view-patients").textContent;
    assert(!doc.querySelector("#view-patients .dl"),
           "a record card was rendered for a patient with no record");
    assert(/New patient/i.test(body), "no new-patient state");
    assert(/confirmation/i.test(body),
           "creating a record is a write and must be described as needing confirmation");
  });

  t("switching views leaves exactly one visible", () => {
    for (const id of ["today", "patients", "consult"]) {
      clickTab(id);
      const active = ["today", "patients", "consult"]
        .filter((v) => doc.getElementById(`view-${v}`).dataset.active === "true");
      assert(active.length === 1 && active[0] === id,
             `after ${id}: active = ${active.join(", ") || "none"}`);
    }
  });

  // Regression: the palette was white and blue, but a prefers-color-scheme block flipped
  // the whole page dark on any machine set to dark mode — so the intended palette was
  // never what the doctor actually saw, and the page looked correct to anyone testing in
  // light mode. jsdom does not evaluate media queries, so this asserts on the source.
  t("the page cannot turn itself dark", () => {
    assert(!/prefers-color-scheme/.test(html.replace(/\/\*[\s\S]*?\*\//g, "")),
           "a prefers-color-scheme rule is back; the page will go dark on a dark laptop");
    assert(/color-scheme:\s*light/.test(html),
           "no color-scheme: light — form controls and scrollbars will still go dark");
  });

  t("the ground and panels are actually white and blue", () => {
    const tokens = Object.fromEntries(
      [...html.matchAll(/--(ground|panel|ink|accent):\s*(#[0-9a-f]{6})/gi)]
        .map((m) => [m[1].toLowerCase(), m[2].toLowerCase()]));
    assert(tokens.panel === "#ffffff", `panels are ${tokens.panel}, not white`);
    const light = (hex) => parseInt(hex.slice(1, 3), 16) + parseInt(hex.slice(3, 5), 16) +
                           parseInt(hex.slice(5, 7), 16) > 550;
    assert(light(tokens.ground), `ground ${tokens.ground} is a dark background`);
    assert(!light(tokens.ink), `ink ${tokens.ink} is a light-on-dark text colour`);
  });

  // Every text colour is checked against every surface it can land on, not just white.
  // --ink-3 measured 4.58:1 on #ffffff and passed review, then failed at 4.37:1 on the
  // tinted panel where most of the meta text it carries actually sits.
  t("every text colour meets WCAG AA on every surface it lands on", () => {
    const tok = Object.fromEntries(
      [...html.matchAll(/--([a-z0-9-]+):\s*(#[0-9a-f]{6})/gi)]
        .map((m) => [m[1].toLowerCase(), m[2].toLowerCase()]));
    const chan = (c) => (c <= 0.03928 ? c / 12.92 : ((c + 0.055) / 1.055) ** 2.4);
    const lum = (h) => {
      const [r, g, b] = [1, 3, 5].map((i) => chan(parseInt(h.slice(i, i + 2), 16) / 255));
      return 0.2126 * r + 0.7152 * g + 0.0722 * b;
    };
    const ratio = (a, b) => {
      const [hi, lo] = [lum(a), lum(b)].sort((x, y) => y - x);
      return (hi + 0.05) / (lo + 0.05);
    };

    const surfaces = ["panel", "panel-2", "ground"];
    const inks = ["ink", "ink-2", "ink-3", "accent", "done", "attention", "refused", "new"];
    const bad = [];
    for (const ink of inks) {
      for (const surface of surfaces) {
        if (!tok[ink] || !tok[surface]) continue;
        const r = ratio(tok[ink], tok[surface]);
        if (r < 4.5) bad.push(`--${ink} on --${surface}: ${r.toFixed(2)}:1`);
      }
    }
    assert(!bad.length, bad.join("; "));
  });

  t("the footer says the data is synthetic", () =>
    assert(/synthetic/i.test(text("#foot")), "no synthetic-data notice"));

  t("no errors accumulated across the whole session", () =>
    assert(!errors.length, errors.join(" | ")));

  page.dom.window.close();

  // --- signed in ----------------------------------------------------------

  console.log("\n— signed in (/me answers) —");
  const live = render({ me: ME });
  await settled();

  t("no script errors with a session", () =>
    assert(!live.errors.length, live.errors.join(" | ")));

  t("the header becomes whoever signed in", () => {
    const who = live.doc.getElementById("who").textContent;
    assert(live.doc.querySelector("#who b").textContent === ME.name,
           `header says "${live.doc.querySelector("#who b").textContent}"`);
    assert(live.doc.getElementById("avatar").textContent === ME.initials);
    assert(who.includes(ME.identifier),
           "the identifier every tool call runs under is not shown");
  });

  t("a real session drops the Preview chip", () =>
    assert(live.doc.getElementById("preview-chip").hidden,
           "still labelled a preview while a real session is attached"));

  t("with a real session sign-out is left to the server, not intercepted", () => {
    const ev = new live.window.MouseEvent("click", { bubbles: true, cancelable: true });
    live.doc.getElementById("signout").dispatchEvent(ev);
    assert(!ev.defaultPrevented,
           "the page swallowed the click instead of letting /auth/logout run");
    assert(!live.doc.querySelector(".signed-out"),
           "showed a fake signed-out screen instead of really signing out");
  });

  t("sign-out points at the logout route", () =>
    assert(live.doc.getElementById("signout").getAttribute("href") === "/auth/logout"));

  t("no token could be rendered, because none is sent", () => {
    const body = live.doc.body.textContent;
    for (const secret of ["access_token", "id_token", "Bearer "]) {
      assert(!body.includes(secret), `"${secret}" appears in the page`);
    }
  });

  t("the consultation still renders under a session", () =>
    assert(live.doc.querySelector(".patient-bar h2").textContent === bound.patient.name,
           "the patient bar changed once signed in"));

  live.dom.window.close();

  console.log(failures ? `\n${failures} failed\n` : "\nall checks passed\n");
  process.exit(failures ? 1 : 0);
}

main().catch((e) => { console.error(e); process.exit(1); });

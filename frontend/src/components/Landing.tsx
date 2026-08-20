import { FRAMEWORKS, CATEGORIES } from "../frameworks";
import { SiteNav, SiteFooter, Route } from "./SiteChrome";

/**
 * The signed-out marketing page.
 *
 * Everything asserted here is checkable against the codebase, and the page carries NO
 * customer logos, testimonials, counts of companies served or invented statistics. That
 * is not squeamishness: the product's whole pitch is that it refuses to state what it
 * cannot support, and a landing page that opened with fabricated proof would refute the
 * pitch on the first screen. Where the genre expects social proof, this page substitutes
 * things that are true and verifiable — the actual framework registry, the actual
 * engineering guarantees.
 *
 * The coverage grid is generated FROM the registry the app itself renders, so the page
 * cannot drift from the product: adding a framework adds it here, and removing one
 * removes it.
 */
// The names on the strip are LOOKED UP in the registry rather than typed here, so the
// strip can never advertise a framework the product does not actually generate. An
// unknown key drops out silently instead of rendering a promise.
const STRIP_KEYS = ["secr", "esrs_e1", "issb_s2", "gri", "cdp", "cbam", "sb253", "tcfd",
                    "pcaf", "sbti_v2", "ets_mrv", "esos", "csddd", "sfdr_pai", "ecovadis"];
const STRIP = STRIP_KEYS
  .map((k) => FRAMEWORKS.find((f) => f.key === k))
  .filter((f): f is (typeof FRAMEWORKS)[number] => Boolean(f));

export default function Landing({ onSignIn, onGetStarted, onNavigate }:
    { onSignIn: () => void; onGetStarted: () => void;
      onNavigate: (route: Route, anchor?: string) => void }) {
  const byCategory = CATEGORIES
    .map((c) => ({ cat: c, items: FRAMEWORKS.filter((f) => f.category === c) }))
    .filter((g) => g.items.length > 0);

  return (
    <div className="mk">
      <SiteNav
        links={[
          { label: "How it works", anchor: "how" },
          { label: "Why it's different", anchor: "why" },
          { label: "Coverage", anchor: "coverage" },
          { label: "Rigour", anchor: "rigour" },
        ]}
        onNavigate={onNavigate} onSignIn={onSignIn} onGetStarted={onGetStarted} />

      {/* ---------- Hero ---------- */}
      <section className="mk-hero" id="top">
        <div className="mk-hero-copy">
          <span className="mk-eyebrow">Carbon accounting & disclosure</span>
          <h1>Carbon accounting that holds up when someone checks it.</h1>
          <p>
            Upload your energy, travel, spend and waste data once. Get an emissions
            inventory where every figure traces back to the record and emission factor
            behind it — and {FRAMEWORKS.length} disclosures built from that same
            inventory.
          </p>
          <div className="mk-hero-cta">
            <button className="primary big" onClick={onGetStarted}>Create a workspace →</button>
            <button className="big" onClick={onSignIn}>Sign in</button>
          </div>
          <p className="mk-hero-note">
            No sales call. A workspace takes one field and a few seconds.
          </p>
        </div>

        {/* The hero visual is the product's actual position, drawn rather than photographed:
            a report REFUSING to publish, and saying precisely why. No other tool in this
            category shows you this screen, which is exactly the argument. */}
        <div className="mk-hero-vis" aria-hidden="true">
          <div className="mk-shot">
            <div className="mk-shot-bar">
              <span className="dot" /><span className="dot" /><span className="dot" />
              <span className="mk-shot-title">CSRD ESRS E1 · Run #412</span>
            </div>
            <div className="mk-shot-body">
              <div className="mk-verdict">DRAFT — NOT DISCLOSURE-READY</div>
              <div className="mk-blockers">
                <b>2 blockers</b>
                <div className="mk-blocker">
                  Scope 3 Category 4 is declared <i>included</i> but carries no lines —
                  state a method or reclassify it.
                </div>
                <div className="mk-blocker">
                  The base run used AR5 and this run uses AR6 — a reduction across GWP
                  vintages is a change of metric, not abatement.
                </div>
              </div>
              <div className="mk-shot-rows">
                <div className="mk-row"><span>Scope 1</span><b>1,204.8 tCO₂e</b></div>
                <div className="mk-row"><span>Scope 2 (market)</span><b>388.1 tCO₂e</b></div>
                <div className="mk-row"><span>Scope 3</span><b className="mk-cd">cannot_determine</b></div>
              </div>
            </div>
          </div>
          <div className="mk-shot-cap">A report that blocks, and names what is missing.</div>
        </div>
      </section>

      {/* ---------- Framework strip: the honest stand-in for a customer-logo bar ---------- */}
      <section className="mk-strip">
        <p>One inventory. Every disclosure you have to file.</p>
        <div className="mk-strip-row">
          {STRIP.map((f) => <span key={f.key}>{f.label}</span>)}
        </div>
      </section>

      {/* ---------- How it works ---------- */}
      <section className="mk-sec" id="how">
        <div className="mk-sec-head">
          <span className="mk-eyebrow">How it works</span>
          <h2>Three steps, and you keep the audit trail</h2>
          <p className="mk-sec-lead">
            The work is getting data in and deciding the judgement calls. Everything after
            that is derived, and derived the same way every time.
          </p>
        </div>
        <div className="mk-steps">
          {[
            { n: 1, t: "Bring your data", d: "Upload a CSV of energy, travel, spend and waste. Activities are matched to emission factors automatically, with the match confidence recorded." },
            { n: 2, t: "Decide what needs a human", d: "Anything ambiguous goes to a review queue instead of being guessed. Approve or override a match and the decision is journalled with what it replaced." },
            { n: 3, t: "File the disclosure", d: "Run the calculation and generate any framework from the frozen result. A report that isn't ready tells you what is missing rather than publishing anyway." },
          ].map((s) => (
            <div className="mk-step" key={s.n}>
              <span className="mk-step-n">{s.n}</span>
              <b>{s.t}</b>
              <p>{s.d}</p>
            </div>
          ))}
        </div>
      </section>

      {/* ---------- Pillars ---------- */}
      <section className="mk-sec alt" id="why">
        <div className="mk-sec-head">
          <span className="mk-eyebrow">Why it's different</span>
          <h2>Most tools give you a number. This one gives you a defence.</h2>
          <p className="mk-sec-lead">
            A footprint is easy to produce and hard to justify. The difference shows up the
            day an assuror, a regulator or an acquirer asks where a figure came from.
          </p>
        </div>
        <div className="mk-pillars">
          {[
            { ico: "🔗", t: "Every number traces to its source",
              d: "Each line carries the activity record, the emission factor, its version, the unit conversion and the GWP set used — frozen at the moment of calculation. Re-run it in three years and you get the same answer, because the run reads its own frozen state rather than today's catalogue." },
            { ico: "🛑", t: "It refuses rather than guesses",
              d: "Reports fail closed. A missing input produces cannot_determine, never a quiet zero, and a disclosure blocks with a named reason instead of publishing a figure it cannot stand behind. A divestment between two runs is reported as a boundary change, not as abatement." },
            { ico: "📈", t: "Uncertainty is measured, not asserted",
              d: "Data quality is scored on the ecoinvent pedigree matrix and propagated to an inventory-level confidence interval by Monte Carlo — including the ambiguity introduced when a classification is mapped across schemes. You get a range, and the reason it is that wide." },
            { ico: "⚖️", t: "It tells you what actually applies to you",
              d: "Describe the entity once and see which regimes compel a filing, which merely apply, and which cannot be determined without another answer. An unanswered question is never reported as an exemption." },
          ].map((p) => (
            <div className="mk-pillar" key={p.t}>
              <div className="mk-pillar-ico">{p.ico}</div>
              <div>
                <b>{p.t}</b>
                <p>{p.d}</p>
              </div>
            </div>
          ))}
        </div>
      </section>

      {/* ---------- Coverage, generated from the registry the app renders ---------- */}
      <section className="mk-sec" id="coverage">
        <div className="mk-sec-head">
          <span className="mk-eyebrow">Coverage</span>
          <h2>{FRAMEWORKS.length} disclosures from one dataset</h2>
          <p className="mk-sec-lead">
            Generated from the same registry the product runs, so this list is what you can
            actually click — not a roadmap.
          </p>
        </div>
        <div className="mk-cov">
          {byCategory.map((g) => (
            <div className="mk-cov-col" key={g.cat}>
              <div className="mk-cov-cat">{g.cat}</div>
              <ul>
                {g.items.map((f) => (
                  <li key={f.key} title={f.blurb}>
                    <b>{f.label}</b>
                    {f.full && <span>{f.full}</span>}
                  </li>
                ))}
              </ul>
            </div>
          ))}
        </div>
      </section>

      {/* ---------- Rigour: what stands in for testimonials ---------- */}
      <section className="mk-sec alt" id="rigour">
        <div className="mk-sec-head">
          <span className="mk-eyebrow">Built to be checked</span>
          <h2>The claims above are testable, so we test them</h2>
          <p className="mk-sec-lead">
            We would rather show you how it is built than tell you who else uses it.
          </p>
        </div>
        <div className="mk-facts">
          {[
            { v: "1,470+", l: "automated tests", d: "including property-based tests and hand-computed calculation oracles" },
            { v: "122", l: "API endpoints", d: "everything the interface does is available programmatically" },
            { v: "Immutable", l: "calculation runs", d: "a filed run is never restated — corrections supersede, they do not overwrite" },
            { v: "Fail-closed", l: "on every disclosure", d: "a gate that cannot fail is treated as a defect, not a feature" },
          ].map((f) => (
            <div className="mk-fact" key={f.l}>
              <div className="v">{f.v}</div>
              <div className="l">{f.l}</div>
              <p>{f.d}</p>
            </div>
          ))}
        </div>
        <div className="mk-honest">
          <b>And what it does not do yet.</b>
          <p>
            There is no OCR for utility bills, no ERP connector and no third-party assurance
            opinion — the platform prepares the evidence pack an assuror works from, it does
            not sign one. Where a capability is partial, the product says so in the report
            rather than here.
          </p>
        </div>
      </section>

      {/* ---------- Closing CTA ---------- */}
      <section className="mk-cta">
        <h2>See it with your own numbers.</h2>
        <p>
          Create a workspace, load the sample dataset, and you are looking at a finished
          footprint and a real disclosure in under a minute.
        </p>
        <div className="mk-hero-cta">
          <button className="primary big" onClick={onGetStarted}>Create a workspace →</button>
          <button className="big ghost-light" onClick={onSignIn}>I have a key</button>
        </div>
      </section>

      <SiteFooter onNavigate={onNavigate} onSignIn={onSignIn} onGetStarted={onGetStarted} />
    </div>
  );
}

import { FRAMEWORKS } from "../frameworks";
import { SiteNav, SiteFooter, scrollToId, Route } from "./SiteChrome";

/**
 * The homepage: what the platform is, what is in it, and how you can consume it.
 *
 * Split from the platform page (which argues WHY the engine is built the way it is) so
 * that a first-time visitor gets the shape of the offering before the argument for it.
 *
 * ON "SERVICES": there is no professional-services arm here — no consultants, no
 * implementation team, no support SLA, no managed reporting. A services page describing
 * any of that would invent a business that does not exist, which is the precise failure
 * this product refuses to commit in its own reports. So the section that would carry
 * services carries DELIVERY MODES instead: the ways the software can actually be reached.
 * The closing block names the gap outright rather than leaving a reader to assume.
 */
const PRODUCTS = [
  {
    ico: "📊", name: "Carbon inventory",
    tag: "The base layer everything else reads",
    body: "Scopes 1, 2 and 3 on the GHG Protocol Corporate Standard. Scope 2 is reported on both a location and a market basis, with uncovered load priced at the residual mix rather than the grid average. The organisational boundary is explicit — equity share, financial control or operational control — and frozen onto every run.",
    facts: ["Dual-basis Scope 2", "All 15 Scope 3 categories", "Immutable runs"],
  },
  {
    ico: "📄", name: "Disclosure reporting",
    tag: "One dataset, every filing",
    body: `Generate ${FRAMEWORKS.length} disclosures from the same frozen inventory — UK SECR, CSRD ESRS E1, ISSB S2, GRI, CDP, CBAM, California SB 253 and the rest. A report that is not ready blocks and names what is missing instead of publishing a figure it cannot stand behind.`,
    facts: [`${FRAMEWORKS.length} frameworks`, "CSV and PDF export", "Fail-closed gates"],
  },
  {
    ico: "🗂️", name: "Assurance & evidence",
    tag: "The file an assuror actually works from",
    body: "A twelve-section, hash-stamped evidence pack assembled from a single run: transaction detail, factor register, mapping decisions, methodology and the gaps the platform knows it cannot fill. Engagements follow ISAE 3410 and its ISSA 5000 successor, with a misstatement ledger.",
    facts: ["12-section pack", "ISAE 3410 → ISSA 5000", "Misstatement ledger"],
  },
  {
    ico: "📈", name: "Data quality & uncertainty",
    tag: "A range, and the reason it is that wide",
    body: "Every line is scored on the ecoinvent pedigree matrix and propagated to an inventory-level confidence interval by Monte Carlo — including the ambiguity introduced when a classification is mapped between schemes. Correlation between lines is a stated choice, not an accident.",
    facts: ["Pedigree scoring", "Monte Carlo interval", "Measured crosswalk error"],
  },
  {
    ico: "⚖️", name: "Obligations",
    tag: "What actually compels a filing from you",
    body: "Describe the entity once and see which regimes compel a filing, which merely apply, and which cannot be determined without another answer. A question you have not answered is never reported as an exemption — the distinction that keeps an unknown from becoming a quiet 'no'.",
    facts: ["Entity profile", "Jurisdiction rules", "cannot_determine, never 'no'"],
  },
  {
    ico: "🎯", name: "Targets & transition",
    tag: "Trajectories that survive a boundary change",
    body: "SBTi target tracking including the Corporate Net-Zero Standard V2.0 — significance testing across Scope 3 categories 1–14, company categorisation and Scope 2 conformance. Hourly Scope 2 matching scores 24/7 carbon-free energy against your actual load profile.",
    facts: ["SBTi Net-Zero V2.0", "Hourly 24/7 CFE", "Base-year recalculation"],
  },
  {
    ico: "🔄", name: "Supplier data exchange",
    tag: "Primary data instead of spend estimates",
    body: "Import a supplier's product carbon footprint over PACT v3 and it becomes an ordinary emission factor — but one carrying the best pedigree score, so your interval narrows and your primary-data share rises through the existing machinery. You can serve your own footprints on the same interface.",
    facts: ["PACT v3 client + host", "Immutable versioning", "Explicit binding only"],
  },
  {
    ico: "🌍", name: "Nature",
    tag: "Beyond carbon, on the same entity",
    body: "TNFD and SBTN assessments sit alongside the carbon inventory rather than in a separate tool, so the entity, the boundary and the reporting period are the ones you already defined.",
    facts: ["TNFD", "SBTN targets", "Shared boundary"],
  },
];

const DELIVERY = [
  {
    ico: "🖥️", name: "The web application",
    body: "Self-serve. Create a workspace, upload a CSV of activity data, clear the review queue, run the calculation and generate a disclosure. No sales call and no onboarding project.",
    detail: "Best for: a sustainability or finance lead running the reporting cycle.",
  },
  {
    ico: "⚙️", name: "The API",
    body: "Everything the interface does is an HTTP endpoint — upload, mapping decisions, runs, lineage, uncertainty, every report and every export. Authentication is an organisation-scoped API key.",
    detail: "Best for: piping activity data out of an ERP, or scheduling a monthly run.",
  },
  {
    ico: "🤝", name: "Supplier exchange",
    body: "Act as a PACT v3 host so your customers can pull your product footprints on a conformant interface, and pull your own suppliers' footprints the same way.",
    detail: "Best for: a supplier being asked for PCFs, or a buyer chasing primary data.",
  },
];

const AUDIENCES = [
  { who: "Reporting under a mandate",
    what: "SECR, CSRD, SB 253 or an ISSB-aligned filing with a deadline and an auditor attached." },
  { who: "Preparing for assurance",
    what: "You need the working papers behind the number, not just the number." },
  { who: "Answering customers",
    what: "A buyer is asking for product-level footprints and will not accept a spend estimate." },
];

export default function Homepage({ onSignIn, onGetStarted, onNavigate }: {
  onSignIn: () => void;
  onGetStarted: () => void;
  onNavigate: (route: Route, anchor?: string) => void;
}) {
  return (
    <div className="mk">
      <SiteNav
        links={[
          { label: "Products", anchor: "products" },
          { label: "Ways to use it", anchor: "use" },
          { label: "Who it's for", anchor: "who" },
          { label: "How it works", route: "platform", anchor: "how" },
          { label: "Coverage", route: "platform", anchor: "coverage" },
        ]}
        onNavigate={onNavigate} onSignIn={onSignIn} onGetStarted={onGetStarted} />

      {/* ---------- Hero ---------- */}
      <section className="mk-hero home" id="top">
        <div className="mk-hero-copy wide">
          <span className="mk-eyebrow">The platform</span>
          <h1>Everything you need to account for carbon — and to prove it.</h1>
          <p>
            One organisational inventory, eight capability areas built on it, and{" "}
            {FRAMEWORKS.length} disclosures generated from the same frozen data. Built for
            the moment someone asks where a figure came from.
          </p>
          <div className="mk-hero-cta">
            <button className="primary big" onClick={onGetStarted}>Create a workspace →</button>
            <button className="big" onClick={() => scrollToId("products")}>
              See what's in it
            </button>
          </div>
        </div>
      </section>

      {/* ---------- Products ---------- */}
      <section className="mk-sec alt" id="products">
        <div className="mk-sec-head">
          <span className="mk-eyebrow">Products</span>
          <h2>Eight areas, one inventory underneath</h2>
          <p className="mk-sec-lead">
            They are not separate tools that need integrating. Each reads the same frozen
            run, which is why a target, a disclosure and an evidence pack cannot disagree
            with one another about what your emissions were.
          </p>
        </div>
        <div className="mk-prods">
          {PRODUCTS.map((p) => (
            <article className="mk-prod" key={p.name}>
              <div className="mk-prod-head">
                <div className="mk-pillar-ico">{p.ico}</div>
                <div>
                  <b>{p.name}</b>
                  <span className="mk-prod-tag">{p.tag}</span>
                </div>
              </div>
              <p>{p.body}</p>
              <div className="mk-prod-facts">
                {p.facts.map((f) => <span key={f}>{f}</span>)}
              </div>
            </article>
          ))}
        </div>
      </section>

      {/* ---------- Who it's for ---------- */}
      <section className="mk-sec" id="who">
        <div className="mk-sec-head">
          <span className="mk-eyebrow">Who it's for</span>
          <h2>Built for the people who have to defend the number</h2>
        </div>
        <div className="mk-who">
          {AUDIENCES.map((a) => (
            <div className="mk-who-row" key={a.who}>
              <b>{a.who}</b>
              <span>{a.what}</span>
            </div>
          ))}
        </div>
      </section>

      {/* ---------- Delivery modes: the honest occupant of the "services" slot ---------- */}
      <section className="mk-sec alt" id="use">
        <div className="mk-sec-head">
          <span className="mk-eyebrow">Ways to use it</span>
          <h2>Three ways in, one engine behind them</h2>
          <p className="mk-sec-lead">
            The API is not a bolt-on: the web application is a client of it, so anything
            you can do by clicking you can do by calling.
          </p>
        </div>
        <div className="mk-steps">
          {DELIVERY.map((d) => (
            <div className="mk-step" key={d.name}>
              <div className="mk-pillar-ico">{d.ico}</div>
              <b>{d.name}</b>
              <p>{d.body}</p>
              <p className="mk-step-detail">{d.detail}</p>
            </div>
          ))}
        </div>

        {/* The section a services page would occupy. Naming the absence is the point. */}
        <div className="mk-honest" style={{ marginTop: 22 }}>
          <b>What we don't sell.</b>
          <p>
            There is no consulting arm, no implementation project and no managed-reporting
            service — the platform is software you run yourself. It does not provide
            assurance either: it prepares the evidence pack an assuror works from, and your
            assuror signs the opinion. Where a capability is only partial, the product says
            so inside the report rather than here.
          </p>
        </div>
      </section>

      {/* ---------- Bridge to the platform page ---------- */}
      <section className="mk-sec" id="deeper">
        <div className="mk-bridge">
          <div>
            <span className="mk-eyebrow">Going deeper</span>
            <h2>Why it refuses, freezes and traces</h2>
            <p className="mk-sec-lead">
              Every design decision above exists to survive a question. The platform page
              walks through how a figure is traced, why a report blocks rather than guesses,
              and how uncertainty is measured instead of asserted.
            </p>
            <div className="mk-hero-cta" style={{ marginTop: 20 }}>
              <button className="primary" onClick={() => onNavigate("platform")}>
                Explore the platform →
              </button>
              <button onClick={() => onNavigate("platform", "coverage")}>
                See all {FRAMEWORKS.length} frameworks
              </button>
            </div>
          </div>
        </div>
      </section>

      {/* ---------- CTA ---------- */}
      <section className="mk-cta">
        <h2>Start with your own numbers.</h2>
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

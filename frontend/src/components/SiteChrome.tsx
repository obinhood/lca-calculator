import { useEffect, useState } from "react";

/**
 * The nav and footer shared by every marketing page.
 *
 * Extracted the moment there was a second page rather than copied into it. A rule with
 * one caller in this codebase has a habit of becoming N partial re-derivations of itself,
 * and chrome is the most copy-pasted thing on any site — two footers drifting apart is
 * how a stale link outlives the page it points at.
 */
export type Route = "home" | "platform" | "signin";

export type NavLink =
  | { label: string; anchor: string }          // scrolls within the current page
  | { label: string; route: Route; anchor?: string };  // navigates, optionally to a section

/**
 * Scroll to a section, and actually arrive.
 *
 * Smooth scrolling is not universally honoured: some embedded and automated browsers
 * accept `behavior: "smooth"` and then do nothing at all, which turns every in-page nav
 * link on the site into a dead control. So the smooth call is verified — if nothing has
 * moved shortly afterwards, jump. A link that jumps is worse than one that glides; a link
 * that silently does nothing is worse than both.
 *
 * Readers who have asked for reduced motion skip the animation entirely rather than
 * having it forced on them and then corrected.
 */
export const scrollToId = (id: string) => {
  const el = document.getElementById(id);
  if (!el) return;
  if (window.matchMedia?.("(prefers-reduced-motion: reduce)").matches) {
    el.scrollIntoView();
    return;
  }
  const before = window.scrollY;
  el.scrollIntoView({ behavior: "smooth", block: "start" });
  window.setTimeout(() => {
    // A reader who scrolled themselves in the meantime has moved it; leave them alone.
    if (Math.abs(window.scrollY - before) < 2) el.scrollIntoView();
  }, 350);
};

export function SiteNav({ links, onNavigate, onSignIn, onGetStarted }: {
  links: NavLink[];
  onNavigate: (route: Route, anchor?: string) => void;
  onSignIn: () => void;
  onGetStarted: () => void;
}) {
  const [scrolled, setScrolled] = useState(false);

  useEffect(() => {
    const onScroll = () => setScrolled(window.scrollY > 8);
    window.addEventListener("scroll", onScroll, { passive: true });
    return () => window.removeEventListener("scroll", onScroll);
  }, []);

  const click = (l: NavLink) => (e: React.MouseEvent) => {
    e.preventDefault();
    if ("route" in l && l.route) onNavigate(l.route, l.anchor);
    else if (l.anchor) scrollToId(l.anchor);
  };

  return (
    <header className={"mk-nav" + (scrolled ? " stuck" : "")}>
      <div className="mk-nav-in">
        <a className="mk-logo" href="#/"
           onClick={(e) => { e.preventDefault(); onNavigate("home"); }}>
          <span className="mark">🌿</span>
          <b>Carbon Platform</b>
        </a>
        <nav className="mk-links">
          {links.map((l) => (
            <a key={l.label} href={"#" + (l.anchor || "")} onClick={click(l)}>{l.label}</a>
          ))}
        </nav>
        <div className="mk-nav-cta">
          <button className="ghost" onClick={onSignIn}>Sign in</button>
          <button className="primary" onClick={onGetStarted}>Get started</button>
        </div>
      </div>
    </header>
  );
}

export function SiteFooter({ onNavigate, onSignIn, onGetStarted }: {
  onNavigate: (route: Route, anchor?: string) => void;
  onSignIn: () => void;
  onGetStarted: () => void;
}) {
  const go = (route: Route, anchor?: string) => (e: React.MouseEvent) => {
    e.preventDefault();
    onNavigate(route, anchor);
  };

  return (
    <footer className="mk-foot">
      <div className="mk-foot-in">
        <div className="mk-foot-brand">
          <span className="mark">🌿</span>
          <div>
            <b>Carbon Platform</b>
            <span>Audit-grade carbon accounting and disclosure.</span>
          </div>
        </div>
        <div className="mk-foot-cols">
          <div>
            <h4>Product</h4>
            <a href="#/" onClick={go("home", "products")}>What's in the platform</a>
            <a href="#/platform" onClick={go("platform", "how")}>How it works</a>
            <a href="#/platform" onClick={go("platform", "why")}>Why it's different</a>
            <a href="#/platform" onClick={go("platform", "coverage")}>Framework coverage</a>
          </div>
          <div>
            <h4>Ways to use it</h4>
            <a href="#/" onClick={go("home", "use")}>The web app</a>
            <a href="#/" onClick={go("home", "use")}>The API</a>
            <a href="#/" onClick={go("home", "use")}>Supplier exchange (PACT)</a>
          </div>
          <div>
            <h4>Standards</h4>
            <span>GHG Protocol Corporate</span>
            <span>Scope 2 &amp; Scope 3 Guidance</span>
            <span>ISO 14064 · ISO 14067</span>
          </div>
          <div>
            <h4>Access</h4>
            <a href="#/signin" onClick={(e) => { e.preventDefault(); onGetStarted(); }}>
              Create a workspace
            </a>
            <a href="#/signin" onClick={(e) => { e.preventDefault(); onSignIn(); }}>Sign in</a>
          </div>
        </div>
      </div>
      <div className="mk-foot-bar">
        <span>
          Emission factors are attributed to their publishers. This platform prepares
          disclosures and the evidence an assuror works from; it does not provide
          assurance, audit or legal advice.
        </span>
      </div>
    </footer>
  );
}

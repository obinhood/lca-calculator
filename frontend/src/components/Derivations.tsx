type Term = {
  label: string; value: number | null; unit?: string | null;
  count?: number | null; source?: string | null; note?: string | null;
  // `why` is emitted only when a term's value is unknown; `conversion` only when a
  // currency or unit conversion was applied.
  conversion?: string | null; why?: string | null;
};
type Figure = {
  figure: string; unit?: string | null; operation: string; basis?: string | null;
  note?: string | null; terms: Term[]; expression: string;
  computed: number | null; reported: number | null; reconciles: boolean;
  independent?: boolean;
  reconciliation_error?: string; blocked_reason?: string; difference?: number | null;
};
export type DerivationBlock = {
  figures: Figure[]; count: number; computed_count: number; all_reconcile: boolean;
  unreconciled: string[]; calculations_refused: string[];
  independently_verified: string[]; note: string; warning?: string;
};

const fmt = (v: number | null | undefined) =>
  v === null || v === undefined ? "—"
    : Number.isInteger(v) ? v.toLocaleString()
    : v.toLocaleString(undefined, { maximumSignificantDigits: 6 });

function Row({ t }: { t: Term }) {
  const known = t.value !== null && t.value !== undefined;
  return (
    <>
      <tr>
        <td style={{ paddingLeft: 4 }}>
          {t.label}
          {t.count !== null && t.count !== undefined && (
            <span className="muted"> · {t.count} record{t.count === 1 ? "" : "s"}</span>
          )}
        </td>
        <td style={{ textAlign: "right", fontVariantNumeric: "tabular-nums" }}>
          {known ? fmt(t.value) : <span className="muted">{t.why || "not provided"}</span>}
        </td>
      </tr>
      {(t.note || t.conversion) && (
        <tr>
          <td colSpan={2} className="muted" style={{ paddingLeft: 16, fontSize: "0.9em" }}>
            {t.conversion && <div>{t.conversion}</div>}
            {t.note && <div>{t.note}</div>}
          </td>
        </tr>
      )}
    </>
  );
}

function FigureCard({ f }: { f: Figure }) {
  const isBlocked = f.operation === "blocked";
  const isAlt = f.operation === "alternatives";
  const isInput = f.operation === "stated";
  return (
    <div className="notice" style={{ marginTop: 10 }}>
      <div className="row" style={{ justifyContent: "space-between", alignItems: "baseline" }}>
        <b>{f.figure}</b>
        {f.independent && <span className="badge ok" style={{ marginLeft: 8 }}>verified</span>}
        <span style={{ fontVariantNumeric: "tabular-nums" }}>
          <b>{fmt(f.reported)}</b>{f.unit ? ` ${f.unit}` : ""}
        </span>
      </div>
      {f.basis && <div className="muted" style={{ marginTop: 2 }}>Basis: {f.basis}</div>}

      {isBlocked ? (
        <div className="notice warn" style={{ marginTop: 8 }}>
          <b>No working is shown for this figure.</b>
          <div style={{ marginTop: 4 }}>{f.blocked_reason}</div>
        </div>
      ) : (
        <>
          <table style={{ width: "100%", marginTop: 8 }}>
            <tbody>{f.terms.map((t, i) => <Row key={i} t={t} />)}</tbody>
          </table>
          {!isInput && !isAlt && (
            <div style={{ marginTop: 6, fontVariantNumeric: "tabular-nums" }}>
              <b>= {f.expression}</b>
            </div>
          )}
          {isAlt && (
            <div className="muted" style={{ marginTop: 6 }}>
              These are alternative measurements of the same quantity. The standard
              requires both; they are never added together.
            </div>
          )}
          {isInput && (
            <div className="muted" style={{ marginTop: 6 }}>
              An input, not a calculation — there is no arithmetic behind this figure.
            </div>
          )}
        </>
      )}

      {f.note && !isBlocked && (
        <div className="muted" style={{ marginTop: 6 }}>{f.note}</div>
      )}
      {!f.reconciles && !isBlocked && (
        <div className="notice warn" style={{ marginTop: 8 }}>{f.reconciliation_error}</div>
      )}
    </div>
  );
}

export default function Derivations({ block }: { block: DerivationBlock }) {
  if (!block?.figures?.length) return null;
  return (
    <div className="card">
      <div className="card-head">
        <h3>How these figures were calculated</h3>
        <div className="spacer" />
        <span className={"badge " + (block.all_reconcile ? "ok" : "warn")}>
          {/* Counts only actual calculations — inputs and alternative-measurement pairs
              are neither reperformed nor reperformable. */}
          {block.all_reconcile
            ? `✓ ${block.computed_count} calculation${block.computed_count === 1 ? "" : "s"} check out`
            : `${block.unreconciled.length + block.calculations_refused.length} of ` +
              `${block.computed_count} did not`}
        </span>
      </div>
      <p className="lead" style={{ marginTop: 6 }}>{block.note}</p>
      {block.independently_verified?.length > 0 && (
        <div className="muted" style={{ marginTop: 6 }}>
          <b>Independently verified:</b> {block.independently_verified.join(", ")} — checked
          against a separately derived value, so these fail on bad data rather than
          restating it.
        </div>
      )}
      {block.warning && (
        <div className="notice warn" style={{ marginTop: 8 }}>{block.warning}</div>
      )}
      {block.figures.map((f) => <FigureCard key={f.figure} f={f} />)}
    </div>
  );
}

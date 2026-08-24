"""Importing and holding PACT ProductFootprints.

The validator in ``services/pact.py`` decides whether a document conforms; this
decides what happens to it. Two rules govern that, and both follow from the fact
that a supplier's PCF is somebody else's assertion which we will later put inside
our own inventory:

1. AN INVALID DOCUMENT IS NEVER STORED AS THOUGH IT PASSED. Import refuses and
   returns the errors. There is no "store it and flag it" path, because a stored
   footprint is exactly the thing that later becomes a primary-data factor — and a
   flag is not a barrier once the row exists.

2. VERSIONING IS IMMUTABLE, AS v3 REQUIRES. A corrected PCF arrives as a NEW id
   listing the old one in `precedingPfIds`. Importing it deprecates the superseded
   row rather than overwriting it, so a filed run that used the old figure can
   still show what it used. Re-importing the SAME id is idempotent, not an update:
   v3 removed in-place edits entirely, so a same-id document whose content differs
   is a protocol violation and is refused with both hashes.
"""
import hashlib
import json
from typing import Optional

from sqlalchemy.orm import Session

from ..models import ProductFootprint
from .pact import kg_co2e_per_declared_unit, parse_document, summarise, validate


def _canonical_hash(doc: dict) -> str:
    """Content hash of a document, independent of key order and whitespace."""
    return hashlib.sha256(
        json.dumps(doc, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _row_document(row: ProductFootprint) -> dict:
    try:
        return json.loads(row.document)
    except (ValueError, TypeError):
        return {}


def import_footprint(db: Session, organisation_id: int, raw, *,
                     direction: str = "received",
                     source_url: Optional[str] = None,
                     now: Optional[str] = None) -> dict:
    """Validate and store one ProductFootprint document.

    Returns ``{stored, pf_id, id, warnings, errors, deprecated, reason}``.
    ``stored`` is False for every refusal, and the reason says which.
    """
    from .calc import _utcnow_iso
    now = now or _utcnow_iso()

    if direction not in ("received", "published"):
        return {"stored": False, "errors": [], "warnings": [],
                "reason": "direction must be 'received' or 'published'"}

    doc, parse_error = parse_document(raw)
    if parse_error:
        return {"stored": False, "errors": [{"field": None, "severity": "error",
                                             "message": parse_error}],
                "warnings": [], "reason": parse_error}

    verdict = validate(doc)
    if not verdict["valid"]:
        return {"stored": False, "pf_id": doc.get("id"),
                "errors": verdict["errors"], "warnings": verdict["warnings"],
                "reason": f"{len(verdict['errors'])} validation error(s); a "
                          f"non-conforming footprint is not stored, because a stored "
                          f"footprint is what later becomes a primary-data factor"}

    s = summarise(doc)
    incoming_hash = _canonical_hash(doc)

    existing = db.query(ProductFootprint).filter(
        ProductFootprint.organisation_id == organisation_id,
        ProductFootprint.pf_id == s["pf_id"]).first()
    if existing is not None:
        if _canonical_hash(_row_document(existing)) == incoming_hash:
            return {"stored": False, "idempotent": True, "id": existing.id,
                    "pf_id": existing.pf_id, "errors": [],
                    "warnings": verdict["warnings"],
                    "reason": "already held, byte-identical content — nothing to do"}
        return {
            "stored": False, "id": existing.id, "pf_id": existing.pf_id,
            "errors": [{"field": "id", "severity": "error",
                        "message": "a footprint with this id is already held and the "
                                   "content differs. v3 removed in-place updates: a "
                                   "correction must arrive as a NEW id listing this one "
                                   "in precedingPfIds."}],
            "warnings": verdict["warnings"],
            "held_content_hash": _canonical_hash(_row_document(existing)),
            "incoming_content_hash": incoming_hash,
            "reason": "same id, different content — a protocol violation under v3"}

    row = ProductFootprint(
        organisation_id=organisation_id, direction=direction,
        pf_id=s["pf_id"], spec_version=s["spec_version"], status=s["status"],
        created=s["created"],
        preceding_pf_ids=json.dumps(s["preceding_pf_ids"]),
        company_name=s["company_name"], company_ids=json.dumps(s["company_ids"]),
        product_name=s["product_name"], product_description=s["product_description"],
        product_ids=json.dumps(s["product_ids"]),
        declared_unit=s["declared_unit"], declared_unit_amount=s["declared_unit_amount"],
        kg_co2e_per_unit_excl_biogenic=kg_co2e_per_declared_unit(s),
        kg_co2e_per_unit_incl_biogenic=kg_co2e_per_declared_unit(s, include_biogenic=True),
        reference_period_start=s["reference_period_start"],
        reference_period_end=s["reference_period_end"],
        validity_period_start=s["validity_period_start"],
        validity_period_end=s["validity_period_end"],
        primary_data_share=s["primary_data_share"],
        geography_level=s["geography_level"], geography_value=s["geography_value"],
        dqi_technological=s["dqi_technological"],
        dqi_geographical=s["dqi_geographical"],
        dqi_temporal=s["dqi_temporal"],
        # Verbatim. Re-serialised from the parsed object so the stored form is
        # canonical JSON, but no field is added, dropped or coerced.
        document=json.dumps(doc, sort_keys=True, separators=(",", ":")),
        source_url=source_url,
        validation_warnings=json.dumps(verdict["warnings"]),
        received_at=now, created_at=now)
    db.add(row)
    db.flush()

    # Supersession: deprecate every held footprint this one declares it replaces.
    # The superseded rows are KEPT — a filed run that used the old figure must still
    # be able to show what it used.
    deprecated = []
    for old_id in s["preceding_pf_ids"]:
        old = db.query(ProductFootprint).filter(
            ProductFootprint.organisation_id == organisation_id,
            ProductFootprint.pf_id == old_id).first()
        if old is not None and old.status != "Deprecated":
            old.status = "Deprecated"
            deprecated.append(old.pf_id)
    # THE REVERSE QUESTION, which was never asked: does anything already held name THIS
    # footprint among the ones IT replaces? Supersession was walked in one direction only,
    # so an out-of-order import — the correction arriving before the document it corrects,
    # which is exactly what a backfill or a replayed feed does — left the superseded
    # footprint Active and materialisable as a best-pedigree supplier_specific factor.
    # A figure its own author has withdrawn would then price next year's inventory.
    superseded_by = [r.pf_id for r in db.query(ProductFootprint).filter(
        ProductFootprint.organisation_id == organisation_id,
        ProductFootprint.pf_id != row.pf_id).all()
        if row.pf_id in _preceding_ids(r)]
    if superseded_by and row.status != "Deprecated":
        row.status = "Deprecated"

    db.commit()
    db.refresh(row)

    return {"stored": True, "id": row.id, "pf_id": row.pf_id,
            "spec_version": row.spec_version,
            "kg_co2e_per_unit": row.kg_co2e_per_unit_excl_biogenic,
            "declared_unit": row.declared_unit,
            "deprecated_superseded": deprecated,
            "deprecated_on_arrival_by": superseded_by,
            "deprecated_on_arrival_note": (
                f"a footprint already held ({', '.join(superseded_by)}) declares this one "
                f"among those it replaces, so it arrived already superseded and is stored "
                f"Deprecated. It is kept, because a filed run that used it must still be "
                f"able to show what it used."
            ) if superseded_by else None,
            "errors": [], "warnings": verdict["warnings"],
            "reason": None}


def _preceding_ids(row: ProductFootprint) -> set:
    """The pf_ids a held footprint declares it replaces, from its stored document."""
    try:
        doc = json.loads(row.document or "{}")
    except (ValueError, TypeError):
        return set()
    raw = doc.get("precedingPfIds")
    return {str(v).strip() for v in raw if str(v).strip()} if isinstance(raw, list) else set()


def footprint_view(row: ProductFootprint, *, include_document: bool = False) -> dict:
    """One held footprint, as reported."""
    out = {
        "id": row.id, "pf_id": row.pf_id, "direction": row.direction,
        "spec_version": row.spec_version, "status": row.status,
        "created": row.created,
        "preceding_pf_ids": json.loads(row.preceding_pf_ids or "[]"),
        "company_name": row.company_name,
        "company_ids": json.loads(row.company_ids or "[]"),
        "product_name": row.product_name,
        "product_description": row.product_description,
        "product_ids": json.loads(row.product_ids or "[]"),
        "declared_unit": row.declared_unit,
        "declared_unit_amount": row.declared_unit_amount,
        "kg_co2e_per_unit_excl_biogenic": row.kg_co2e_per_unit_excl_biogenic,
        "kg_co2e_per_unit_incl_biogenic": row.kg_co2e_per_unit_incl_biogenic,
        "reference_period": {"start": row.reference_period_start,
                             "end": row.reference_period_end},
        "validity_period": {"start": row.validity_period_start,
                            "end": row.validity_period_end},
        "primary_data_share": row.primary_data_share,
        "geography": {"level": row.geography_level, "value": row.geography_value},
        "dqi": {"technological": row.dqi_technological,
                "geographical": row.dqi_geographical,
                "temporal": row.dqi_temporal},
        "source_url": row.source_url,
        "received_at": row.received_at,
        "validation_warnings": json.loads(row.validation_warnings or "[]"),
        "note": "kg_co2e_per_unit_* are per ONE declared unit. The document quotes "
                "the footprint against declaredUnitAmount, so it was divided at "
                "import; the undivided figure is in the document.",
    }
    if include_document:
        out["document"] = json.loads(row.document)
    return out


def list_footprints(db: Session, organisation_id: int, *,
                    direction: Optional[str] = None,
                    status: Optional[str] = None,
                    product_id: Optional[str] = None) -> list:
    """Held footprints, newest first, filtered as asked."""
    q = db.query(ProductFootprint).filter(
        ProductFootprint.organisation_id == organisation_id)
    if direction:
        q = q.filter(ProductFootprint.direction == direction)
    if status:
        q = q.filter(ProductFootprint.status == status)
    rows = q.order_by(ProductFootprint.id.desc()).all()
    if product_id:
        needle = product_id.strip().lower()
        rows = [r for r in rows
                if any(needle == str(x).strip().lower()
                       for x in json.loads(r.product_ids or "[]"))]
    return [footprint_view(r) for r in rows]

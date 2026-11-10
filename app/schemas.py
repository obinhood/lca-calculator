from pydantic import BaseModel, ConfigDict
from typing import Optional, List, Any

class UploadResponse(BaseModel):
    records_ingested: int
    issues: List[str]

class SummaryRow(BaseModel):
    scope: str
    category: str
    co2e: float

class SummaryResponse(BaseModel):
    total_co2e: float
    by_scope: List[SummaryRow]
    by_category: List[SummaryRow]
    notes: Optional[str] = None


# --- Narrative request bodies ------------------------------------------------
#
# Narrative free text and person-identifying values travel in the request BODY, never in
# the query string.
#
# The shipped production command logs the full request line verbatim — including on
# requests that fail authorisation, and with no deletion path. So an assurance finding's
# description, an assuror's name, or the note explaining why a misstatement was accepted,
# sent as `?description=...`, came to rest in an access log that nobody can redact and
# that was never meant to hold it. A request body is not part of the request line.
#
# Every field defined below is either prose a person wrote or the name of a person or
# firm. Codes, enums, identifiers, dates, currencies and units stay in the query string:
# they are not narrative, and moving them would be churn without a reason.


class NarrativeBody(BaseModel):
    """Base for the bodies below: unknown keys are REFUSED, not ignored.

    A client that misspells `resolution_note` has to be told. Silently accepting the
    request and storing no note would be the same defect in a new place — a write that
    looks like it succeeded while the narrative it carried is gone.
    """
    model_config = ConfigDict(extra="forbid")


class MarketInstrumentBody(NarrativeBody):
    description: Optional[str] = None


class CbamGoodBody(NarrativeBody):
    description: Optional[str] = None
    installation: Optional[str] = None       # names an operator's facility


class LcaItemBody(NarrativeBody):
    description: Optional[str] = None


class NatureImpactBody(NarrativeBody):
    description: Optional[str] = None


class ConsolidationBody(NarrativeBody):
    reason: str                              # REQUIRED; checked for boilerplate


class EntityBody(NarrativeBody):
    equity_share_basis: Optional[str] = None
    control_rationale: Optional[str] = None


class Scope3DeclarationBody(NarrativeBody):
    justification: Optional[str] = None
    method_description: Optional[str] = None
    declared_by: Optional[str] = None        # names a person


class EngagementBody(NarrativeBody):
    assuror_name: Optional[str] = None       # names a person or firm


class FindingBody(NarrativeBody):
    description: str                         # REQUIRED


class ResolveFindingBody(NarrativeBody):
    resolution_note: str                     # REQUIRED


class ConcludeBody(NarrativeBody):
    opinion_note: Optional[str] = None


class DisposeFindingBody(NarrativeBody):
    note: str                                # REQUIRED


class DeliverabilityLinkBody(NarrativeBody):
    rationale: Optional[str] = None


class PactClientBody(NarrativeBody):
    partner_name: Optional[str] = None       # names a counterparty


class Iso140642Body(NarrativeBody):
    baseline_justification: Optional[str] = None
    project_name: Optional[str] = None


class Sb253Body(NarrativeBody):
    assurance_provider: Optional[str] = None  # names a firm

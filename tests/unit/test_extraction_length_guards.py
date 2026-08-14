from auspex.models.enums import NarrativeClaimType, RiskCategory, RiskSeverity, ThemeStrength
from auspex.models.extraction import KeyQuote, NarrativeClaim, RiskFactorAdded, RiskFactorRemoved


def test_llm_excerpts_are_clipped_before_length_validation():
    claim = NarrativeClaim(
        claim_type=NarrativeClaimType.NEW_PRODUCT,
        strength=ThemeStrength.STRONG,
        evidence_excerpt="x" * 500,
    )
    assert len(claim.evidence_excerpt) == 300
    assert claim.evidence_excerpt.endswith("...")


def test_quotes_and_comparative_verbatim_are_clipped():
    quote = KeyQuote(text="x" * 500, section="Item 7", why_it_matters="evidence")
    added = RiskFactorAdded(
        summary="risk",
        verbatim="x" * 500,
        category=RiskCategory.SUPPLY,
        severity=RiskSeverity.HIGH,
    )
    removed = RiskFactorRemoved(summary="risk", prior_verbatim="x" * 500)

    assert len(quote.text) == 400
    assert len(added.verbatim) == 400
    assert len(removed.prior_verbatim) == 400

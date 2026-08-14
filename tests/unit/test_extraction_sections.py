"""Unit tests for filing section targeting (arc42 §5.4 "Section targeting")."""

from __future__ import annotations

from auspex.extraction.sections import WHOLE_DOCUMENT_FORMS, target_sections

SAMPLE_10K = """
Item 1. Business
We design and manufacture semiconductors for the data center market.

Item 1A. Risk Factors
Our business depends on a small number of customers, which creates customer
concentration risk. Supply chain disruption could materially affect margins.

Item 7. Management's Discussion and Analysis
Revenue grew 20% year over year driven by strong demand.

Item 7A. Quantitative and Qualitative Disclosures About Market Risk
We are exposed to interest rate and foreign currency risk.

Item 8. Financial Statements and Supplementary Data
[tables omitted, not targeted]
"""

SAMPLE_10Q = """
Item 1. Financial Statements
[tables omitted]

Item 2. Management's Discussion and Analysis of Financial Condition
Results of Operations
Revenue increased sequentially.

Item 1A. Risk Factors
No material changes from the prior annual report except as noted below.
"""


class TestTenKSectionTargeting:
    def test_extracts_all_four_sections(self):
        sections = target_sections("10-K", SAMPLE_10K)
        items = {s.item for s in sections}
        assert items == {"item_1_business", "item_1a_risk_factors", "item_7_mda", "item_7a_market_risk"}

    def test_business_section_excludes_risk_factors_content(self):
        sections = target_sections("10-K", SAMPLE_10K)
        business = next(s for s in sections if s.item == "item_1_business")
        assert "semiconductors" in business.text
        assert "customer concentration" not in business.text.lower()

    def test_item_8_financial_statements_not_targeted(self):
        sections = target_sections("10-K", SAMPLE_10K)
        for s in sections:
            assert "tables omitted, not targeted" not in s.text

    def test_unknown_form_type_returns_empty(self):
        assert target_sections("UNKNOWN-FORM", SAMPLE_10K) == []

    def test_no_matching_headings_returns_empty(self):
        assert target_sections("10-K", "no relevant headings here at all") == []

    def test_strips_html_and_rejects_table_of_contents_duplicates(self):
        html = """
        <html><body>
        <table><tr><td><span>Item 1. Business</span></td><td>12</td></tr>
        <tr><td>Item 1A. Risk Factors</td><td>24</td></tr></table>
        <div><span>Item 1. </span><b>Business</b></div>
        <p>Microsoft develops productivity software, cloud infrastructure,
        operating systems, and business applications for customers worldwide.</p>
        <div>Item 1A. Risk Factors</div>
        <p>Competition and cybersecurity incidents may affect results.</p>
        <div>Item 2. Properties</div>
        </body></html>
        """

        sections = target_sections("10-K", html)
        business = next(section for section in sections if section.item == "item_1_business")

        assert "cloud infrastructure" in business.text
        assert len(business.text) > len("Item 1. Business")
        assert "<span>" not in business.text


class TestTenQSectionTargeting:
    def test_extracts_mda_and_risk_updates(self):
        sections = target_sections("10-Q", SAMPLE_10Q)
        items = {s.item for s in sections}
        assert "mda" in items
        assert "item_1a_updates" in items

    def test_risk_factor_updates_isolated_from_mda(self):
        sections = target_sections("10-Q", SAMPLE_10Q)
        risk_section = next(s for s in sections if s.item == "item_1a_updates")
        assert "no material changes" in risk_section.text.lower()
        assert "revenue increased" not in risk_section.text.lower()


class TestWholeDocumentForms:
    def test_8k_and_6k_are_whole_document_forms(self):
        assert WHOLE_DOCUMENT_FORMS == frozenset({"8-K", "6-K"})

    def test_whole_document_forms_are_not_in_section_patterns(self):
        # 8-K/6-K submit the entire document per arc42 §5.4 — no section targeting applies
        assert target_sections("8-K", "anything") == []
        assert target_sections("6-K", "anything") == []

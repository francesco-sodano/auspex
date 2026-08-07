# Auspex MVP Compliance Assessment

Assessment baseline: 4 August 2026. This is an engineering control assessment, not legal advice, certification, or a declaration of compliance. Applicability depends on the deploying entity, jurisdiction, clients, commercialization, and actual use.

## Intended Use And Classification

Auspex is a closed MVP for authenticated users to research public securities and maintain a manual portfolio ledger. It produces personalized buy, hold, sell, target-weight, and amount suggestions, but cannot place orders, connect to a broker or bank, custody assets, grant credit, price insurance, or make a decision with legal effect.

The current use is not listed as high-risk in Annex III of the EU AI Act. It becomes a different legal use case if adapted for creditworthiness, insurance pricing, employment, access to essential services, or another Annex III purpose. The scientific-research exclusion is not relied on because the application is user-facing and its output can influence investment decisions.

A disclaimer does not determine financial-services classification. Personalized transaction recommendations can constitute investment advice under MiFID II and Swiss FinSA when provided commercially or by a regulated institution. The MVP therefore is not approved for public or production investment-advice use.

Prohibited uses without a new legal classification and control assessment:

- trade execution, discretionary portfolio management, or custody;
- credit, lending, insurance, employment, or eligibility decisions;
- processing special-category or sensitive personal data in advisor notes or discussion;
- deceptive, manipulative, social-scoring, biometric, or emotion-recognition use;
- removal of evidence, coverage, human-decision, or AI-transparency controls.

## Current AI Inventory

| Component | Version | Function | Decision authority | Current controls |
| --- | --- | --- | --- | --- |
| Azure OpenAI text embeddings | `text-embedding-3-large:1` | Evidence vectorization and retrieval | Retrieval only | deployment version pinned with no automatic upgrade; deterministic document IDs and revisions |
| Azure OpenAI GPT-4o | `gpt-4o:2024-11-20` | E7 sentiment extraction | Feature only | immutable cache; evidence quote validation; prompt `e7_sentiment_v2` |
| Azure OpenAI GPT-4o | `gpt-4o:2024-11-20` | E21 narrative extraction | Feature only | newest three documents per security in the active portfolio/theme universe; bounded evidence indexes; immutable cache; prompt `e21_narrative_v1` |
| Azure OpenAI GPT-4o | `gpt-4o:2024-11-20` | Recommendation explanation | Narration only | action and arithmetic supplied by policy; citation validation; immutable decision log; prompt `e16_grounded_v1` |
| Azure OpenAI GPT-4o | `gpt-4o:2024-11-20` | Portfolio discussion | Narration only | owner-scoped context; deterministic calculations; grounded citations; prompt `e18_discussion_v1` |
<!-- BEGIN GENERATED DETERMINISTIC INVENTORY -->
| Deterministic policy | `policy_v2` | Personalized portfolio actions and amounts | Recommendation | risk-profile policy; coverage, raw-composite, financing and cost gates; no execution |
| Deterministic company engine | `company_opportunity_v1` / `fresh_balanced_v1` | Fresh 90-day six-leg company outlook across the research universe | Research classification | compact active windows; per-leg evidence lineage; PIT checks; theme-relative normalization; append-on-change packages |
<!-- END GENERATED DETERMINISTIC INVENTORY -->
| Deterministic models | `e20_v2`, `e22_v4` | Fundamental anchor and narrative premium | Features | content-addressed code; version checks; immutable evidence links |

The UI identifies the discussion as an AI interaction and labels generated explanations and answers with machine-readable `data-ai-generated="true"` markup. This markup is an MVP signal, not a claim that a future Article 50 technical standard has been certified.

## Compliance Matrix

| Framework | Applicability | Implemented evidence | Open MVP gap | Required next action | Owner |
| --- | --- | --- | --- | --- | --- |
| EU AI Act, Articles 4 and 50 | Applicable to provider/deployer roles for this AI system; current use is not Annex III high-risk | direct-interaction disclosure; generated-output labels; pinned model/prompt versions; deterministic recommendation boundary; evidence validation | no formal AI literacy record, accountable model owner, periodic risk review, or approved machine-readable marking standard | maintain AI inventory and change log; assign owner; train operators; test disclosure on every generated surface; reassess on every intended-purpose change | provider and deployer |
| EU AI Act high-risk controls | Not mandatory for current intended use; useful voluntary baseline under Article 95 | PIT data, logs, replay controls, validation, human choice, withheld states | no conformity assessment, FRIA, QMS, post-market plan, or EU registration | do not represent the MVP as high-risk compliant; perform classification before each new use; implement full Chapter III controls if use enters Annex III | provider |
| GDPR and Swiss FADP/DPO | Applies to identity, investor profile, ledger, decisions, discussion, and telemetry when territorial scope is met | authentication; owner partitioning; server-controlled owner IDs; managed identity; Key Vault; versioned acknowledgements; encryption by Azure services; log-retention parameters | repository has no deployer-specific privacy notice, legal-basis record, ROPA, DPIA, retention schedule, data export/erasure workflow, transfer assessment, or breach playbook | before real users: name controller/processor roles; approve notice and legal bases; complete ROPA/DPIA; define retention; add tested access/export/rectification/erasure handling; document subprocessors and transfers | deploying controller |
| MiFID II, Articles 24-25 | Applies when an investment firm provides these personalized recommendations as investment advice | no execution; cost estimate; deterministic rationale; evidence; immutable recommendation/disposition history; user chooses all action | coarse risk profile and horizon are not a suitability assessment; no client classification, knowledge/experience, financial situation/loss capacity, objectives, target-market review, suitability statement, conflicts/inducements process, complaint route, or adviser competence record | keep public/commercial personalized recommendations disabled until the regulated firm implements and validates the full advice journey and records | regulated deployer |
| Swiss FinSA, Articles 3-16 and 21-27 | Personalized portfolio-aware recommendations can be investment advice when offered commercially in or into Switzerland | risk and horizon acknowledgement; recommendation rationale; no execution; records of suggestions and user dispositions | missing client segmentation, appropriateness/suitability data, per-recommendation needs/grounds record suitable for FinSA, provider/supervisory/ombudsman disclosures, adviser registration analysis, conflicts and third-party compensation controls | legal classification by Swiss counsel; for regulated use implement FinSA client journey, documentation, document access, complaint/ombudsman, staff competence and conflicts controls | Swiss deployer |
| FINMA Guidance 08/2024 on AI | Direct supervisory expectation for FINMA-supervised deployers; proportional control benchmark otherwise | versioned inventory above; model/prompt provenance; immutable cache and decision logs; data-quality gates; grounded outputs; deterministic decision boundary | no board-approved AI policy, accountable owner, risk classification, independent validation, bias/performance thresholds, change approval, staff training, incident taxonomy, or third-party AI register | adopt proportionate AI governance; validate each use case before activation; define monitoring, fallback, change and incident procedures; retain evidence | FINMA-supervised deployer |
| FINMA operational risk, resilience and outsourcing expectations | Applies to supervised Swiss deployment and cloud outsourcing | IaC; managed identity; monitoring and alerts; capacity guard; deterministic recovery tools; tests | no deployer BIA, RTO/RPO approval, tested DR, outsourcing inventory, audit/access terms, concentration assessment, exit test, or regulatory notification analysis | classify criticality; complete outsourcing/cloud assessment; approve resilience and incident plans; test restore and exit; align contracts and register | FINMA-supervised deployer |
| DORA | Applies to EU financial entities using Auspex, not automatically to this repository as a standalone prototype | CI checks; Application Insights; managed identities; source/release reconciliation; dependency-pinned builds | no entity-level ICT framework, asset/third-party register, incident classification/reporting, annual resilience program, BCP/DR evidence, contractual register, or exit strategy | deploying financial entity must onboard Auspex into DORA governance, testing, incident, third-party and contract processes before critical/important use | EU financial entity |
| Consumer, marketing, accessibility and record rules | Can apply to retail distribution in the EU/Switzerland | research-only and no-trade disclosures; source links; authenticated decision history; accessible AI notice text | no approved terms/privacy/risk documents in repository, complaints workflow, jurisdiction-specific marketing review, or formal WCAG/EAA evidence | deployer supplies approved documents and contact routes; perform accessibility and fair/clear/not-misleading review before external release | deploying provider |

## MVP Remediation Plan

### P0: Before any external pilot

1. Freeze the intended purpose above and designate the system as a non-production, no-execution research MVP.
2. Assign a product owner, AI risk owner, privacy owner, security owner, and regulated-conduct owner.
3. Replace placeholder legal acknowledgements with deploying-entity-approved terms, privacy notice, risk disclosure, controller contact, complaint contact, and versioning.
4. Complete an AI risk assessment and GDPR/FADP ROPA and DPIA for the actual tenant, users, regions, subprocessors, transfers, retention, and telemetry.
5. Decide the financial-services boundary. For any bank or commercial pilot, disable personalized recommendations unless the deployer has implemented the applicable MiFID/FinSA advice controls.
6. Prohibit sensitive data in free text, document the control, and train pilot users and operators.

### P1: During a controlled MVP pilot

1. Add authenticated data access/export, profile correction, account closure/erasure, retention execution, and auditable request handling.
2. Add an operator AI register/change log tied to model, prompt, policy, data, test result, approval, deployment, incident, and rollback versions.
3. Define acceptance thresholds for grounding, invalid citations, withheld output, freshness, recommendation stability, and owner isolation; review them on every release.
4. Create privacy/security/AI incident triage with legal reporting clocks, evidence preservation, communication ownership, and post-incident review.
5. Record third-party services, data locations, contracts, subprocessors, concentration, contingency, audit rights, and exit procedures.
6. Perform accessibility, threat-model, restore, tenant-isolation, prompt-injection, data-leakage, and model-output evaluations with retained results.

### P2: Production-only gates

1. Obtain jurisdiction-specific legal approval, regulatory/licensing analysis, and documented product governance.
2. Implement full suitability/appropriateness, client classification, cost/conflict/inducement disclosure, suitability reports, durable records, adviser competence, complaints and ombudsman integration where applicable.
3. Complete the deploying institution's DORA or FINMA operational-resilience and outsourcing approval, including tested BCP/DR and exit.
4. Establish independent model validation, periodic monitoring, bias and performance review, AI literacy, management reporting, and internal audit.
5. Add production network controls, penetration testing, recovery objectives, capacity/availability design, and service-level monitoring appropriate to criticality.

## Primary Sources

- EU AI Act, consolidated 27 July 2026: https://eur-lex.europa.eu/legal-content/EN/TXT/?uri=CELEX:02024R1689-20260727
- European Commission Article 50 guidelines, 20 July 2026: https://digital-strategy.ec.europa.eu/en/library/guidelines-transparency-obligations-providers-and-deployers-ai-systems
- GDPR: https://eur-lex.europa.eu/eli/reg/2016/679/oj
- DORA: https://eur-lex.europa.eu/eli/reg/2022/2554/oj
- MiFID II, consolidated 6 June 2026: https://eur-lex.europa.eu/legal-content/EN/TXT/?uri=CELEX:02014L0065-20260606
- Swiss FinSA: https://www.fedlex.admin.ch/eli/cc/2019/758/en
- Swiss FADP: https://www.fedlex.admin.ch/eli/cc/2022/491/en
- Swiss Data Protection Ordinance: https://www.fedlex.admin.ch/eli/cc/2022/568/en
- FINMA Guidance 08/2024, Governance and risk management when using AI: https://www.finma.ch/en/~/media/finma/dokumente/dokumentencenter/myfinma/4dokumentation/finma-aufsichtsmitteilungen/20241218-finma-aufsichtsmitteilung-08-2024.pdf

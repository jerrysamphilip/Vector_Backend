# app/db/seed_blueprints.py
"""
Seed script for Persona Blueprint intelligence cards.

Blueprint fields are repurposed as REASONING CONTEXT for the LLM, NOT example copy:
  - openers      → role frustrations (what this person actually struggles with day-to-day)
  - value_angles → change signals (what types of improvement matter to this persona)
  - ctas         → CTA guidance notes (2 rule lines — NOT example sentences to copy)
  - proof_points → evidence types (what kind of proof resonates with this persona)
  - tone_rules   → expanded persona intelligence:
                     persona_context, language_use, language_avoid, what_resonates, style, avoid

The LLM uses these fields to REASON about the person's world, not to copy phrases.
The 'openers' field name is kept for DB compatibility but no longer stores example opener sentences.
"""

import os
import sys

_CURRENT_DIR = os.path.dirname(__file__)
_PROJECT_ROOT = os.path.abspath(os.path.join(_CURRENT_DIR, "..", ".."))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from sqlalchemy.orm import Session
from app.models import PersonaBlueprint, ProspectPersona

DEFAULT_BLUEPRINTS = [
    {
        "persona_type": "PATIENT_SERVICES_HUB",
        "openers": [
            # Role frustrations — what hub/patient-services staff actually struggle with day-to-day
            "Patient enrollment forms stuck in fax queues while therapy start dates slip",
            "Benefits investigation turnaround that patients feel as delayed access to therapy, not a back-office metric",
            "Case managers juggling five different payer portals to answer one coverage question",
            "Hub performance reports that show volume but not where patients actually drop off",
        ],
        "value_angles": [
            # Change signals — what types of improvement matter to this persona
            "faster benefits investigation turnaround",
            "patient drop-off point visibility across the hub journey",
            "reduced manual case management workload",
            "reimbursement and copay approval speed",
        ],
        "ctas": [
            "interest question only — binary yes/no",
            "under 6 words, no booking ask on first touch",
        ],
        "proof_points": [
            # Types of evidence that land with this persona
            "days removed from benefits investigation turnaround with a named workflow fix",
            "% reduction in patient drop-off at a named hub-journey stage",
        ],
        "tone_rules": {
            "style": "empathetic-operational",
            "persona_context": (
                "runs or supports a pharma hub / patient services program; KPIs are time-to-therapy, "
                "benefits investigation turnaround, case resolution rate, and patient adherence; "
                "balances patient-impact sensitivity with operational throughput; makes decisions "
                "based on patient outcomes and case-load relief, not abstract efficiency"
            ),
            "language_use": ["hub operations", "benefits investigation", "prior authorization", "case load",
                             "patient access", "reimbursement", "copay", "enrollment", "time-to-therapy"],
            "language_avoid": ["synergy", "leverage", "AI-powered", "best-in-class", "cutting-edge"],
            "what_resonates": (
                "a specific patient-journey friction point (enrollment delay, BI turnaround, case load) "
                "framed in terms of patient impact, not just internal metrics; named mechanism and a "
                "concrete time or percentage improvement"
            ),
            "avoid": ["cold operational language with no patient-impact framing", "generic automation claims",
                      "deep technical jargon"],
        },
    },
    {
        "persona_type": "MARKET_ACCESS",
        "openers": [
            "Payer contract renewals that surface access barriers only after formulary decisions are locked in",
            "Trade and channel data that lives in three systems and never quite agrees with itself",
            "Value dossiers that take months to build and are outdated before the first payer meeting",
            "Reimbursement friction that shows up as slow uptake, not as a line item anyone tracks",
        ],
        "value_angles": [
            "faster payer/value dossier turnaround",
            "trade & channel data reconciliation",
            "formulary and reimbursement friction visibility",
            "commercial operations reporting speed",
        ],
        "ctas": [
            "interest question only — binary yes/no",
            "under 6 words, no booking ask on first touch",
        ],
        "proof_points": [
            "weeks removed from value-dossier or payer-strategy prep with a named data-consolidation approach",
            "uptake or reimbursement friction reduced with a named mechanism and a measurable %",
        ],
        "tone_rules": {
            "style": "strategic-commercial",
            "persona_context": (
                "owns market access, payer strategy, or commercial operations for a brand/franchise; "
                "KPIs are formulary access, time-to-reimbursement, trade & channel accuracy, and commercial "
                "data reliability; makes decisions based on evidence-backed access strategy and cross-payer "
                "consistency; low tolerance for vague 'digital transformation' pitches without a payer-specific angle"
            ),
            "language_use": ["market access", "payer strategy", "value and access", "trade and channel",
                             "reimbursement", "formulary", "commercial operations", "data & analytics"],
            "language_avoid": ["synergy", "leverage", "digital transformation", "AI-powered", "paradigm shift"],
            "what_resonates": (
                "a specific payer or trade/channel data friction point framed as a commercial risk, not "
                "just an IT problem; named mechanism with a measurable access or turnaround improvement"
            ),
            "avoid": ["generic tech buzzwords", "feature lists", "vague ROI claims"],
        },
    },
    {
        "persona_type": "OPERATIONS_PHARMACY",
        "openers": [
            "Prior authorization queues that back up faster than staff can clear them, delaying therapy starts",
            "Specialty pharmacy intake volume that spikes without a matching increase in headcount",
            "Revenue cycle denials that repeat the same root cause every month without anyone fixing the upstream step",
            "Clinical operations staff spending more time on manual reconciliation than on patient-facing work",
        ],
        "value_angles": [
            "prior authorization turnaround reduction",
            "specialty pharmacy intake automation",
            "denial rate reduction at the root cause",
            "clinical operations capacity recovered from manual reconciliation",
        ],
        "ctas": [
            "interest question only — binary yes/no",
            "under 6 words, no booking ask on first touch",
        ],
        "proof_points": [
            "days removed from PA turnaround with a named bot/automation mechanism",
            "% reduction in claim denials tied to a specific upstream fix",
        ],
        "tone_rules": {
            "style": "operational-pragmatic",
            "persona_context": (
                "runs specialty pharmacy or pharmacy operations at scale; KPIs are prior authorization "
                "turnaround, intake throughput, denial rate, and revenue cycle days; deeply hands-on with "
                "day-to-day workflow; makes decisions based on operational relief and measurable throughput "
                "gains, not strategic narratives"
            ),
            "language_use": ["prior authorization", "specialty pharmacy", "intake", "revenue cycle",
                             "clinical operations", "denial rate", "throughput", "dispensing"],
            "language_avoid": ["synergy", "leverage", "digital transformation", "best-in-class", "cutting-edge"],
            "what_resonates": (
                "a specific operational bottleneck (PA backlog, intake spike, denial pattern) with a named "
                "automation fix and a concrete time or throughput number"
            ),
            "avoid": ["executive strategy language", "vague efficiency claims", "feature lists without a workflow tie-in"],
        },
    },
    {
        "persona_type": "TECHNOLOGY_DATA_DIGITAL",
        "openers": [
            "Interoperability projects that stall waiting on a data mapping exercise no one owns end-to-end",
            "Salesforce or CRM instances that were built for one use case and now buckle under five",
            "Data & analytics roadmaps that keep getting reprioritized around the loudest stakeholder, not the highest-impact pipeline",
            "AI pilots that prove value in a sandbox and stall the moment they need production data access",
        ],
        "value_angles": [
            "faster interoperability / data-mapping delivery",
            "CRM (Salesforce/Health Cloud) scalability without a rebuild",
            "data & analytics roadmap throughput",
            "AI pilot-to-production acceleration",
        ],
        "ctas": [
            "interest question only — binary yes/no",
            "under 6 words, no booking ask on first touch",
        ],
        "proof_points": [
            "weeks removed from an interoperability or data-mapping project with a named integration approach",
            "AI pilot moved to production with a named data-access fix and a measurable timeline",
        ],
        "tone_rules": {
            "style": "technical-strategic",
            "persona_context": (
                "owns technology, data, or digital strategy (CIO/CTO/Chief Digital/Chief Data/Chief AI or "
                "engineering leadership); KPIs are system uptime, integration delivery speed, data quality, "
                "and AI/digital initiative velocity; respects technical specificity and vendor teams that "
                "understand healthcare data constraints (HIPAA, interoperability standards); low tolerance "
                "for generic 'AI-powered' claims without a concrete technical mechanism"
            ),
            "language_use": ["interoperability", "data & analytics", "Salesforce", "Health Cloud",
                             "data engineering", "AI", "integration", "HIPAA", "data pipeline"],
            "language_avoid": ["AI-powered", "game-changer", "cutting-edge", "revolutionize", "synergy"],
            "what_resonates": (
                "a specific technical bottleneck (integration delay, data mapping ownership gap, "
                "pilot-to-production friction) with a named mechanism and measurable delivery-time "
                "improvement; credibility markers like HIPAA/compliance awareness"
            ),
            "avoid": ["marketing buzzwords", "vague AI claims", "feature lists without technical depth"],
        },
    },
    {
        "persona_type": "INNOVATION_STRATEGY_PRODUCT",
        "openers": [
            "Innovation roadmaps that look great in a slide deck and stall the moment they need a build partner",
            "Emerging-tech pilots that get greenlit but never get the engineering bandwidth to actually ship",
            "Product strategy decisions that wait on a build-vs-buy analysis nobody has time to run properly",
            "Corporate development priorities that shift faster than any single vendor conversation can keep up with",
        ],
        "value_angles": [
            "innovation pilot-to-build acceleration",
            "build-vs-buy analysis speed for new product initiatives",
            "engineering bandwidth recovered for strategic initiatives",
            "solutions architecture turnaround for new product bets",
        ],
        "ctas": [
            "interest question only — binary yes/no",
            "under 6 words, no booking ask on first touch",
        ],
        "proof_points": [
            "weeks removed from pilot-to-build handoff with a named delivery partner model",
            "a named product or platform initiative shipped faster with a measurable timeline",
        ],
        "tone_rules": {
            "style": "visionary-pragmatic",
            "persona_context": (
                "owns innovation, product, or strategic/corporate development initiatives; KPIs are "
                "initiative velocity, time-to-market for new product bets, and successful pilot-to-scale "
                "conversion; thinks in terms of competitive positioning and build-vs-buy trade-offs; makes "
                "decisions based on speed-to-value and delivery credibility, not feature lists"
            ),
            "language_use": ["innovation", "strategy", "product", "solutions architecture",
                             "emerging technology", "build vs buy", "time-to-market", "platform"],
            "language_avoid": ["synergy", "leverage", "paradigm shift", "world-class", "best-in-class"],
            "what_resonates": (
                "a specific innovation-to-execution gap (pilot stalling, engineering bandwidth, "
                "build-vs-buy delay) with a named delivery model and a measurable time-to-market improvement"
            ),
            "avoid": ["vague innovation buzzwords", "feature lists", "overpromising without a delivery mechanism"],
        },
    },
    {
        "persona_type": "OTHER",
        "openers": [
            "The part of most workflows that takes longest is usually the one that doesn't appear on any checklist",
            "Approval bottlenecks rarely show up in project timelines until a deadline is already missed",
            "Most productivity tools add steps before they save steps — the net gain takes months to materialise",
            "The highest-impact changes in most teams are rarely the biggest ones — they're the ones no one tracked",
        ],
        "value_angles": [
            "approval routing automation",
            "workflow bottleneck visibility before deadline risk",
            "status check replacement with automated signals",
            "coordination overhead reduction",
        ],
        "ctas": [
            "interest question only — binary yes/no",
            "under 6 words, no booking ask on first touch",
        ],
        "proof_points": [
            "cycle time % reduction with a named routing mechanism",
            "hours per week recovered from a specific coordination ritual",
        ],
        "tone_rules": {
            "style": "friendly",
            "persona_context": (
                "professional whose specific function is unclear; likely has operational or cross-functional "
                "responsibilities; KPIs vary but typically include delivery speed, team efficiency, "
                "or stakeholder satisfaction; makes decisions based on practical impact and ease of adoption"
            ),
            "language_use": ["workflow", "process", "approval", "coordination", "delivery", "stakeholder"],
            "language_avoid": ["role-specific jargon", "overly specific claims", "synergy", "leverage"],
            "what_resonates": (
                "broadly relatable operational observation about workflow friction or coordination overhead; "
                "practical impact framing; specific time or cycle improvement"
            ),
            "avoid": ["role assumptions", "overly specific industry claims", "jargon"],
        },
    },
]


def seed_blueprints(db: Session) -> int:
    """
    Seed persona intelligence blueprints if they don't exist, or update existing ones.
    Also retires any persona taxonomy no longer in DEFAULT_BLUEPRINTS: stale
    ProspectPersona classifications are cleared first (so affected prospects get
    reclassified under the current taxonomy next time a campaign classifies their
    list), then the orphaned PersonaBlueprint rows are removed. Both deletes are
    scoped to non-canonical persona_type values, so this is a no-op once the
    taxonomy has fully migrated — safe to run on every startup.
    Returns count of blueprints created or updated.
    """
    canonical_types = {bp["persona_type"] for bp in DEFAULT_BLUEPRINTS}

    stale_personas = db.query(ProspectPersona).filter(
        ProspectPersona.persona_type.notin_(canonical_types)
    ).delete(synchronize_session=False)
    stale_blueprints = db.query(PersonaBlueprint).filter(
        PersonaBlueprint.persona_type.notin_(canonical_types)
    ).delete(synchronize_session=False)
    if stale_personas or stale_blueprints:
        print(f"Retired {stale_personas} stale prospect classifications and {stale_blueprints} stale blueprints")

    created = 0
    updated = 0

    for bp_data in DEFAULT_BLUEPRINTS:
        existing = db.query(PersonaBlueprint).filter(
            PersonaBlueprint.persona_type == bp_data["persona_type"]
        ).first()

        if existing:
            existing.openers = bp_data["openers"]
            existing.value_angles = bp_data["value_angles"]
            existing.ctas = bp_data["ctas"]
            existing.proof_points = bp_data.get("proof_points")
            existing.tone_rules = bp_data.get("tone_rules")
            updated += 1
        else:
            blueprint = PersonaBlueprint(
                persona_type=bp_data["persona_type"],
                openers=bp_data["openers"],
                value_angles=bp_data["value_angles"],
                ctas=bp_data["ctas"],
                proof_points=bp_data.get("proof_points"),
                tone_rules=bp_data.get("tone_rules"),
            )
            db.add(blueprint)
            created += 1

    db.commit()
    print(f"Created {created} new blueprints, updated {updated} existing blueprints")
    return created + updated


if __name__ == "__main__":
    from app.core.database import SessionLocal

    db = SessionLocal()
    try:
        count = seed_blueprints(db)
        print(f"Created/Updated {count} blueprints")
    finally:
        db.close()

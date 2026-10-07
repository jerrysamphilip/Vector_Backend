# app/db/test_email_generation.py
"""
Test script for email generation flow.
Tests: Classification → Blueprint lookup → Email generation
"""

from app.core.database import SessionLocal
from app.models import Prospect, PersonaBlueprint, ProspectPersona
from app.services.ai_email_service import (
    classify_prospect,
    get_or_create_prospect_persona,
    generate_email_from_blueprint
)


def test_classification():
    """Test rule-based classification."""
    print("\n" + "="*50)
    print("TEST 1: Classification")
    print("="*50)
    
    test_cases = [
        ("VP Patient Services", "PATIENT_SERVICES_HUB"),
        ("Hub Operations Director", "PATIENT_SERVICES_HUB"),
        ("Director Market Access", "MARKET_ACCESS"),
        ("Chief Commercial Officer", "MARKET_ACCESS"),
        ("Director of Pharmacy", "OPERATIONS_PHARMACY"),
        ("Prior Authorization Specialist", "OPERATIONS_PHARMACY"),
        ("Chief Data Officer", "TECHNOLOGY_DATA_DIGITAL"),
        ("CIO", "TECHNOLOGY_DATA_DIGITAL"),
        ("VP Product", "INNOVATION_STRATEGY_PRODUCT"),
        ("Chief Innovation Officer", "INNOVATION_STRATEGY_PRODUCT"),
        ("Office Assistant", "OTHER"),
    ]
    
    for designation, expected in test_cases:
        result, confidence = classify_prospect(designation)
        status = "✓" if result == expected else "✗"
        print(f"{status} '{designation}' → {result} ({confidence:.0%})")


def test_email_generation():
    """Test full email generation with mock prospect."""
    print("\n" + "="*50)
    print("TEST 2: Email Generation")
    print("="*50)
    
    db = SessionLocal()
    
    try:
        # Get TECHNOLOGY_DATA_DIGITAL blueprint
        blueprint = db.query(PersonaBlueprint).filter(
            PersonaBlueprint.persona_type == "TECHNOLOGY_DATA_DIGITAL"
        ).first()

        if not blueprint:
            print("✗ No TECHNOLOGY_DATA_DIGITAL blueprint found!")
            return
        
        print(f"✓ Blueprint loaded: {blueprint.persona_type}")
        print(f"  - Openers: {len(blueprint.openers)} options")
        print(f"  - Value angles: {len(blueprint.value_angles)} options")
        print(f"  - CTAs: {len(blueprint.ctas)} options")
        
        # Create mock prospect (in memory, not saved)
        class MockProspect:
            prospect_id = "test-123"
            first_name = "John"
            last_name = "Doe"
            email = "john@example.com"
            company_name = "TechCorp"
            designation = "Senior Backend Engineer"
        
        prospect = MockProspect()
        
        # Generate email
        email = generate_email_from_blueprint(
            blueprint=blueprint,
            prospect=prospect,
            product_name="OutreachAI"
        )
        
        print(f"\n✓ Email Generated:")
        print("-" * 40)
        print(f"Subject: {email['subject']}")
        print(f"\n{email['body']}")
        print("-" * 40)
        print(f"Tone: {email['tone']}")
        print(f"Opener used: {email['opener_used'][:50]}...")
        print(f"CTA used: {email['cta_used']}")
        
    finally:
        db.close()


def test_all_blueprints():
    """Test that all 6 blueprints are present."""
    print("\n" + "="*50)
    print("TEST 3: All Blueprints")
    print("="*50)
    
    db = SessionLocal()
    
    try:
        blueprints = db.query(PersonaBlueprint).filter(
            PersonaBlueprint.is_active == True
        ).all()
        
        print(f"Total blueprints: {len(blueprints)}")
        
        for bp in blueprints:
            print(f"  ✓ {bp.persona_type}: {len(bp.openers)} openers, {len(bp.ctas)} CTAs")
        
        if len(blueprints) == 6:
            print("\n✓ All 6 blueprints present!")
        else:
            print(f"\n✗ Expected 6 blueprints, found {len(blueprints)}")
            
    finally:
        db.close()


if __name__ == "__main__":
    print("\n" + "="*60)
    print("   AI EMAIL GENERATOR - FULL TEST SUITE")
    print("="*60)
    
    test_classification()
    test_all_blueprints()
    test_email_generation()
    
    print("\n" + "="*60)
    print("   ALL TESTS COMPLETE")
    print("="*60 + "\n")

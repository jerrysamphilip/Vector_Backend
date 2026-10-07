# app/db/test_full_flow.py
"""
Full Flow Test: Prospect → Persona → LLM Email Generation
Tests the complete pipeline with a real database prospect.
"""

from app.core.database import SessionLocal
from app.models import Prospect, PersonaBlueprint, ProspectPersona
from app.services.ai_email_service import (
    classify_prospect,
    get_or_create_prospect_persona,
    generate_email_hybrid,
    openai_client
)


def test_full_flow():
    """Test the complete flow: Prospect → Persona → Email."""
    
    print("\n" + "="*70)
    print("   FULL FLOW TEST: Prospect → Persona → Email Generation")
    print("="*70)
    
    db = SessionLocal()
    
    try:
        # =====================================================
        # STEP 1: Get a real prospect from database
        # =====================================================
        print("\n📋 STEP 1: Loading prospect from database...")
        
        prospect = db.query(Prospect).filter(
            Prospect.designation != None,
            Prospect.first_name != None
        ).first()
        
        if not prospect:
            print("❌ No prospects found in database!")
            print("   Please seed some prospects first.")
            return
        
        print(f"   ✓ Found prospect:")
        print(f"     - Name: {prospect.first_name} {prospect.last_name}")
        print(f"     - Email: {prospect.email}")
        print(f"     - Designation: {prospect.designation}")
        print(f"     - Company: {prospect.company_name}")
        print(f"     - Industry: {prospect.industry}")
        
        # =====================================================
        # STEP 2: Classify and create persona
        # =====================================================
        print("\n🧠 STEP 2: Creating/Getting prospect persona...")
        
        # First, show what the classifier thinks
        persona_type, confidence = classify_prospect(
            designation=prospect.designation or "",
            company_name=prospect.company_name
        )
        print(f"   ✓ Classification: {persona_type} (confidence: {confidence:.0%})")
        
        # Get or create the persona in database
        persona = get_or_create_prospect_persona(db, prospect)
        print(f"   ✓ Persona saved: {persona.persona_type}")
        print(f"     - Persona ID: {persona.persona_id[:8]}...")
        print(f"     - Blueprint linked: {persona.blueprint_id[:8] if persona.blueprint_id else 'None'}...")
        
        # =====================================================
        # STEP 3: Generate email using hybrid method
        # =====================================================
        print("\n📧 STEP 3: Generating email...")
        
        if openai_client:
            print("   Using: GPT-4o-mini (LLM mode)")
        else:
            print("   Using: Blueprint mode (no API key)")
        
        result = generate_email_hybrid(
            db=db,
            prospect=prospect,
            product_name="OutreachAI",
            product_description="AI-powered email outreach platform that automates personalization and improves reply rates",
            use_llm=True
        )
        
        if "error" in result:
            print(f"   ❌ Error: {result['error']}")
            return
        
        print(f"   ✓ Email generated!")
        print(f"     - Method: {result.get('generation_method', 'N/A')}")
        print(f"     - Persona: {result.get('persona_type', 'N/A')}")
        if 'tokens_used' in result:
            print(f"     - Tokens: {result['tokens_used']}")
        
        # =====================================================
        # STEP 4: Display the generated email
        # =====================================================
        print("\n" + "="*70)
        print("   GENERATED EMAIL")
        print("="*70)
        print(f"\n📬 TO: {prospect.email}")
        print(f"📌 SUBJECT: {result.get('subject', 'N/A')}")
        print("-"*70)
        print(result.get('body', 'No body generated'))
        print("-"*70)
        
        # =====================================================
        # Summary
        # =====================================================
        print("\n" + "="*70)
        print("   ✅ FULL FLOW TEST COMPLETE")
        print("="*70)
        print(f"""
Summary:
  - Prospect: {prospect.first_name} {prospect.last_name}
  - Persona: {persona.persona_type} ({confidence:.0%} confidence)
  - Email Method: {result.get('generation_method', 'N/A')}
  - Tokens Used: {result.get('tokens_used', 'N/A')}
""")
        
    finally:
        db.close()


if __name__ == "__main__":
    test_full_flow()

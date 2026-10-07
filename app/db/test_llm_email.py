# app/db/test_llm_email.py
"""
Test LLM-powered email generation with real OpenAI API call.
"""

from app.core.database import SessionLocal
from app.models import PersonaBlueprint
from app.services.ai_email_service import (
    generate_email_with_llm,
    openai_client
)


def test_llm_generation():
    """Test LLM email generation with real API call."""
    
    print("\n" + "="*60)
    print("   LLM EMAIL GENERATION TEST (GPT-4o-mini)")
    print("="*60)
    
    # Check if OpenAI client is available
    if not openai_client:
        print("\n❌ OPENAI_API_KEY not set in .env!")
        print("   Please add: OPENAI_API_KEY=sk-your-key")
        return
    
    print("\n✓ OpenAI client initialized")
    
    db = SessionLocal()
    
    try:
        # Get TECHNOLOGY_DATA_DIGITAL blueprint
        blueprint = db.query(PersonaBlueprint).filter(
            PersonaBlueprint.persona_type == "TECHNOLOGY_DATA_DIGITAL"
        ).first()

        if not blueprint:
            print("❌ No TECHNOLOGY_DATA_DIGITAL blueprint found!")
            return

        print(f"✓ Blueprint loaded: {blueprint.persona_type}")

        # Test prospect data
        prospect_data = {
            "first_name": "Pranav",
            "designation": "Chief Data Officer",
            "company_name": "PharmaCorp Specialty",
            "industry": "specialty pharmacy"
        }
        
        print(f"\n📧 Generating email for: {prospect_data['first_name']} ({prospect_data['designation']})")
        print("-" * 50)
        
        # Call LLM
        result = generate_email_with_llm(
            prospect_data=prospect_data,
            blueprint=blueprint,
            product_name="OutreachAI",
            product_description="AI-powered email outreach platform that automates personalization"
        )
        
        if result:
            print(f"\n✓ Email Generated Successfully!")
            print(f"  Model: {result.get('model_used', 'N/A')}")
            print(f"  Tokens: {result.get('tokens_used', 'N/A')}")
            print("\n" + "="*50)
            print(f"SUBJECT: {result['subject']}")
            print("="*50)
            print(result['body'])
            print("="*50)
        else:
            print("❌ LLM generation failed!")
            
    finally:
        db.close()
    
    print("\n" + "="*60)
    print("   TEST COMPLETE")
    print("="*60 + "\n")


if __name__ == "__main__":
    test_llm_generation()

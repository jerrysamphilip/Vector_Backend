# app/prompts/__init__.py
"""
Prompts module for AI email generation.

Each prompt type has its own file:
- email_generation.py: Base email generation prompts
- llm_system_prompts.py: System prompts for LLM-based generation
"""

from app.prompts.email_generation import (
    get_system_prompt,
    get_email_generation_prompt,
    get_persona_inference_prompt,
)

from app.prompts.llm_system_prompts import (
    get_email_generation_system_prompt,
    get_sequence_generation_system_prompt,
    get_persona_classification_prompt,
    get_subject_line_prompt,
    get_conference_email_system_prompt,
    get_conference_sequence_system_prompt,
)

from app.prompts.email_examples import (
    get_few_shot_examples_block,
    is_healthcare_pharma,
    build_persona_intelligence_block,
    get_capability_pool_block,
    get_sequence_tone_block,
    get_step_tone_block,
    get_conference_few_shot_examples_block,
    get_conference_sequence_tone_block,
    get_conference_step_tone_block,
)


__all__ = [
    # Base email generation
    "get_system_prompt",
    "get_email_generation_prompt",
    "get_persona_inference_prompt",

    # LLM prompts — cold outreach
    "get_email_generation_system_prompt",
    "get_sequence_generation_system_prompt",
    "get_persona_classification_prompt",
    "get_subject_line_prompt",

    # LLM prompts — conference / in-person outreach
    "get_conference_email_system_prompt",
    "get_conference_sequence_system_prompt",

    # Few-shot examples + industry helpers — cold outreach
    "get_few_shot_examples_block",
    "is_healthcare_pharma",
    "build_persona_intelligence_block",
    "get_capability_pool_block",
    "get_sequence_tone_block",
    "get_step_tone_block",

    # Few-shot examples + tone helpers — conference outreach
    "get_conference_few_shot_examples_block",
    "get_conference_sequence_tone_block",
    "get_conference_step_tone_block",
]

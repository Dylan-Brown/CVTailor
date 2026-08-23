#!/usr/bin/env python3
"""
py_stage08_reviewer_gemini.py -- the "Gemini" Agent backend for stage 8.
See _agent_reviewer_common.py for the actual driver logic. Needs
GEMINI_API_KEY regardless of your default $CVTAILOR_LLM_PROVIDER.
"""
from _agent_reviewer_common import agent_reviewer_main

if __name__ == "__main__":
    agent_reviewer_main(
        generation_provider="gemini",
        description="Gemini Agent backend for stage 8 -- see _agent_reviewer_common.py.",
    )

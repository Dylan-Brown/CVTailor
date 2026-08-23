#!/usr/bin/env python3
"""
py_stage08_reviewer_local.py -- the "Local" Agent backend for stage 8.
See _agent_reviewer_common.py for the actual driver logic. Uses LM
Studio by default -- see $LMSTUDIO_BASE_URL to point it elsewhere.
"""

from _agent_reviewer_common import agent_reviewer_main

if __name__ == "__main__":
    agent_reviewer_main(
        generation_provider="lmstudio",
        description="Local/LM Studio Agent backend for stage 8 -- see _agent_reviewer_common.py.",
    )

"""ADK agent definition (the escalation path).

The model runs through ADK's LiteLlm wrapper so we can use Claude. Override with
the EDCA_MODEL env var (e.g. "anthropic/claude-opus-4-8", or a Gemini id for
native ADK). ANTHROPIC_API_KEY (or Vertex creds) must be present at runtime.
"""
from __future__ import annotations

import os

from google.adk.agents import LlmAgent
from google.adk.models.lite_llm import LiteLlm

from edca.agent.prompts import SYSTEM_INSTRUCTION
from edca.client.control_center import ControlCenterClient
from edca.agent.tools import build_tools

DEFAULT_MODEL = "anthropic/claude-opus-4-8"


def build_agent(client: ControlCenterClient, model: str | None = None) -> LlmAgent:
    """Construct the recovery agent with tools bound to a control-center client."""
    model_id = model or os.environ.get("EDCA_MODEL", DEFAULT_MODEL)
    return LlmAgent(
        name="edw_recovery_agent",
        model=LiteLlm(model=model_id),
        instruction=SYSTEM_INSTRUCTION,
        tools=build_tools(client),
    )

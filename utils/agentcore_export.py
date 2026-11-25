# Copyright OpenSearch Contributors
# SPDX-License-Identifier: Apache-2.0
"""
Export OSCAR agent definitions as JSON for the AgentCore runtime.

The AgentCore container cannot import agents/* (those packages import aws_cdk),
so this module runs on the CDK side and writes everything the container needs
into plain JSON files. It reads the same methods the Classic stack reads, so
both systems get identical prompts, tools and access levels.
"""

import importlib
import json
from pathlib import Path
from typing import Any, Dict, List

from agents.supervisor_prompts import (LIMITED_SUPERVISOR_INSTRUCTION,
                                       PRIVILEGED_SUPERVISOR_ACTION_GROUPS,
                                       PRIVILEGED_SUPERVISOR_INSTRUCTION)
from utils.foundation_models import FoundationModels

SUPERVISOR_MODEL = FoundationModels.CLAUDE_4_5_SONNET.value


def _action_groups(agent_name: str) -> List[Dict[str, Any]]:
    """Return the agent's tool groups in one shape: [{name, description, functions}]."""
    specs = importlib.import_module(f"agents.{agent_name}.tool_specs")
    if hasattr(specs, "ACTION_GROUPS"):
        return specs.ACTION_GROUPS
    return [{
        "name": specs.ACTION_GROUP_NAME,
        "description": specs.ACTION_GROUP_DESCRIPTION,
        "functions": specs.FUNCTIONS,
    }]


def build_agent_spec(agent) -> Dict[str, Any]:
    """Everything the AgentCore runtime needs to rebuild one specialist."""
    return {
        "name": agent.name,
        "model_id": agent.get_foundation_model(),
        "instruction": agent.get_agent_instruction(),
        "collaborator_name": agent.get_collaborator_name(),
        "collaborator_instruction": agent.get_collaborator_instruction(),
        "access_level": agent.get_access_level(),
        "uses_knowledge_base": agent.uses_knowledge_base(),
        "action_groups": _action_groups(agent.name),
    }


def build_supervisor_specs() -> Dict[str, Any]:
    """Prompts, model and own tools for the privileged and limited supervisors."""
    return {
        "privileged": {
            "model_id": SUPERVISOR_MODEL,
            "instruction": PRIVILEGED_SUPERVISOR_INSTRUCTION,
            "action_groups": PRIVILEGED_SUPERVISOR_ACTION_GROUPS,
        },
        "limited": {
            "model_id": SUPERVISOR_MODEL,
            "instruction": LIMITED_SUPERVISOR_INSTRUCTION,
            "action_groups": [],
        },
    }


def write_specs(agents: List[Any], out_dir: Path) -> List[Path]:
    """Write one JSON file per specialist plus supervisors.json into out_dir."""
    out_dir.mkdir(parents=True, exist_ok=True)
    written = []
    for agent in agents:
        path = out_dir / f"{agent.name}.json"
        path.write_text(json.dumps(build_agent_spec(agent), indent=2) + "\n")
        written.append(path)
    path = out_dir / "supervisors.json"
    path.write_text(json.dumps(build_supervisor_specs(), indent=2) + "\n")
    written.append(path)
    return written

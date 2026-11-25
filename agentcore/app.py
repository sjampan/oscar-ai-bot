# Copyright OpenSearch Contributors
# SPDX-License-Identifier: Apache-2.0
"""
OSCAR on Bedrock AgentCore Runtime.

Replaces the Classic supervisor/collaborator agents with Strands agents:
a supervisor per access tier (privileged / limited) that calls each specialist
(Jenkins, metrics, GitHub, SecurityAdvisories) as a tool. Prompts and tools come
from agentcore/specs/*.json, exported from the same code the Classic stack uses.

Request payload (sent by the Slack bot's agent_invoker):
    {"prompt": "<user query>", "session_attributes": {"access_tier": "...", ...}}
Response:
    {"text": "<answer>", "needs_confirmation": <bool>}
"""

import json
import logging
import os
import threading
import uuid
from collections import OrderedDict
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple, cast

from agentcore.tools import (ToolFunction, load_specs, make_action_tool,
                             make_kb_tool)

logger = logging.getLogger(__name__)

TIERS = ("privileged", "limited")
DEFAULT_TIER = "limited"  # unknown or missing tier gets the least access
MAX_PROMPT_CHARS = 20_000
MAX_CACHED_SESSIONS = 100
DEFAULT_MAX_TOKENS = 4096
SPECIALIST_GUARDRAIL = {"github"}  # Classic attached the guardrail only to this specialist

AgentFactory = Callable[..., Any]
ModelFactory = Callable[[str, bool], Any]


class OscarRuntime:
    """Builds and runs the OSCAR supervisors and specialists for each request."""

    def __init__(
        self,
        specs_dir: Path,
        lambda_arns: Dict[str, str],
        knowledge_base_id: str,
        lambda_client: Any,
        agent_runtime_client: Any,
        model_factory: ModelFactory,
        agent_factory: AgentFactory,
        tool_factory: Callable[[Dict[str, Any], ToolFunction], Any],
    ) -> None:
        self.specialists, self.supervisors = load_specs(specs_dir)
        self.lambda_arns = lambda_arns
        self.knowledge_base_id = knowledge_base_id
        self.lambda_client = lambda_client
        self.agent_runtime_client = agent_runtime_client
        self.model_factory = model_factory
        self.agent_factory = agent_factory
        self.tool_factory = tool_factory
        self._sessions: "OrderedDict[Tuple[str, str], Tuple[Any, threading.Lock]]" = OrderedDict()
        self._sessions_lock = threading.Lock()

    # ------------------------------------------------------------------ tools
    def _action_tools(self, action_groups: List[Dict[str, Any]], function_arn: str) -> List[Any]:
        return [
            self.tool_factory(*make_action_tool(self.lambda_client, function_arn, group["name"], fn))
            for group in action_groups
            for fn in group["functions"]
        ]

    def _kb_tool(self) -> Any:
        return self.tool_factory(*make_kb_tool(self.agent_runtime_client, self.knowledge_base_id))

    def _specialist_tool(self, spec: Dict[str, Any]) -> Any:
        """Wrap one specialist agent as a tool the supervisor can call."""
        name = spec["collaborator_name"]
        tool_spec = {
            "name": name,
            "description": spec["collaborator_instruction"],
            "inputSchema": {"json": {
                "type": "object",
                "properties": {"request": {
                    "type": "string",
                    "description": "The task for this specialist, including any details from the conversation it needs.",
                }},
                "required": ["request"],
            }},
        }

        def tool_function(tool_use: Dict[str, Any], **invocation_state: Any) -> Dict[str, Any]:
            request = str(tool_use.get("input", {}).get("request", "")).strip()
            if not request:
                return {"toolUseId": tool_use["toolUseId"], "status": "error",
                        "content": [{"text": "A non-empty request is required."}]}
            # A fresh dict: Strands writes invocation_state["agent"], which must not
            # overwrite the supervisor's entry.
            child_state = {
                "session_attributes": invocation_state.get("session_attributes", {}),
                "oscar_ctx": invocation_state.get("oscar_ctx", {}),
            }
            try:
                answer = self._build_specialist(spec)(request, invocation_state=child_state)
            except Exception as exc:  # report to the supervisor instead of failing the request
                logger.exception("Specialist %s failed", name)
                return {"toolUseId": tool_use["toolUseId"], "status": "error",
                        "content": [{"text": f"{name} failed: {exc}"}]}
            return {"toolUseId": tool_use["toolUseId"], "status": "success",
                    "content": [{"text": str(answer).strip()}]}

        tool_function.__name__ = name
        return self.tool_factory(tool_spec, tool_function)

    # ----------------------------------------------------------------- agents
    def _build_specialist(self, spec: Dict[str, Any]) -> Any:
        tools = self._action_tools(spec["action_groups"], self.lambda_arns[spec["name"]])
        if spec["uses_knowledge_base"]:
            tools.append(self._kb_tool())
        model = self.model_factory(spec["model_id"], spec["name"] in SPECIALIST_GUARDRAIL)
        return self.agent_factory(model=model, system_prompt=spec["instruction"], tools=tools)

    def _build_supervisor(self, tier: str) -> Any:
        supervisor = self.supervisors[tier]
        tools = [
            self._specialist_tool(spec)
            for spec in self.specialists
            if spec["access_level"] in (tier, "both")
        ]
        tools.append(self._kb_tool())
        tools += self._action_tools(supervisor["action_groups"], self.lambda_arns["communication"]) \
            if supervisor["action_groups"] else []
        model = self.model_factory(supervisor["model_id"], True)
        return self.agent_factory(model=model, system_prompt=supervisor["instruction"], tools=tools)

    def _supervisor_for(self, session_id: str, tier: str) -> Tuple[Any, threading.Lock]:
        """One supervisor per (session, tier), so a session never mixes tiers' history."""
        key = (session_id, tier)
        with self._sessions_lock:
            if key in self._sessions:
                self._sessions.move_to_end(key)
                return self._sessions[key]
            entry = (self._build_supervisor(tier), threading.Lock())
            self._sessions[key] = entry
            while len(self._sessions) > MAX_CACHED_SESSIONS:
                self._sessions.popitem(last=False)
            return entry

    # ---------------------------------------------------------------- request
    def handle(self, payload: Dict[str, Any], session_id: Optional[str]) -> Dict[str, Any]:
        prompt = payload.get("prompt")
        if not isinstance(prompt, str) or not prompt.strip():
            return {"error": "prompt must be a non-empty string"}
        if len(prompt) > MAX_PROMPT_CHARS:
            return {"error": f"prompt exceeds {MAX_PROMPT_CHARS} characters"}
        raw_attrs = payload.get("session_attributes") or {}
        if not isinstance(raw_attrs, dict):
            return {"error": "session_attributes must be an object"}
        session_attributes = {str(k): str(v) for k, v in raw_attrs.items()}
        tier = session_attributes.get("access_tier")
        if tier not in TIERS:
            tier = DEFAULT_TIER
            session_attributes["access_tier"] = tier

        supervisor, lock = self._supervisor_for(session_id or f"session-{uuid.uuid4().hex}", tier)
        oscar_ctx = {"needs_confirmation": False}
        with lock:  # one request at a time per conversation
            result = supervisor(prompt, invocation_state={
                "session_attributes": session_attributes,
                "oscar_ctx": oscar_ctx,
            })
        return {"text": str(result).strip(), "needs_confirmation": oscar_ctx["needs_confirmation"]}


# ---------------------------------------------------------------------- wiring
def runtime_from_env() -> OscarRuntime:
    """Build the runtime from environment variables set by the AgentCore CDK stack."""
    import boto3
    from strands import Agent
    from strands.models.bedrock import BedrockModel
    from strands.tools.tools import PythonAgentTool
    from strands.types.tools import ToolFunc, ToolSpec

    region = os.environ["AWS_REGION"]
    max_tokens = int(os.environ.get("OSCAR_MAX_TOKENS", DEFAULT_MAX_TOKENS))
    guardrail_id = os.environ.get("GUARDRAIL_ID")
    guardrail_version = os.environ.get("GUARDRAIL_VERSION")

    def model_factory(model_id: str, with_guardrail: bool) -> Any:
        config: Dict[str, Any] = {"model_id": model_id, "max_tokens": max_tokens}
        if with_guardrail and guardrail_id and guardrail_version:
            config.update(guardrail_id=guardrail_id, guardrail_version=guardrail_version)
        return BedrockModel(region_name=region, **config)

    def agent_factory(**kwargs: Any) -> Any:
        return Agent(callback_handler=None, **kwargs)

    return OscarRuntime(
        specs_dir=Path(os.environ.get("OSCAR_SPECS_DIR", Path(__file__).parent / "specs")),
        lambda_arns=json.loads(os.environ["OSCAR_LAMBDA_ARNS"]),
        knowledge_base_id=os.environ["KNOWLEDGE_BASE_ID"],
        lambda_client=boto3.client("lambda", region_name=region),
        agent_runtime_client=boto3.client("bedrock-agent-runtime", region_name=region),
        model_factory=model_factory,
        agent_factory=agent_factory,
        tool_factory=lambda spec, fn: PythonAgentTool(spec["name"], cast(ToolSpec, spec), cast(ToolFunc, fn)),
    )


def main() -> None:
    """Container entry point: serve /ping and /invocations for AgentCore Runtime."""
    from bedrock_agentcore.runtime import BedrockAgentCoreApp

    logging.basicConfig(level=logging.INFO)
    runtime = runtime_from_env()
    app = BedrockAgentCoreApp()

    @app.entrypoint
    def invoke(payload: Dict[str, Any], context: Any) -> Dict[str, Any]:
        return runtime.handle(payload, context.session_id)

    # Binds 127.0.0.1 outside a container and 0.0.0.0 inside one; AgentCore routes
    # traffic to the container internally and publishes no port.
    app.run()


if __name__ == "__main__":
    main()

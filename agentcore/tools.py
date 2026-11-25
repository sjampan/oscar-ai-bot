# Copyright OpenSearch Contributors
# SPDX-License-Identifier: Apache-2.0
"""
Tools for the OSCAR AgentCore runtime.

Action-group Lambdas
--------------------
Call OSCAR's existing action-group Lambdas from the AgentCore runtime.

The Lambdas were written for Bedrock Agents Classic. They read the Classic
action-group event (actionGroup, function, parameters, sessionAttributes) and
return the Classic response envelope. This module builds that same event and
unwraps that same response, so the Lambdas need no changes.

sessionAttributes carry the caller's identity (requester, approver, admin flags,
Slack channel). They come from the invocation payload set by the Slack bot,
never from the model, because the Lambdas authorize on them.
"""

import json
from pathlib import Path
from typing import Any, Callable, Dict, List, Tuple

from botocore.exceptions import BotoCoreError, ClientError

# Must match lambda/shared-layer/python/oscar_shared/approval_guard.py.
CONFIRMATION_MARKER = "[CONFIRMATION_REQUIRED]"


class ActionLambdaError(RuntimeError):
    """The action-group Lambda raised instead of returning a response."""


def _classic_value(value: Any) -> str:
    """Bedrock Classic sends every parameter value as a string; booleans as 'true'/'false'."""
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def build_event(
    action_group: str,
    function: str,
    params: Dict[str, Any],
    param_types: Dict[str, str],
    session_attributes: Dict[str, str],
) -> Dict[str, Any]:
    """Build the event Bedrock Agents Classic would send for this tool call."""
    return {
        "messageVersion": "1.0",
        "actionGroup": action_group,
        "function": function,
        "parameters": [
            {"name": name, "type": param_types.get(name, "string"), "value": _classic_value(value)}
            for name, value in params.items()
            if value is not None
        ],
        "sessionAttributes": dict(session_attributes),
        "promptSessionAttributes": {},
    }


def parse_response(payload: Dict[str, Any]) -> str:
    """Return the text body from a Classic action-group response."""
    try:
        return payload["response"]["functionResponse"]["responseBody"]["TEXT"]["body"]
    except (KeyError, TypeError) as exc:
        raise ActionLambdaError(f"Unexpected action-group response shape: {exc!r}") from exc


def invoke_action_lambda(
    lambda_client: Any,
    function_arn: str,
    action_group: str,
    function: str,
    params: Dict[str, Any],
    param_types: Dict[str, str],
    session_attributes: Dict[str, str],
) -> Tuple[str, bool]:
    """Invoke an action-group Lambda; return (response_text, needs_confirmation)."""
    event = build_event(action_group, function, params, param_types, session_attributes)
    result = lambda_client.invoke(
        FunctionName=function_arn,
        InvocationType="RequestResponse",
        Payload=json.dumps(event).encode("utf-8"),
    )
    payload = json.loads(result["Payload"].read())
    if result.get("FunctionError"):
        raise ActionLambdaError(f"{function} failed: {payload.get('errorMessage', 'unknown error')}")
    text = parse_response(payload)
    return text, CONFIRMATION_MARKER in text

# ----------------------------------------------------------------------------
# Agent specs (agentcore/specs/*.json, written by utils/agentcore_export.py)
# ----------------------------------------------------------------------------


def load_specs(specs_dir: Path) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    """Return (specialist specs, supervisor specs) from the exported JSON files."""
    supervisors = json.loads((specs_dir / "supervisors.json").read_text())
    specialists = [
        json.loads(path.read_text())
        for path in sorted(specs_dir.glob("*.json"))
        if path.name != "supervisors.json"
    ]
    return specialists, supervisors


def input_schema(function_spec: Dict[str, Any]) -> Dict[str, Any]:
    """JSON Schema for one function, built from its Classic parameter definitions."""
    params = function_spec["parameters"]
    return {
        "type": "object",
        "properties": {
            name: {"type": param["type"], "description": param["description"]}
            for name, param in params.items()
        },
        "required": [name for name, param in params.items() if param["required"]],
    }


ToolFunction = Callable[..., Dict[str, Any]]


def make_action_tool(
    lambda_client: Any,
    function_arn: str,
    action_group: str,
    function_spec: Dict[str, Any],
) -> Tuple[Dict[str, Any], ToolFunction]:
    """Return (tool_spec, tool_function) for one action-group function.

    The tool function follows the Strands module-tool contract:
    ``fn(tool_use, **invocation_state) -> {"toolUseId", "status", "content"}``.
    The caller's identity is read from ``invocation_state["session_attributes"]``
    on every call, so it always comes from the current request, never the model.
    """
    name = function_spec["name"]
    param_types = {p: d["type"] for p, d in function_spec["parameters"].items()}
    tool_spec = {
        "name": name,
        "description": function_spec["description"],
        "inputSchema": {"json": input_schema(function_spec)},
    }

    def tool_function(tool_use: Dict[str, Any], **invocation_state: Any) -> Dict[str, Any]:
        session_attributes = invocation_state.get("session_attributes", {})
        oscar_ctx = invocation_state.get("oscar_ctx", {})
        # Only declared parameters are forwarded, as Classic did; anything else the
        # model invents (e.g. a fake requester_user_id) is dropped.
        params = {k: v for k, v in tool_use.get("input", {}).items() if k in param_types}
        try:
            text, needs_confirmation = invoke_action_lambda(
                lambda_client, function_arn, action_group, name,
                params, param_types, session_attributes,
            )
        except ActionLambdaError as exc:
            return {"toolUseId": tool_use["toolUseId"], "status": "error", "content": [{"text": str(exc)}]}
        if needs_confirmation:
            oscar_ctx["needs_confirmation"] = True
        return {"toolUseId": tool_use["toolUseId"], "status": "success", "content": [{"text": text}]}

    tool_function.__name__ = name
    return tool_spec, tool_function

# ----------------------------------------------------------------------------
# Knowledge base search (replaces the knowledge base attached to Classic agents)
# ----------------------------------------------------------------------------


KB_TOOL_NAME = "search_knowledge_base"
KB_TOOL_DESCRIPTION = (
    "Search the knowledge base with all build, test and release related docs. "
    "Returns matching passages with their source document so you can cite them."
)
KB_DEFAULT_RESULTS = 5


def _source(result: Dict[str, Any]) -> str:
    location = result.get("location", {})
    return (
        location.get("s3Location", {}).get("uri")
        or location.get("webLocation", {}).get("url")
        or "unknown source"
    )


def make_kb_tool(
    agent_runtime_client: Any,
    knowledge_base_id: str,
    number_of_results: int = KB_DEFAULT_RESULTS,
) -> Tuple[Dict[str, Any], ToolFunction]:
    """Return (tool_spec, tool_function) that searches the OSCAR knowledge base."""
    tool_spec = {
        "name": KB_TOOL_NAME,
        "description": KB_TOOL_DESCRIPTION,
        "inputSchema": {"json": {
            "type": "object",
            "properties": {"query": {"type": "string", "description": "What to search for"}},
            "required": ["query"],
        }},
    }

    def tool_function(tool_use: Dict[str, Any], **_invocation_state: Any) -> Dict[str, Any]:
        query = str(tool_use.get("input", {}).get("query", "")).strip()
        if not query:
            return {"toolUseId": tool_use["toolUseId"], "status": "error",
                    "content": [{"text": "A non-empty query is required."}]}
        try:
            response = agent_runtime_client.retrieve(
                knowledgeBaseId=knowledge_base_id,
                retrievalQuery={"text": query},
                retrievalConfiguration={"vectorSearchConfiguration": {"numberOfResults": number_of_results}},
            )
        except (BotoCoreError, ClientError) as exc:
            return {"toolUseId": tool_use["toolUseId"], "status": "error",
                    "content": [{"text": f"Knowledge base search failed: {exc}"}]}
        passages = [
            f"[{i}] Source: {_source(r)}\n{r.get('content', {}).get('text', '')}"
            for i, r in enumerate(response.get("retrievalResults", []), start=1)
        ]
        text = "\n\n".join(passages) if passages else "No matching documents found."
        return {"toolUseId": tool_use["toolUseId"], "status": "success", "content": [{"text": text}]}

    tool_function.__name__ = KB_TOOL_NAME
    return tool_spec, tool_function

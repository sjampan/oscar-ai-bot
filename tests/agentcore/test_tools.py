# Copyright OpenSearch Contributors
# SPDX-License-Identifier: Apache-2.0

"""The AgentCore runtime must call action-group Lambdas exactly as Bedrock Classic did."""

import importlib.util
import io
import json
from pathlib import Path

import pytest
from botocore.exceptions import ClientError

from agentcore.tools import (CONFIRMATION_MARKER, ActionLambdaError,
                             build_event, input_schema, invoke_action_lambda,
                             load_specs, make_action_tool, make_kb_tool,
                             parse_response)

ARN = "arn:aws:lambda:us-east-1:123456789012:function:oscar-jenkins"
ATTRS = {"requester_user_id": "U123", "requester_is_admin": "True", "slack_channel": "C1"}


def _classic_response(body: str) -> dict:
    return {
        "messageVersion": "1.0",
        "response": {
            "actionGroup": "jenkinsOperations",
            "function": "get_build_status",
            "functionResponse": {"responseBody": {"TEXT": {"body": body}}},
        },
    }


class FakeLambda:
    def __init__(self, payload: dict, function_error: str = None):
        self.payload = payload
        self.function_error = function_error
        self.calls = []

    def invoke(self, **kwargs):
        self.calls.append(kwargs)
        result = {"Payload": io.BytesIO(json.dumps(self.payload).encode())}
        if self.function_error:
            result["FunctionError"] = self.function_error
        return result


class TestBuildEvent:

    def test_has_the_fields_the_lambdas_read(self):
        event = build_event("jenkinsOperations", "get_build_status",
                            {"job_name": "docker-scan", "build_number": 42},
                            {"job_name": "string", "build_number": "string"}, ATTRS)
        assert event["actionGroup"] == "jenkinsOperations"
        assert event["function"] == "get_build_status"
        assert event["sessionAttributes"] == ATTRS
        assert event["parameters"] == [
            {"name": "job_name", "type": "string", "value": "docker-scan"},
            {"name": "build_number", "type": "string", "value": "42"},
        ]

    def test_values_are_strings_like_classic(self):
        event = build_event("g", "f", {"age_days": 60, "release_components": True, "off": False},
                            {"age_days": "integer", "release_components": "boolean", "off": "boolean"}, {})
        assert [p["value"] for p in event["parameters"]] == ["60", "true", "false"]
        assert [p["type"] for p in event["parameters"]] == ["integer", "boolean", "boolean"]

    def test_omitted_optional_params_are_not_sent(self):
        event = build_event("g", "f", {"repo": "x", "state": None}, {"repo": "string", "state": "string"}, {})
        assert [p["name"] for p in event["parameters"]] == ["repo"]

    def test_session_attributes_are_copied_not_shared(self):
        attrs = dict(ATTRS)
        event = build_event("g", "f", {}, {}, attrs)
        event["sessionAttributes"]["requester_user_id"] = "tampered"
        assert attrs["requester_user_id"] == "U123"


class TestInvokeActionLambda:

    def test_round_trip(self):
        client = FakeLambda(_classic_response("Build #42: SUCCESS"))
        text, needs_confirmation = invoke_action_lambda(
            client, ARN, "jenkinsOperations", "get_build_status",
            {"job_name": "docker-scan", "build_number": "42"}, {}, ATTRS)
        assert text == "Build #42: SUCCESS"
        assert needs_confirmation is False
        call = client.calls[0]
        assert call["FunctionName"] == ARN
        assert call["InvocationType"] == "RequestResponse"
        assert json.loads(call["Payload"])["sessionAttributes"] == ATTRS

    def test_confirmation_marker_is_detected(self):
        client = FakeLambda(_classic_response(f"{CONFIRMATION_MARKER} second approver needed"))
        _, needs_confirmation = invoke_action_lambda(client, ARN, "g", "trigger_job", {}, {}, ATTRS)
        assert needs_confirmation is True

    def test_lambda_exception_raises(self):
        client = FakeLambda({"errorMessage": "boom", "errorType": "Exception"}, function_error="Unhandled")
        with pytest.raises(ActionLambdaError, match="boom"):
            invoke_action_lambda(client, ARN, "g", "f", {}, {}, ATTRS)

    def test_unexpected_shape_raises(self):
        with pytest.raises(ActionLambdaError):
            parse_response({"statusCode": 200})


def test_marker_matches_shared_layer():
    path = Path(__file__).resolve().parents[2] / "lambda/shared-layer/python/oscar_shared/approval_guard.py"
    spec = importlib.util.spec_from_file_location("approval_guard", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert CONFIRMATION_MARKER == module.CONFIRMATION_MARKER

# ---------------------------------------------------------------------------
# Specs and tool construction
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def exported_specs(tmp_path_factory):
    """Export the real agent definitions exactly as the CDK side will."""
    from agents.github import GitHubAgent
    from agents.jenkins import JenkinsAgent
    from agents.metrics import MetricsAgent
    from agents.SecurityAdvisories import SecurityAdvisoriesAgent
    from utils.agentcore_export import write_specs
    out = tmp_path_factory.mktemp("specs")
    write_specs([JenkinsAgent(), MetricsAgent(), SecurityAdvisoriesAgent(), GitHubAgent()], out)
    return load_specs(out)


def _function(specs, agent_name, function_name):
    agent = next(a for a in specs[0] if a["name"] == agent_name)
    for group in agent["action_groups"]:
        for fn in group["functions"]:
            if fn["name"] == function_name:
                return group["name"], fn
    raise KeyError(function_name)


class TestSpecs:

    def test_loads_every_specialist_and_both_supervisors(self, exported_specs):
        specialists, supervisors = exported_specs
        assert sorted(a["name"] for a in specialists) == ["SecurityAdvisories", "github", "jenkins", "metrics"]
        assert set(supervisors) == {"privileged", "limited"}

    def test_input_schema_mirrors_classic_parameters(self, exported_specs):
        _, fn = _function(exported_specs, "SecurityAdvisories", "query_vulnerabilities")
        schema = input_schema(fn)
        assert schema["type"] == "object"
        assert schema["required"] == ["query"]
        assert schema["properties"]["age_days"]["type"] == "integer"
        assert schema["properties"]["release_components"]["type"] == "boolean"

    def test_function_without_parameters(self, exported_specs):
        _, fn = _function(exported_specs, "jenkins", "list_jobs")
        assert input_schema(fn) == {"type": "object", "properties": {}, "required": []}


class TestMakeActionTool:

    def test_spec_keeps_classic_name_and_description(self, exported_specs):
        group, fn = _function(exported_specs, "jenkins", "trigger_job")
        spec, tool_fn = make_action_tool(FakeLambda(_classic_response("ok")), ARN, group, fn)
        assert spec["name"] == "trigger_job" == tool_fn.__name__
        assert spec["description"] == fn["description"]
        assert spec["inputSchema"]["json"]["required"] == ["job_name", "confirmed"]

    def test_identity_comes_from_invocation_state_not_model_input(self, exported_specs):
        group, fn = _function(exported_specs, "jenkins", "get_build_status")
        client = FakeLambda(_classic_response("SUCCESS"))
        _, tool_fn = make_action_tool(client, ARN, group, fn)
        model_input = {"job_name": "docker-scan", "build_number": "7", "requester_user_id": "U_FAKE"}
        result = tool_fn({"toolUseId": "t1", "input": model_input}, session_attributes=ATTRS, oscar_ctx={})
        sent = json.loads(client.calls[0]["Payload"])
        assert sent["sessionAttributes"] == ATTRS
        assert [p["name"] for p in sent["parameters"]] == ["job_name", "build_number"]
        assert sent["actionGroup"] == "jenkinsOperations"
        assert result == {"toolUseId": "t1", "status": "success", "content": [{"text": "SUCCESS"}]}

    def test_confirmation_sets_flag_on_request_context(self, exported_specs):
        group, fn = _function(exported_specs, "jenkins", "trigger_job")
        _, tool_fn = make_action_tool(FakeLambda(_classic_response(CONFIRMATION_MARKER)), ARN, group, fn)
        ctx = {}
        tool_fn({"toolUseId": "t1", "input": {}}, session_attributes=ATTRS, oscar_ctx=ctx)
        assert ctx == {"needs_confirmation": True}

    def test_lambda_failure_becomes_error_result(self, exported_specs):
        group, fn = _function(exported_specs, "jenkins", "list_jobs")
        client = FakeLambda({"errorMessage": "boom"}, function_error="Unhandled")
        _, tool_fn = make_action_tool(client, ARN, group, fn)
        result = tool_fn({"toolUseId": "t1", "input": {}}, session_attributes=ATTRS)
        assert result["status"] == "error"
        assert "boom" in result["content"][0]["text"]

# ---------------------------------------------------------------------------
# Knowledge base search
# ---------------------------------------------------------------------------


class FakeAgentRuntime:
    def __init__(self, response=None, error=None):
        self.response = response or {"retrievalResults": []}
        self.error = error
        self.calls = []

    def retrieve(self, **kwargs):
        self.calls.append(kwargs)
        if self.error:
            raise self.error
        return self.response


class TestKnowledgeBaseTool:

    def test_returns_passages_with_sources(self):
        client = FakeAgentRuntime({"retrievalResults": [
            {"content": {"text": "Cut the RC branch first."},
             "location": {"type": "S3", "s3Location": {"uri": "s3://docs/release.md"}}},
            {"content": {"text": "Then run the build."}, "location": {}},
        ]})
        spec, tool_fn = make_kb_tool(client, "KB123")
        result = tool_fn({"toolUseId": "t1", "input": {"query": "release steps"}})
        assert spec["name"] == "search_knowledge_base"
        assert client.calls == [{
            "knowledgeBaseId": "KB123",
            "retrievalQuery": {"text": "release steps"},
            "retrievalConfiguration": {"vectorSearchConfiguration": {"numberOfResults": 5}},
        }]
        text = result["content"][0]["text"]
        assert result["status"] == "success"
        assert "[1] Source: s3://docs/release.md\nCut the RC branch first." in text
        assert "[2] Source: unknown source\nThen run the build." in text

    def test_no_results(self):
        _, tool_fn = make_kb_tool(FakeAgentRuntime(), "KB123")
        result = tool_fn({"toolUseId": "t1", "input": {"query": "nothing"}})
        assert result["content"][0]["text"] == "No matching documents found."

    def test_empty_query_is_rejected_without_calling_aws(self):
        client = FakeAgentRuntime()
        _, tool_fn = make_kb_tool(client, "KB123")
        result = tool_fn({"toolUseId": "t1", "input": {"query": "  "}})
        assert result["status"] == "error"
        assert client.calls == []

    def test_aws_error_becomes_error_result(self):
        error = ClientError({"Error": {"Code": "AccessDeniedException", "Message": "denied"}}, "Retrieve")
        _, tool_fn = make_kb_tool(FakeAgentRuntime(error=error), "KB123")
        result = tool_fn({"toolUseId": "t1", "input": {"query": "x"}})
        assert result["status"] == "error"
        assert "AccessDeniedException" in result["content"][0]["text"]

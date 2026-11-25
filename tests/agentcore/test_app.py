# Copyright OpenSearch Contributors
# SPDX-License-Identifier: Apache-2.0

"""OSCAR AgentCore runtime: tiers, specialists, sessions and identity flow."""

import io
import json

import pytest

from agentcore.app import DEFAULT_TIER, MAX_CACHED_SESSIONS, OscarRuntime

LAMBDA_ARNS = {
    "jenkins": "arn:aws:lambda:us-east-1:123456789012:function:jenkins",
    "metrics": "arn:aws:lambda:us-east-1:123456789012:function:metrics",
    "github": "arn:aws:lambda:us-east-1:123456789012:function:github",
    "SecurityAdvisories": "arn:aws:lambda:us-east-1:123456789012:function:sa",
    "communication": "arn:aws:lambda:us-east-1:123456789012:function:comm",
}
PRIVILEGED_ATTRS = {"access_tier": "privileged", "requester_user_id": "U123", "requester_is_admin": "True"}


class FakeLambda:
    """Records invocations and answers like a Classic action-group Lambda."""

    def __init__(self, body="ok"):
        self.body = body
        self.calls = []

    def invoke(self, **kwargs):
        self.calls.append(json.loads(kwargs["Payload"]))
        reply = {"response": {"functionResponse": {"responseBody": {"TEXT": {"body": self.body}}}}}
        return {"Payload": io.BytesIO(json.dumps(reply).encode())}


class FakeAgent:
    """Stands in for strands.Agent; `script` decides what each call does."""

    script = staticmethod(lambda agent, prompt, state: f"answer to: {prompt}")

    def __init__(self, model, system_prompt, tools):
        self.model, self.system_prompt, self.tools = model, system_prompt, tools
        self.calls = []

    def __call__(self, prompt, invocation_state):
        self.calls.append((prompt, invocation_state))
        return FakeAgent.script(self, prompt, invocation_state)

    def tool(self, name):
        return next(fn for spec, fn in self.tools if spec["name"] == name)

    @property
    def tool_names(self):
        return [spec["name"] for spec, _ in self.tools]


@pytest.fixture(scope="module")
def specs_dir(tmp_path_factory):
    from agents.github import GitHubAgent
    from agents.jenkins import JenkinsAgent
    from agents.metrics import MetricsAgent
    from agents.SecurityAdvisories import SecurityAdvisoriesAgent
    from utils.agentcore_export import write_specs
    out = tmp_path_factory.mktemp("specs")
    write_specs([JenkinsAgent(), MetricsAgent(), SecurityAdvisoriesAgent(), GitHubAgent()], out)
    return out


@pytest.fixture
def lambda_client():
    return FakeLambda()


@pytest.fixture
def runtime(specs_dir, lambda_client, monkeypatch):
    monkeypatch.setattr(FakeAgent, "script", staticmethod(lambda agent, prompt, state: f"answer to: {prompt}"))
    return OscarRuntime(
        specs_dir=specs_dir,
        lambda_arns=LAMBDA_ARNS,
        knowledge_base_id="KB123",
        lambda_client=lambda_client,
        agent_runtime_client=None,
        model_factory=lambda model_id, guardrail: {"model_id": model_id, "guardrail": guardrail},
        agent_factory=FakeAgent,
        tool_factory=lambda spec, fn: (spec, fn),
    )


class TestSupervisorTools:

    def test_privileged_gets_every_specialist_kb_and_messaging(self, runtime):
        names = runtime._build_supervisor("privileged").tool_names
        assert set(names) == {
            "Jenkins-Specialist", "Metrics-Specialist", "GitHub-Specialist",
            "Security-Advisories-Specialist", "search_knowledge_base", "send_automated_message",
        }

    def test_limited_gets_no_jenkins_security_or_messaging(self, runtime):
        names = runtime._build_supervisor("limited").tool_names
        assert set(names) == {"Metrics-Specialist", "GitHub-Specialist", "search_knowledge_base"}

    def test_guardrail_matches_classic(self, runtime):
        assert runtime._build_supervisor("privileged").model["guardrail"] is True
        assert runtime._build_supervisor("limited").model["guardrail"] is True
        specs = {s["name"]: s for s in runtime.specialists}
        assert runtime._build_specialist(specs["github"]).model["guardrail"] is True
        assert runtime._build_specialist(specs["jenkins"]).model["guardrail"] is False

    def test_specialist_kb_matches_classic(self, runtime):
        specs = {s["name"]: s for s in runtime.specialists}
        assert "search_knowledge_base" in runtime._build_specialist(specs["jenkins"]).tool_names
        assert "search_knowledge_base" not in runtime._build_specialist(specs["github"]).tool_names


class TestSpecialistTool:

    def test_passes_identity_in_a_fresh_state(self, runtime):
        supervisor = runtime._build_supervisor("privileged")
        ctx = {"needs_confirmation": False}
        parent_state = {"session_attributes": PRIVILEGED_ATTRS, "oscar_ctx": ctx, "agent": supervisor}
        result = supervisor.tool("Jenkins-Specialist")({"toolUseId": "t1", "input": {"request": "list jobs"}},
                                                       **parent_state)
        assert result == {"toolUseId": "t1", "status": "success", "content": [{"text": "answer to: list jobs"}]}
        assert parent_state["agent"] is supervisor

    def test_empty_request_is_rejected(self, runtime):
        tool = runtime._build_supervisor("privileged").tool("Jenkins-Specialist")
        assert tool({"toolUseId": "t1", "input": {"request": " "}})["status"] == "error"

    def test_specialist_failure_becomes_error_result(self, runtime, monkeypatch):
        def boom(agent, prompt, state):
            if "Jenkins" in agent.system_prompt:
                raise RuntimeError("model unavailable")
            return "unused"
        monkeypatch.setattr(FakeAgent, "script", staticmethod(boom))
        tool = runtime._build_supervisor("privileged").tool("Jenkins-Specialist")
        result = tool({"toolUseId": "t1", "input": {"request": "x"}}, session_attributes={}, oscar_ctx={})
        assert result["status"] == "error"
        assert "model unavailable" in result["content"][0]["text"]


class TestHandle:

    def test_identity_reaches_the_lambda_end_to_end(self, runtime, lambda_client, monkeypatch):
        """supervisor -> Jenkins specialist -> get_build_status -> Lambda gets the payload's identity."""
        def script(agent, prompt, state):
            if "Jenkins-Specialist" in agent.tool_names:
                return agent.tool("Jenkins-Specialist")(
                    {"toolUseId": "s1", "input": {"request": "status of docker-scan #7"}}, **state
                )["content"][0]["text"]
            if "get_build_status" in agent.tool_names:
                return agent.tool("get_build_status")(
                    {"toolUseId": "j1", "input": {"job_name": "docker-scan", "build_number": "7",
                                                  "requester_user_id": "U_FAKE"}}, **state
                )["content"][0]["text"]
            return "unexpected"
        monkeypatch.setattr(FakeAgent, "script", staticmethod(script))
        out = runtime.handle({"prompt": "status?", "session_attributes": PRIVILEGED_ATTRS}, "session-1")
        assert out == {"text": "ok", "needs_confirmation": False}
        sent = lambda_client.calls[0]
        assert sent["function"] == "get_build_status"
        assert sent["sessionAttributes"] == PRIVILEGED_ATTRS
        assert "requester_user_id" not in [p["name"] for p in sent["parameters"]]

    def test_confirmation_flag_is_returned(self, runtime, monkeypatch):
        def script(agent, prompt, state):
            state["oscar_ctx"]["needs_confirmation"] = True
            return "needs a second approver"
        monkeypatch.setattr(FakeAgent, "script", staticmethod(script))
        out = runtime.handle({"prompt": "trigger", "session_attributes": PRIVILEGED_ATTRS}, "session-1")
        assert out["needs_confirmation"] is True

    @pytest.mark.parametrize("attrs", [{}, {"access_tier": "admin"}, {"access_tier": "PRIVILEGED"}])
    def test_missing_or_unknown_tier_falls_back_to_limited(self, runtime, attrs):
        runtime.handle({"prompt": "hi", "session_attributes": attrs}, "session-1")
        (session_id, tier), (supervisor, _) = next(iter(runtime._sessions.items()))
        assert tier == DEFAULT_TIER == "limited"
        assert "send_automated_message" not in supervisor.tool_names
        assert supervisor.calls[0][1]["session_attributes"]["access_tier"] == "limited"

    @pytest.mark.parametrize("payload", [
        {}, {"prompt": ""}, {"prompt": "  "}, {"prompt": 5},
        {"prompt": "x" * 20_001}, {"prompt": "hi", "session_attributes": ["not", "a", "dict"]},
    ])
    def test_invalid_payload_is_rejected(self, runtime, payload):
        assert "error" in runtime.handle(payload, "session-1")
        assert not runtime._sessions

    def test_same_session_reuses_its_supervisor(self, runtime):
        runtime.handle({"prompt": "a", "session_attributes": PRIVILEGED_ATTRS}, "s1")
        runtime.handle({"prompt": "b", "session_attributes": PRIVILEGED_ATTRS}, "s1")
        assert len(runtime._sessions) == 1
        supervisor, _ = runtime._sessions[("s1", "privileged")]
        assert [c[0] for c in supervisor.calls] == ["a", "b"]

    def test_tiers_never_share_a_conversation(self, runtime):
        runtime.handle({"prompt": "a", "session_attributes": PRIVILEGED_ATTRS}, "s1")
        runtime.handle({"prompt": "b", "session_attributes": {"access_tier": "limited"}}, "s1")
        assert set(runtime._sessions) == {("s1", "privileged"), ("s1", "limited")}

    def test_session_cache_is_bounded(self, runtime):
        for i in range(MAX_CACHED_SESSIONS + 5):
            runtime.handle({"prompt": "a", "session_attributes": {}}, f"s{i}")
        assert len(runtime._sessions) == MAX_CACHED_SESSIONS
        assert ("s0", "limited") not in runtime._sessions


def test_real_strands_accepts_every_tool(specs_dir, lambda_client):
    """Build both supervisors with real Strands objects (no model call is made)."""
    from strands import Agent
    from strands.models.bedrock import BedrockModel
    from strands.tools.tools import PythonAgentTool
    real = OscarRuntime(
        specs_dir=specs_dir, lambda_arns=LAMBDA_ARNS, knowledge_base_id="KB123",
        lambda_client=lambda_client, agent_runtime_client=None,
        model_factory=lambda model_id, guardrail: BedrockModel(model_id=model_id, max_tokens=256,
                                                               region_name="us-east-1"),
        agent_factory=lambda **kw: Agent(callback_handler=None, **kw),
        tool_factory=lambda spec, fn: PythonAgentTool(spec["name"], spec, fn),
    )
    assert "send_automated_message" in real._build_supervisor("privileged").tool_names
    assert len(real._build_specialist(next(s for s in real.specialists if s["name"] == "github")).tool_names) == 15

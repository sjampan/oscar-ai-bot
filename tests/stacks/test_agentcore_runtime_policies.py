# Copyright OpenSearch Contributors
# SPDX-License-Identifier: Apache-2.0

"""Least-privilege policies for the OSCAR AgentCore Runtime execution role."""

import pytest

from stacks.policy_definitions import OscarPolicyDefinitions

ACCOUNT, REGION = "123456789012", "us-east-1"
MODEL = "us.anthropic.claude-sonnet-4-5-20250929-v1:0"
GUARDRAIL = f"arn:aws:bedrock:{REGION}:{ACCOUNT}:guardrail/abc123"
LAMBDAS = [f"arn:aws:lambda:{REGION}:{ACCOUNT}:function:oscar-{n}-dev" for n in ("jenkins", "metrics")]


@pytest.fixture
def statements():
    policies = OscarPolicyDefinitions(ACCOUNT, REGION, "dev").get_agentcore_runtime_policies(
        runtime_name="oscar_dev", model_ids=[MODEL, MODEL], guardrail_arn=GUARDRAIL,
        knowledge_base_id="KB123", lambda_arns=LAMBDAS,
    )
    return {s.to_statement_json()["Sid"]: s.to_statement_json() for s in policies}


def _as_list(value):
    return value if isinstance(value, list) else [value]


def test_models_are_limited_to_oscars_model(statements):
    resources = _as_list(statements["InvokeOscarModels"]["Resource"])
    assert sorted(resources) == sorted([
        f"arn:aws:bedrock:{REGION}:{ACCOUNT}:inference-profile/{MODEL}",
        "arn:aws:bedrock:*::foundation-model/anthropic.claude-sonnet-4-5-20250929-v1:0",
    ])


def test_guardrail_kb_and_lambdas_are_exact_resources(statements):
    assert _as_list(statements["ApplyOscarGuardrail"]["Resource"]) == [GUARDRAIL]
    assert _as_list(statements["RetrieveFromOscarKnowledgeBase"]["Resource"]) == [
        f"arn:aws:bedrock:{REGION}:{ACCOUNT}:knowledge-base/KB123"]
    assert _as_list(statements["InvokeOscarToolLambdas"]["Resource"]) == LAMBDAS


def test_no_wildcard_access_to_models_kb_lambdas_or_agents(statements):
    for sid in ("InvokeOscarModels", "ApplyOscarGuardrail", "RetrieveFromOscarKnowledgeBase", "InvokeOscarToolLambdas"):
        assert "*" not in _as_list(statements[sid]["Resource"])
    actions = [a for s in statements.values() for a in _as_list(s["Action"])]
    assert not any(a.startswith(("bedrock:InvokeAgent", "bedrock-agentcore:GetWorkloadAccessToken")) for a in actions)


def test_metrics_are_scoped_to_agentcore_namespace(statements):
    assert statements["RuntimeMetrics"]["Condition"] == {"StringEquals": {"cloudwatch:namespace": "bedrock-agentcore"}}

# Copyright OpenSearch Contributors
# SPDX-License-Identifier: Apache-2.0

"""OscarAgentsStack with the Classic / AgentCore flags."""

import json
import os
import re

import pytest
from aws_cdk import App, Environment
from aws_cdk.assertions import Match, Template

from agents.github import GitHubAgent
from agents.jenkins import JenkinsAgent
from agents.metrics import MetricsAgent
from agents.SecurityAdvisories import SecurityAdvisoriesAgent
from stacks.bedrock_agents_stack import OscarAgentsStack
from stacks.lambda_stack import OscarLambdaStack
from stacks.permissions_stack import OscarPermissionsStack
from stacks.secrets_stack import OscarSecretsStack
from stacks.storage_stack import OscarStorageStack
from stacks.vpc_stack import OscarVpcStack

ENV = Environment(account="123456789012", region="us-east-1")
ALL_AGENTS = [JenkinsAgent(), MetricsAgent(), SecurityAdvisoriesAgent(), GitHubAgent()]


def _agents_template(**flags) -> Template:
    for key, value in {
        "CDK_DEFAULT_ACCOUNT": "123456789012", "CDK_DEFAULT_REGION": "us-east-1",
        "REMEDIATION_WRITE_OWNER": "test-owner", "REMEDIATION_BASE_OWNER": "test-owner",
        "REMEDIATION_GIT_NAME": "test-bot", "REMEDIATION_GIT_EMAIL": "test-bot@users.noreply.github.com",
    }.items():
        os.environ.setdefault(key, value)
    app = App(context={"aws:cdk:bundling-stacks": []})
    permissions = OscarPermissionsStack(app, "Perms", environment="dev", agents=ALL_AGENTS, env=ENV)
    secrets = OscarSecretsStack(app, "Secrets", environment="dev", agents=ALL_AGENTS, env=ENV)
    storage = OscarStorageStack(app, "Storage", environment="dev", env=ENV)
    vpc = OscarVpcStack(app, "Vpc", env=ENV)
    lambda_stack = OscarLambdaStack(app, "Lambda", permissions_stack=permissions, secrets_stack=secrets,
                                    storage_stack=storage, vpc_stack=vpc, environment="dev",
                                    agents=ALL_AGENTS, env=ENV)
    stack = OscarAgentsStack(app, "Agents", permissions_stack=permissions, lambda_stack=lambda_stack,
                             environment="dev", agents=ALL_AGENTS, env=ENV, **flags)
    return Template.from_stack(stack)


@pytest.fixture(scope="module")
def default_template():
    return _agents_template()


@pytest.fixture(scope="module")
def both_template():
    return _agents_template(enable_agentcore=True)


@pytest.fixture(scope="module")
def agentcore_only_template():
    return _agents_template(enable_classic=False, enable_agentcore=True)


class TestDefaultsKeepClassicOnly:

    def test_classic_agents_unchanged(self, default_template):
        default_template.resource_count_is("AWS::Bedrock::Agent", 6)
        default_template.resource_count_is("AWS::Bedrock::AgentAlias", 6)
        default_template.resource_count_is("AWS::Bedrock::Guardrail", 1)

    def test_no_agentcore_resources(self, default_template):
        default_template.resource_count_is("AWS::BedrockAgentCore::Runtime", 0)
        default_template.resource_count_is("AWS::BedrockAgentCore::RuntimeEndpoint", 0)


class TestClassicAndAgentCoreSideBySide:

    def test_classic_untouched_and_agentcore_added(self, default_template, both_template):
        def classic_resources(template):
            # Supervisor alias descriptions embed the synth timestamp by design; ignore it.
            text = json.dumps({k: v for k, v in template.to_json()["Resources"].items()
                               if v["Type"].startswith("AWS::Bedrock::")}, sort_keys=True)
            return re.sub(r"deployed \d{8}T\d{6}Z", "deployed <ts>", text)
        assert classic_resources(both_template) == classic_resources(default_template)
        both_template.resource_count_is("AWS::BedrockAgentCore::Runtime", 1)
        both_template.resource_count_is("AWS::BedrockAgentCore::RuntimeEndpoint", 1)

    def test_runtime_configuration(self, both_template):
        both_template.has_resource_properties("AWS::BedrockAgentCore::Runtime", {
            "AgentRuntimeName": "oscar_dev",
            "NetworkConfiguration": {"NetworkMode": "PUBLIC"},
            "ProtocolConfiguration": "HTTP",
            "LifecycleConfiguration": {"IdleRuntimeSessionTimeout": 600},
            "EnvironmentVariables": Match.object_like({
                "OSCAR_LAMBDA_ARNS": Match.any_value(),
                "KNOWLEDGE_BASE_ID": Match.any_value(),
                "GUARDRAIL_ID": Match.any_value(),
                "GUARDRAIL_VERSION": Match.any_value(),
            }),
        })
        both_template.has_resource_properties("AWS::BedrockAgentCore::RuntimeEndpoint", {"Name": "LIVE"})
        both_template.has_resource_properties("AWS::SSM::Parameter", {"Name": "/oscar/dev/agentcore/runtime-arn"})

    def test_no_secrets_in_environment(self, both_template):
        runtime = next(iter(both_template.find_resources("AWS::BedrockAgentCore::Runtime").values()))
        names = set(runtime["Properties"]["EnvironmentVariables"])
        assert not any(word in name.upper() for name in names for word in ("SECRET", "TOKEN", "PASSWORD", "KEY_ID"))

    def test_role_is_trusted_only_by_agentcore_in_this_account(self, both_template):
        both_template.has_resource_properties("AWS::IAM::Role", {
            "AssumeRolePolicyDocument": {"Statement": [Match.object_like({
                "Principal": {"Service": "bedrock-agentcore.amazonaws.com"},
                "Condition": {
                    "StringEquals": {"aws:SourceAccount": "123456789012"},
                    "ArnLike": {"aws:SourceArn": "arn:aws:bedrock-agentcore:us-east-1:123456789012:*"},
                },
            })]},
        })

    def test_runtime_can_invoke_exactly_the_five_tool_lambdas(self, both_template):
        policies = both_template.find_resources("AWS::IAM::Policy")
        statements = [s for p in policies.values() for s in p["Properties"]["PolicyDocument"]["Statement"]
                      if s.get("Sid") == "InvokeOscarToolLambdas"]
        assert len(statements) == 1
        assert len(statements[0]["Resource"]) == 5

    def test_specs_are_exported_for_the_image(self, both_template):
        from stacks.bedrock_agents_stack import AGENTCORE_DIR
        files = sorted(p.name for p in (AGENTCORE_DIR / "specs").glob("*.json"))
        assert files == ["SecurityAdvisories.json", "github.json", "jenkins.json", "metrics.json", "supervisors.json"]
        assert json.loads((AGENTCORE_DIR / "specs" / "jenkins.json").read_text())["access_level"] == "privileged"


class TestAgentCoreOnly:

    def test_no_classic_agents_but_guardrail_kept(self, agentcore_only_template):
        agentcore_only_template.resource_count_is("AWS::Bedrock::Agent", 0)
        agentcore_only_template.resource_count_is("AWS::Bedrock::AgentAlias", 0)
        agentcore_only_template.resource_count_is("AWS::Bedrock::Guardrail", 1)
        agentcore_only_template.resource_count_is("AWS::BedrockAgentCore::Runtime", 1)

# Copyright OpenSearch Contributors
# SPDX-License-Identifier: Apache-2.0

"""The AgentCore export must carry exactly what the Classic stack deploys."""

import json

import pytest

from agents.github import GitHubAgent
from agents.jenkins import JenkinsAgent
from agents.metrics import MetricsAgent
from agents.SecurityAdvisories import SecurityAdvisoriesAgent
from agents.supervisor_prompts import (LIMITED_SUPERVISOR_INSTRUCTION,
                                       PRIVILEGED_SUPERVISOR_INSTRUCTION)
from utils.agentcore_export import (build_agent_spec, build_supervisor_specs,
                                    write_specs)

LAMBDA_ARN = "arn:aws:lambda:us-east-1:123456789012:function:placeholder"
ALL_AGENTS = [JenkinsAgent(), MetricsAgent(), SecurityAdvisoriesAgent(), GitHubAgent()]


@pytest.mark.parametrize("agent", ALL_AGENTS, ids=[a.name for a in ALL_AGENTS])
class TestAgentSpecMatchesClassic:

    def test_tools_match_classic_action_groups(self, agent):
        exported = build_agent_spec(agent)["action_groups"]
        classic = agent.get_action_groups(LAMBDA_ARN)
        assert [g["name"] for g in exported] == [g.action_group_name for g in classic]
        for group, cfn_group in zip(exported, classic):
            assert group["description"] == cfn_group.description
            cfn_functions = cfn_group.function_schema.functions
            assert [f["name"] for f in group["functions"]] == [f.name for f in cfn_functions]
            for fn, cfn_fn in zip(group["functions"], cfn_functions):
                assert fn["description"] == cfn_fn.description
                cfn_params = cfn_fn.parameters or {}
                assert set(fn["parameters"]) == set(cfn_params)
                for name, param in fn["parameters"].items():
                    assert param["type"] == cfn_params[name].type
                    assert param["required"] == cfn_params[name].required
                    assert param["description"] == cfn_params[name].description

    def test_prompts_and_routing_match_classic(self, agent):
        spec = build_agent_spec(agent)
        assert spec["name"] == agent.name
        assert spec["model_id"] == agent.get_foundation_model()
        assert spec["instruction"] == agent.get_agent_instruction()
        assert spec["collaborator_name"] == agent.get_collaborator_name()
        assert spec["collaborator_instruction"] == agent.get_collaborator_instruction()
        assert spec["access_level"] == agent.get_access_level()
        assert spec["uses_knowledge_base"] == agent.uses_knowledge_base()

    def test_spec_survives_json_round_trip(self, agent):
        spec = build_agent_spec(agent)
        assert json.loads(json.dumps(spec)) == spec


def test_supervisor_specs_use_shared_prompts():
    specs = build_supervisor_specs()
    assert specs["privileged"]["instruction"] == PRIVILEGED_SUPERVISOR_INSTRUCTION
    assert specs["limited"]["instruction"] == LIMITED_SUPERVISOR_INSTRUCTION


def test_write_specs_writes_one_file_per_agent_plus_supervisors(tmp_path):
    written = write_specs(ALL_AGENTS, tmp_path)
    assert sorted(p.name for p in written) == sorted(
        [f"{a.name}.json" for a in ALL_AGENTS] + ["supervisors.json"]
    )
    jenkins = json.loads((tmp_path / "jenkins.json").read_text())
    assert jenkins == build_agent_spec(JenkinsAgent())

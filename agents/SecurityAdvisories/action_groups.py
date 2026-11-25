# Copyright OpenSearch Contributors
# SPDX-License-Identifier: Apache-2.0

"""Bedrock action group definitions for security advisories agent."""

from typing import List

from aws_cdk import aws_bedrock as bedrock

from agents.SecurityAdvisories.tool_specs import (ACTION_GROUP_DESCRIPTION,
                                                  ACTION_GROUP_NAME, FUNCTIONS)


def get_action_groups(lambda_arn: str) -> List[bedrock.CfnAgent.AgentActionGroupProperty]:
    return [
        _privileged_action_group(lambda_arn),
    ]


def _privileged_action_group(
    lambda_arn: str,
) -> bedrock.CfnAgent.AgentActionGroupProperty:
    """Action group for privileged users — full vulnerability querying."""
    return bedrock.CfnAgent.AgentActionGroupProperty(
        action_group_name=ACTION_GROUP_NAME,
        description=ACTION_GROUP_DESCRIPTION,
        action_group_state="ENABLED",
        action_group_executor=bedrock.CfnAgent.ActionGroupExecutorProperty(lambda_=lambda_arn),
        function_schema=bedrock.CfnAgent.FunctionSchemaProperty(
            functions=[
                bedrock.CfnAgent.FunctionProperty(
                    name=fn["name"],
                    description=fn["description"],
                    parameters={
                        param_name: bedrock.CfnAgent.ParameterDetailProperty(
                            type=param["type"],
                            description=param["description"],
                            required=param["required"],
                        )
                        for param_name, param in fn["parameters"].items()
                    },
                )
                for fn in FUNCTIONS
            ]
        ),
    )

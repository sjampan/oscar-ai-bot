# Copyright OpenSearch Contributors
# SPDX-License-Identifier: Apache-2.0

"""Bedrock action group definitions for GitHub agent.

Three tier-tagged groups: contributor (read-only),
maintainer (create_tag, create_branch), admin (all writes).
The Lambda enforces authorization at runtime via group gate + function gate.
"""

from typing import List

from aws_cdk import aws_bedrock as bedrock

from agents.github.tool_specs import ACTION_GROUPS


def get_action_groups(lambda_arn: str) -> List[bedrock.CfnAgent.AgentActionGroupProperty]:
    executor = bedrock.CfnAgent.ActionGroupExecutorProperty(lambda_=lambda_arn)

    return [
        bedrock.CfnAgent.AgentActionGroupProperty(
            action_group_name=group["name"],
            description=group["description"],
            action_group_state="ENABLED",
            action_group_executor=executor,
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
                    for fn in group["functions"]
                ]
            ),
        )
        for group in ACTION_GROUPS
    ]

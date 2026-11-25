#!/usr/bin/env python
# Copyright OpenSearch Contributors
# SPDX-License-Identifier: Apache-2.0
#
# The OpenSearch Contributors require contributions made to
# this file be licensed under the Apache-2.0 license or a
# compatible open source license.
"""
Bedrock Agents stack for OSCAR CDK automation.

This module defines the Bedrock agents infrastructure including:
- Agent-based collaborator agents created from OscarAgent definitions
- Privileged supervisor agent with full access capabilities
- Limited supervisor agent with read-only access
- Action groups with proper Lambda function associations
"""
import hashlib
import logging
import os
from datetime import datetime, timezone
from typing import Any, List, Optional

from aws_cdk import Fn, Stack
from aws_cdk import aws_bedrock as bedrock
from aws_cdk import aws_ssm as ssm
from constructs import Construct

from agents.supervisor_prompts import (LIMITED_SUPERVISOR_INSTRUCTION,
                                       PRIVILEGED_SUPERVISOR_ACTION_GROUPS,
                                       PRIVILEGED_SUPERVISOR_INSTRUCTION)
from utils.foundation_models import FoundationModels
from utils.guardrail import create_guardrail, get_guardrail_configuration

from .bedrock_agent_details import get_ssm_param_paths

logger = logging.getLogger(__name__)


def _dir_hash(path: str) -> str:
    """Compute a short hash of all files in a directory."""
    h = hashlib.md5()
    for root, _, files in sorted(os.walk(path)):
        for f in sorted(files):
            h.update(open(os.path.join(root, f), "rb").read())
    return h.hexdigest()[:8]


class OscarAgentsStack(Stack):
    """Bedrock agents infrastructure for OSCAR."""

    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        permissions_stack: Any,
        environment: str,
        lambda_stack: Any,
        agents: Optional[List] = None,
        **kwargs,
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)

        self.permissions_stack = permissions_stack
        self.lambda_stack = lambda_stack
        self.agent_role_arn = self.permissions_stack.bedrock_agent_role.role_arn
        self.knowledge_base_id = Fn.import_value("OscarKnowledgeBaseId")
        self.env_name = environment
        self._deploy_ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")

        self.guardrail, self.guardrail_version = create_guardrail(self, self.env_name)
        self.guardrail_config = get_guardrail_configuration(self.guardrail, self.guardrail_version)

        # Create agent-based collaborators, then supervisors
        privileged_collaborators, limited_collaborators = self._create_collaborator_agents(agents or [])
        self._create_supervisor_agent(privileged_collaborators)
        self._create_limited_supervisor_agent(limited_collaborators)

    # --------------------------------------------------------- collaborators
    def _create_collaborator_agents(self, agents) -> tuple:
        """Create Bedrock agents for each agent module and partition into supervisor lists."""
        privileged_collaborators = []
        limited_collaborators = []

        for agent in agents:
            lambda_fn = self.lambda_stack.lambda_functions[agent.name]
            construct_name = agent.name.replace("-", " ").title().replace(" ", "")

            # Knowledge base attachment
            kb_config = None
            if agent.uses_knowledge_base():
                kb_config = [bedrock.CfnAgent.AgentKnowledgeBaseProperty(
                    description="Knowledge base with all build, test and release related docs",
                    knowledge_base_id=self.knowledge_base_id,
                )]

            # Attach guardrail to agents that process untrusted external content
            agent_guardrail = self.guardrail_config if agent.name == "github" else None

            # Create agent
            cfn_agent = bedrock.CfnAgent(
                self, f"Oscar{construct_name}Agent",
                agent_name=f"oscar-{agent.name}-agent-{self.env_name}",
                agent_resource_role_arn=self.agent_role_arn,
                description=f"OSCAR {agent.name} collaborator agent",
                foundation_model=agent.get_foundation_model(),
                idle_session_ttl_in_seconds=600,
                auto_prepare=True,
                skip_resource_in_use_check_on_delete=True,
                action_groups=agent.get_action_groups(lambda_fn.function_arn),
                instruction=agent.get_agent_instruction(),
                knowledge_bases=kb_config,
                guardrail_configuration=agent_guardrail,
            )

            # Create alias — description uses content hash + instruction hash so
            # new version is created when agent code OR env-driven config changes
            agent_hash = _dir_hash(f"agents/{agent.name}")
            instruction_hash = hashlib.md5(
                agent.get_agent_instruction().encode()
            ).hexdigest()[:8]
            alias = bedrock.CfnAgentAlias(
                self, f"Oscar{construct_name}Alias",
                agent_alias_name="LIVE",
                agent_id=cfn_agent.attr_agent_id,
                description=f"Live alias for OSCAR {agent.name} agent ({agent_hash}-{instruction_hash})",
            )
            alias.node.add_dependency(cfn_agent)

            # Write agent ID + alias to SSM
            ssm.StringParameter(
                self, f"{construct_name}AgentIdParam",
                parameter_name=f"/oscar/{self.env_name}/bedrock/{agent.name}-agent-id",
                string_value=cfn_agent.attr_agent_id,
                description=f"OSCAR {agent.name} agent ID for {self.env_name}",
            )
            ssm.StringParameter(
                self, f"{construct_name}AgentAliasParam",
                parameter_name=f"/oscar/{self.env_name}/bedrock/{agent.name}-agent-alias",
                string_value=alias.attr_agent_alias_id,
                description=f"OSCAR {agent.name} agent alias ID for {self.env_name}",
            )

            # Build collaborator spec
            collaborator = bedrock.CfnAgent.AgentCollaboratorProperty(
                agent_descriptor=bedrock.CfnAgent.AgentDescriptorProperty(
                    alias_arn=alias.attr_agent_alias_arn
                ),
                collaboration_instruction=agent.get_collaborator_instruction(),
                collaborator_name=agent.get_collaborator_name(),
                relay_conversation_history="TO_COLLABORATOR",
            )

            # Route to correct supervisor(s)
            access = agent.get_access_level()
            if access in ("privileged", "both"):
                privileged_collaborators.append(collaborator)
            if access in ("limited", "both"):
                limited_collaborators.append(collaborator)

        return privileged_collaborators, limited_collaborators

    # ----------------------------------------------------------- supervisors
    def _create_supervisor_agent(self, collaborators: List[bedrock.CfnAgent.AgentCollaboratorProperty]) -> None:
        """Create the privileged supervisor agent with communication action group."""
        communication_lambda_arn = self.lambda_stack.lambda_functions[
            self.lambda_stack.get_communication_handler_lambda_function_name(self.env_name)
        ].function_arn
        supervisor_action_groups = [
            bedrock.CfnAgent.AgentActionGroupProperty(
                action_group_name=group["name"],
                description=group["description"],
                action_group_state="ENABLED",
                action_group_executor=bedrock.CfnAgent.ActionGroupExecutorProperty(
                    lambda_=communication_lambda_arn,
                ),
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
            for group in PRIVILEGED_SUPERVISOR_ACTION_GROUPS
        ]

        privileged_agent = bedrock.CfnAgent(
            self, "OscarPrivilegedAgent",
            agent_name=f"oscar-privileged-agent-{self.env_name}",
            agent_resource_role_arn=self.agent_role_arn,
            description="Supervisor Agent for OSCAR with intelligent routing between knowledge base, metrics specialists, and a Jenkins specialist.",
            foundation_model=FoundationModels.CLAUDE_4_5_SONNET.value,
            idle_session_ttl_in_seconds=600,
            auto_prepare=True,
            agent_collaboration="SUPERVISOR_ROUTER",
            agent_collaborators=collaborators,
            action_groups=supervisor_action_groups,
            guardrail_configuration=self.guardrail_config,
            knowledge_bases=[bedrock.CfnAgent.AgentKnowledgeBaseProperty(
                description="Knowledge base with all build, test and release related docs",
                knowledge_base_id=self.knowledge_base_id,
            )],
            instruction=PRIVILEGED_SUPERVISOR_INSTRUCTION,
        )

        privileged_alias = bedrock.CfnAgentAlias(
            self, "OscarPrivilegedAgentAlias",
            agent_alias_name="LIVE",
            agent_id=privileged_agent.attr_agent_id,
            description=f"OSCAR privileged agent (deployed {self._deploy_ts})",
        )
        privileged_alias.node.add_dependency(privileged_agent)

        params = get_ssm_param_paths(self.env_name)
        ssm.StringParameter(
            self, "SupervisorAgentIdParam",
            parameter_name=params["supervisor_agent_id"],
            string_value=privileged_agent.attr_agent_id,
            description=f"OSCAR supervisor agent ID for {self.env_name}",
        )
        ssm.StringParameter(
            self, "SupervisorAgentAliasParam",
            parameter_name=params["supervisor_agent_alias"],
            string_value=privileged_alias.attr_agent_alias_id,
            description=f"OSCAR supervisor agent alias ID for {self.env_name}",
        )

    def _create_limited_supervisor_agent(self, collaborators: List[bedrock.CfnAgent.AgentCollaboratorProperty]) -> None:
        """Create the limited supervisor agent (no Jenkins, no communication)."""
        limited_agent = bedrock.CfnAgent(
            self, "OscarLimitedAgent",
            agent_name=f"oscar-limited-agent-{self.env_name}",
            agent_resource_role_arn=self.agent_role_arn,
            description="OSCAR agent with limited access and capabilities",
            foundation_model=FoundationModels.CLAUDE_4_5_SONNET.value,
            idle_session_ttl_in_seconds=600,
            auto_prepare=True,
            agent_collaboration="SUPERVISOR_ROUTER",
            agent_collaborators=collaborators,
            guardrail_configuration=self.guardrail_config,
            knowledge_bases=[bedrock.CfnAgent.AgentKnowledgeBaseProperty(
                description="Knowledge base with all build, test and release related docs",
                knowledge_base_id=self.knowledge_base_id,
            )],
            instruction=LIMITED_SUPERVISOR_INSTRUCTION,
        )

        limited_alias = bedrock.CfnAgentAlias(
            self, "OscarLimitedAgentAlias",
            agent_alias_name="LIVE",
            agent_id=limited_agent.attr_agent_id,
            description=f"Live alias for OSCAR limited agent (deployed {self._deploy_ts})",
        )
        limited_alias.node.add_dependency(limited_agent)

        params = get_ssm_param_paths(self.env_name)
        ssm.StringParameter(
            self, "LimitedSupervisorAgentIdParam",
            parameter_name=params["limited_supervisor_agent_id"],
            string_value=limited_agent.attr_agent_id,
            description=f"OSCAR limited supervisor agent ID for {self.env_name}",
        )
        ssm.StringParameter(
            self, "LimitedSupervisorAgentAliasParam",
            parameter_name=params["limited_supervisor_agent_alias"],
            string_value=limited_alias.attr_agent_alias_id,
            description=f"OSCAR limited supervisor agent alias ID for {self.env_name}",
        )

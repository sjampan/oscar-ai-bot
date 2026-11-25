# Copyright OpenSearch Contributors
# SPDX-License-Identifier: Apache-2.0

"""GitHub tool definitions as plain data, shared by the Classic stack and the AgentCore runtime.

Three tier-tagged groups: contributor (read-only), maintainer (create_tag, create_branch),
admin (all writes). The Lambda enforces authorization at runtime via group gate + function gate.
"""

ACTION_GROUPS = [
    {
        "name": "githubContributorOps",
        "description": "Read-only GitHub operations: PRs, issues, search, and maintainer lookup",
        "functions": [
            {
                "name": "get_pr_details",
                "description": "Get details of a pull request including title, state, author, reviewers, merge status, and CI check results.",
                "parameters": {
                    "repo": {
                        "type": "string",
                        "description": "Repository name",
                        "required": True,
                    },
                    "pr_number": {
                        "type": "string",
                        "description": "Pull request number",
                        "required": True,
                    },
                },
            },
            {
                "name": "list_prs",
                "description": "List pull requests for a repository. Can filter by state.",
                "parameters": {
                    "repo": {
                        "type": "string",
                        "description": "Repository name",
                        "required": True,
                    },
                    "state": {
                        "type": "string",
                        "description": "Filter by state: 'open', 'closed', or 'all'. Defaults to 'open'.",
                        "required": False,
                    },
                },
            },
            {
                "name": "get_issue_details",
                "description": "Get details of an issue including title, state, assignees, labels, and comments.",
                "parameters": {
                    "repo": {
                        "type": "string",
                        "description": "Repository name",
                        "required": True,
                    },
                    "issue_number": {
                        "type": "string",
                        "description": "Issue number",
                        "required": True,
                    },
                },
            },
            {
                "name": "list_issues",
                "description": "List issues for a repository. Can filter by state and labels.",
                "parameters": {
                    "repo": {
                        "type": "string",
                        "description": "Repository name",
                        "required": True,
                    },
                    "state": {
                        "type": "string",
                        "description": "Filter by state: 'open', 'closed', or 'all'. Defaults to 'open'.",
                        "required": False,
                    },
                    "labels": {
                        "type": "string",
                        "description": "Comma-separated label names to filter by",
                        "required": False,
                    },
                },
            },
            {
                "name": "search_issues",
                "description": "Search for issues using GitHub search syntax (already scoped to is:issue). Examples: 'is:open label:bug', 'author:username is:closed'.",
                "parameters": {
                    "query": {
                        "type": "string",
                        "description": "Issue search query using GitHub search syntax",
                        "required": True,
                    },
                    "sort": {
                        "type": "string",
                        "description": "Sort by: 'comments', 'reactions', 'created', 'updated'",
                        "required": False,
                    },
                    "order": {
                        "type": "string",
                        "description": "Sort order: 'asc' or 'desc'",
                        "required": False,
                    },
                },
            },
            {
                "name": "search_pull_requests",
                "description": "Search for pull requests using GitHub search syntax (already scoped to is:pr). Examples: 'is:open author:username', 'is:merged label:enhancement'.",
                "parameters": {
                    "query": {
                        "type": "string",
                        "description": "Pull request search query using GitHub search syntax",
                        "required": True,
                    },
                    "sort": {
                        "type": "string",
                        "description": "Sort by: 'comments', 'reactions', 'created', 'updated'",
                        "required": False,
                    },
                    "order": {
                        "type": "string",
                        "description": "Sort order: 'asc' or 'desc'",
                        "required": False,
                    },
                },
            },
            {
                "name": "get_repo_maintainers",
                "description": "Get the current maintainers of a repository by parsing its MAINTAINERS.md file. Returns GitHub handles and display names. Use this when the user asks to tag or mention maintainers (e.g. in bulk comments), or to look up who maintains a specific repo.",
                "parameters": {
                    "repo": {
                        "type": "string",
                        "description": "Repository name",
                        "required": True,
                    },
                    "organization": {
                        "type": "string",
                        "description": "GitHub organization (defaults to 'opensearch-project')",
                        "required": False,
                    },
                },
            },
        ],
    },
    {
        "name": "githubMaintainerOps",
        "description": "Repository maintainer operations: create tags and branches. Requires the requester to be a maintainer of the target repository.",
        "functions": [
            {
                "name": "create_ref",
                "description": "Create a lightweight Git tag or branch on a specific commit in a repository. Requires explicit user confirmation before execution. The commit can be a full or abbreviated SHA; abbreviated SHAs are resolved to the full SHA automatically. When the request originated from a GitHub @mention, pass github_requester, source_repo, and source_issue_number so the bot can verify the requester and respond on the issue.",
                "parameters": {
                    "repo": {
                        "type": "string",
                        "description": "Repository name",
                        "required": True,
                    },
                    "ref_type": {
                        "type": "string",
                        "description": "Type of ref to create: 'tag' or 'branch'",
                        "required": True,
                    },
                    "ref_name": {
                        "type": "string",
                        "description": "Name for the tag or branch (e.g., '3.12.0' or '3.12')",
                        "required": True,
                    },
                    "commit_sha": {
                        "type": "string",
                        "description": "Commit SHA (full or abbreviated) to create the ref from. If omitted, the HEAD of the default branch is used.",
                        "required": False,
                    },
                    "github_requester": {
                        "type": "string",
                        "description": "GitHub username of the person who @mentioned the bot on the issue. Required when the request comes from a GitHub webhook notification.",
                        "required": False,
                    },
                    "source_repo": {
                        "type": "string",
                        "description": "Repository where the bot was @mentioned (may differ from repo). Required when the request comes from a GitHub webhook notification.",
                        "required": False,
                    },
                    "source_issue_number": {
                        "type": "string",
                        "description": "Issue number where the bot was @mentioned. Required when the request comes from a GitHub webhook notification.",
                        "required": False,
                    },
                },
            },
        ],
    },
    {
        "name": "githubAdminOps",
        "description": "Admin-only operations: create/close issues, merge PRs, transfer issues, bulk comment, and bulk merge automated PRs with guardrail validation.",
        "functions": [
            {
                "name": "create_issue",
                "description": "Create an issue on a repository. Requires explicit user confirmation before execution.",
                "parameters": {
                    "repo": {
                        "type": "string",
                        "description": "Repository name",
                        "required": True,
                    },
                    "title": {
                        "type": "string",
                        "description": "Issue title",
                        "required": True,
                    },
                    "body": {
                        "type": "string",
                        "description": "Issue description body",
                        "required": False,
                    },
                    "labels": {
                        "type": "string",
                        "description": "Comma-separated label names to apply",
                        "required": False,
                    },
                    "assignees": {
                        "type": "string",
                        "description": "Comma-separated GitHub usernames to assign",
                        "required": False,
                    },
                },
            },
            {
                "name": "close_issue",
                "description": "Close an issue with a reason. Requires explicit user confirmation before execution.",
                "parameters": {
                    "repo": {
                        "type": "string",
                        "description": "Repository name",
                        "required": True,
                    },
                    "issue_number": {
                        "type": "string",
                        "description": "Issue number to close",
                        "required": True,
                    },
                    "reason": {
                        "type": "string",
                        "description": "Reason for closing: 'completed' or 'not_planned'. Defaults to 'completed'.",
                        "required": False,
                    },
                },
            },
            {
                "name": "merge_pr",
                "description": "Merge a pull request. Requires explicit user confirmation before execution. For automated PRs that fail guardrail checks, set force='true' to override guardrails and merge anyway (only after the user explicitly says 'force merge' or similar).",
                "parameters": {
                    "repo": {
                        "type": "string",
                        "description": "Repository name",
                        "required": True,
                    },
                    "pr_number": {
                        "type": "string",
                        "description": "Pull request number",
                        "required": True,
                    },
                    "merge_method": {
                        "type": "string",
                        "description": "Merge method: 'merge', 'squash', or 'rebase'. Defaults to 'merge'.",
                        "required": False,
                    },
                    "commit_title": {
                        "type": "string",
                        "description": "Custom merge commit title",
                        "required": False,
                    },
                    "commit_message": {
                        "type": "string",
                        "description": "Custom merge commit message",
                        "required": False,
                    },
                    "force": {
                        "type": "string",
                        "description": "Set to 'true' to override guardrail failures for automated PRs. Only use when the user explicitly requests a force merge.",
                        "required": False,
                    },
                },
            },
            {
                "name": "transfer_issue",
                "description": "Transfer an issue to another repository within the organization. Target repository must be within the configured organization. Requires explicit user confirmation before execution.",
                "parameters": {
                    "repo": {
                        "type": "string",
                        "description": "Source repository name",
                        "required": True,
                    },
                    "issue_number": {
                        "type": "string",
                        "description": "Issue number to transfer",
                        "required": True,
                    },
                    "target_repo": {
                        "type": "string",
                        "description": "Target repository name to transfer the issue to",
                        "required": True,
                    },
                },
            },
            {
                "name": "bulk_comment",
                "description": "Add the same comment to multiple issues or pull requests at once, even across different repositories. Useful for announcements, release notes, or campaign-style updates. Requires explicit user confirmation before execution.",
                "parameters": {
                    "issues": {
                        "type": "string",
                        "description": "Comma-separated list of repo#number pairs identifying the issues to comment on. Format: 'repo1#1,repo2#2,repo3#5'. Each entry is repo_name#issue_number. Example: 'opensearch-build#1,flow-framework#2,reporting#2'",
                        "required": True,
                    },
                    "body": {
                        "type": "string",
                        "description": "Comment body text (supports markdown)",
                        "required": True,
                    },
                },
            },
            {
                "name": "list_merge_candidates",
                "description": "Search for automated PRs (version increments and release notes) across all repositories in the organization for a given version. Validates each PR against safety guardrails (author, title pattern, version label, CI status, merge conflicts, draft state, version consistency) and returns a detailed report. Always call this before bulk_merge_prs.",
                "parameters": {
                    "version": {
                        "type": "string",
                        "description": "Version to search for (e.g., '3.6.0')",
                        "required": True,
                    },
                    "organization": {
                        "type": "string",
                        "description": "GitHub organization to search (defaults to 'opensearch-project')",
                        "required": False,
                    },
                },
            },
            {
                "name": "bulk_merge_prs",
                "description": "Merge all automated PRs that pass guardrail validation for the given version. Re-validates every PR before merging. PRs that fail any guardrail are skipped. CRITICAL: Only executes when confirmed=true. Use list_merge_candidates first to review PRs, then call this only after user confirmation.",
                "parameters": {
                    "version": {
                        "type": "string",
                        "description": "Version to merge PRs for (e.g., '3.6.0')",
                        "required": True,
                    },
                    "organization": {
                        "type": "string",
                        "description": "GitHub organization (defaults to 'opensearch-project')",
                        "required": False,
                    },
                    "confirmed": {
                        "type": "string",
                        "description": "REQUIRED: Must be 'true' to execute. Set to 'true' ONLY after user explicitly confirms.",
                        "required": True,
                    },
                },
            },
        ],
    },
]

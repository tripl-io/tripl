"""Tracker HTTP clients for implementation tickets (GH #258).

The Jira create / search / status helpers predate this module and stay in
``alerts_channels`` (the alert Jira destination shares them). What lives here is
what implementation tickets need on top:

* the Linear backend — create, find-by-marker, status, comment — over Linear's
  GraphQL API, authenticated exactly as the Linear ALERT destination is (the
  raw API key in ``Authorization``);
* a Jira comment call, so ``add_ticket_comment`` can post on either tracker.

Every function takes the transport (``post_json``) as its first argument, like
the helpers in ``alerts_channels``, so tests pass a fake and never open a
socket. None of them logs or echoes a credential: errors carry the GraphQL
message text only.
"""

from __future__ import annotations

import base64
from urllib.parse import quote

from tripl.worker.tasks.alerts_channels import PostJson
from tripl.worker.tasks.alerts_messages import _build_jira_adf_body

LINEAR_GRAPHQL_URL = "https://api.linear.app/graphql"

# Linear workflow state TYPES (stable across every team's custom workflow):
# triage, backlog, unstarted, started, completed, canceled.
LINEAR_COMPLETED_STATE_TYPE = "completed"


class LinearGraphQLError(ValueError):
    """Linear answered 200 with a GraphQL ``errors`` array."""


def _linear_headers(api_key: str) -> dict[str, str]:
    return {"Authorization": api_key, "Accept": "application/json"}


def _linear_call(
    post_json: PostJson,
    *,
    api_key: str,
    query: str,
    variables: dict[str, object],
) -> dict[str, object]:
    """Run one GraphQL operation and return its ``data`` object.

    Linear reports most failures (bad team id, revoked key surfaced as a
    GraphQL error, validation) as HTTP 200 with an ``errors`` array, which the
    shared transport would hand back as a success. Raise instead, with the
    messages only — the request itself carries the key.
    """
    response = post_json(
        LINEAR_GRAPHQL_URL,
        {"query": query, "variables": variables},
        headers=_linear_headers(api_key),
    )
    if not isinstance(response, dict):
        raise LinearGraphQLError("Linear returned no JSON body")
    errors = response.get("errors")
    if isinstance(errors, list) and errors:
        messages = [
            str(error.get("message"))
            for error in errors
            if isinstance(error, dict) and error.get("message")
        ]
        raise LinearGraphQLError("Linear error: " + ("; ".join(messages) or "unknown error"))
    data = response.get("data")
    if not isinstance(data, dict):
        raise LinearGraphQLError("Linear returned no data")
    return data


def _issue_triple(issue: object) -> tuple[str | None, str | None, str]:
    if not isinstance(issue, dict):
        return (None, None, "")
    issue_id = issue.get("id")
    identifier = issue.get("identifier")
    url = issue.get("url")
    return (
        issue_id if isinstance(issue_id, str) else None,
        identifier if isinstance(identifier, str) else None,
        url if isinstance(url, str) else "",
    )


def create_linear_issue(
    post_json: PostJson,
    *,
    api_key: str,
    team_id: str,
    title: str,
    description: str,
) -> tuple[str | None, str | None, str]:
    """Create an issue on ``team_id``; ``(issue_id, identifier, url)``.

    Raises ``LinearGraphQLError`` unless Linear says ``success: true`` AND hands
    back the issue's ``id`` and ``identifier``. A 200 with ``success: false``
    (or a payload without the issue) created nothing we can point at; returning
    ``(None, None, "")`` would let the caller persist a ticket row with no
    external id, which then counts as "this branch has its ticket" and blocks
    every retry. Raising leaves nothing persisted, so the next merge tries again.
    """
    mutation = (
        "mutation IssueCreate($input: IssueCreateInput!) {"
        " issueCreate(input: $input) { success issue { id identifier url } }"
        " }"
    )
    data = _linear_call(
        post_json,
        api_key=api_key,
        query=mutation,
        variables={"input": {"teamId": team_id, "title": title, "description": description}},
    )
    created = data.get("issueCreate")
    if not isinstance(created, dict) or created.get("success") is not True:
        raise LinearGraphQLError("Linear did not create the issue (success is not true)")
    issue_id, identifier, url = _issue_triple(created.get("issue"))
    if not issue_id or not identifier:
        raise LinearGraphQLError("Linear created no issue id and identifier")
    return (issue_id, identifier, url)


def find_linear_issue_by_marker(
    post_json: PostJson,
    *,
    api_key: str,
    team_id: str,
    marker: str,
) -> tuple[str | None, str | None, str]:
    """The team's issue whose description carries ``marker``, or ``(None, None, "")``.

    The Linear twin of ``_find_jira_issue_by_label``: Linear's create takes no
    idempotency key either, so a redelivered create asks first. The marker is
    written into the description by ``create_linear_issue``'s caller; it is a
    uuid-derived token, so a substring match cannot hit an unrelated issue.
    """
    query = (
        "query FindIssue($filter: IssueFilter) {"
        " issues(filter: $filter, first: 1) { nodes { id identifier url } }"
        " }"
    )
    data = _linear_call(
        post_json,
        api_key=api_key,
        query=query,
        variables={
            "filter": {
                "team": {"id": {"eq": team_id}},
                "description": {"contains": marker},
            }
        },
    )
    issues = data.get("issues")
    nodes = issues.get("nodes") if isinstance(issues, dict) else None
    first = nodes[0] if isinstance(nodes, list) and nodes else None
    return _issue_triple(first)


def get_linear_issue_state_type(
    post_json: PostJson,
    *,
    api_key: str,
    issue_id: str,
) -> str | None:
    """The issue's workflow state TYPE, lowercased (``completed`` when done).

    Polled on the state type rather than its name for the reason the Jira poll
    reads the status category: any team's "Done", "Shipped" or "Released" is a
    ``completed`` state.
    """
    query = "query IssueState($id: String!) { issue(id: $id) { state { type } } }"
    data = _linear_call(post_json, api_key=api_key, query=query, variables={"id": issue_id})
    issue = data.get("issue")
    state = issue.get("state") if isinstance(issue, dict) else None
    state_type = state.get("type") if isinstance(state, dict) else None
    if not isinstance(state_type, str) or not state_type.strip():
        return None
    return state_type.strip().lower()


def add_linear_comment(
    post_json: PostJson,
    *,
    api_key: str,
    issue_id: str,
    body: str,
) -> bool:
    """Post a comment on a Linear issue; True when Linear reports success."""
    mutation = (
        "mutation CommentCreate($input: CommentCreateInput!) {"
        " commentCreate(input: $input) { success }"
        " }"
    )
    data = _linear_call(
        post_json,
        api_key=api_key,
        query=mutation,
        variables={"input": {"issueId": issue_id, "body": body}},
    )
    created = data.get("commentCreate")
    return isinstance(created, dict) and created.get("success") is True


def add_jira_comment(
    post_json: PostJson,
    *,
    base_url: str,
    auth_email: str,
    api_token: str,
    issue_key: str,
    body_text: str,
) -> bool:
    """Post a comment on a Jira issue (REST v3, ADF body); True when Jira returns its id."""
    credentials = base64.b64encode(f"{auth_email}:{api_token}".encode()).decode()
    response = post_json(
        f"{base_url}/rest/api/3/issue/{quote(issue_key, safe='')}/comment",
        {"body": _build_jira_adf_body(body_text)},
        headers={"Authorization": f"Basic {credentials}", "Accept": "application/json"},
    )
    return isinstance(response, dict) and isinstance(response.get("id"), str)

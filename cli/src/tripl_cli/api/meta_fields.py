"""Branch-scoped catalog metadata definitions."""

from tripl_cli.api.request import ApiRequest

LIST = "/projects/{slug}/meta-fields"
ENDPOINTS = (("get", LIST),)


def list_fields(slug: str, branch: str) -> ApiRequest:
    return ApiRequest("GET", LIST.format(slug=slug), params={"branch": branch})

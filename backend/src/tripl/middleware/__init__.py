"""ASGI middleware and request-scoped context.

Request-id, plan-branch context, organization context and the org-qualified
path rewrite, security headers, static caching, rate limiting.
"""

from tripl.middleware.branch_context import bound_branch, current_branch
from tripl.middleware.org_context import (
    ORG_REWRITE_PREFIXES,
    ORG_SCOPE_STATE_KEY,
    OrgContextMissing,
    OrgRef,
    bind_org,
    bound_org,
    current_org,
    current_org_id,
    path_org_slug,
    require_org_id,
    reset_org,
)
from tripl.middleware.org_path_rewrite import OrgPathRewriteMiddleware
from tripl.middleware.rate_limit import RateLimitExceeded, login_rate_limiter, register_rate_limiter
from tripl.middleware.request_id import RequestIDMiddleware, current_request_id
from tripl.middleware.security_headers import SecurityHeadersMiddleware
from tripl.middleware.static_cache import StaticCacheMiddleware

__all__ = [
    "ORG_REWRITE_PREFIXES",
    "ORG_SCOPE_STATE_KEY",
    "OrgContextMissing",
    "OrgPathRewriteMiddleware",
    "OrgRef",
    "RateLimitExceeded",
    "RequestIDMiddleware",
    "SecurityHeadersMiddleware",
    "StaticCacheMiddleware",
    "bind_org",
    "bound_branch",
    "bound_org",
    "current_branch",
    "current_org",
    "current_org_id",
    "current_request_id",
    "login_rate_limiter",
    "path_org_slug",
    "register_rate_limiter",
    "require_org_id",
    "reset_org",
]

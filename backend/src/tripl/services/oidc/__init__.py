"""OpenID Connect sign-in, the parts every provider shares.

Community's "Sign in with Google" (``google_login_service``) and an
organization's single sign-on (``sso_login_service``) both build on this:
discovery and JWKS fetches (``idp_http``), the code exchange and id_token checks
(``id_tokens``), the flow's error codes and PKCE (``flow``), and taking over an
account nobody proved the address of (``accounts``). Nothing here knows about
organizations.
"""

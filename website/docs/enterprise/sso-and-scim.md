---
title: Single sign-on and provisioning
sidebar_position: 1
---

# Single sign-on and provisioning

:::info Enterprise
This page describes a feature of the [Enterprise edition](../editions.md).
:::

## Single sign-on (OIDC and SAML) {#single-sign-on}

An organization can let its people sign in through its own identity provider
(IdP) over **OpenID Connect** or **SAML 2.0**, and can require it. Pick the
protocol under **Protocol** on the settings page; an organization uses one at a
time, and the other protocol's saved settings are kept for switching back.
Prefer OpenID Connect when your IdP offers both. Single sign-on is set up per
organization, under **Settings → Organization → Single sign-on**, and only an
organization **owner** can see or change it (an admin gets a notice; the API
answers `403`). It needs a browser session: an API key cannot read or change
it. No environment variable is involved.

### Set up the identity provider (OpenID Connect) {#set-up-the-identity-provider}

In your IdP, register tripl as a **web application** that uses the
authorization code flow (any standards-compliant OpenID Connect provider
works). Use this redirect URI, shown on the settings page:

```
{APP_BASE_URL}/api/v1/auth/sso/{org}/callback
```

where `{APP_BASE_URL}` is the instance's public address (see
[Configuration](../run/configuration.md)) and `{org}` is the organization's
slug, for example `https://tripl.example.com/api/v1/auth/sso/acme/callback`.
The IdP must send the `email` claim and `email_verified: true` in the ID token.

Then fill in, on the settings page:

| Field | Value |
|---|---|
| **Issuer URL** | The IdP's issuer, for example `https://idp.example.com`. It must use `https` and, with `OUTBOUND_PUBLIC_HOSTS_ONLY=true` or on a hosted instance, resolve to a public address; this is checked when you save and again at every use. tripl reads `{issuer}/.well-known/openid-configuration`, and the document's `issuer` must equal this value exactly. |
| **Client ID** | The client ID the IdP issued for tripl. |
| **Client secret** | The client secret. It is stored encrypted with the operator's `ENCRYPTION_KEY`, is write-only (the page and the API only say whether one is stored) and never appears in audit entries. Type a new one to replace it. |
| **Scopes** | `openid email profile` by default. `openid` is required. |

**Test connection** fetches the discovery document and checks that the issuer
matches and the authorization, token and key endpoints are there. The token and
key endpoints are always taken from the discovery document. A failed test
answers a short code and a fixed text, never what the host answered. The test
and domain verification share a small rate limit (10 a minute).

API: `GET` and `PUT /api/v1/orgs/{org}/sso` (the response carries
`client_secret_configured`, never the secret) and `POST /api/v1/orgs/{org}/sso/test`.

### Set up SAML 2.0 {#saml}

Choose **SAML 2.0** under **Protocol**. The page shows three values to give
your IdP (`{org}` is the organization's slug):

| Value | Address |
|---|---|
| **Entity ID** (also called audience, SP entity ID or Identifier) | `{APP_BASE_URL}/api/v1/auth/sso/{org}/saml/metadata` |
| **ACS URL** (Assertion Consumer Service, Reply URL; HTTP-POST binding) | `{APP_BASE_URL}/api/v1/auth/sso/{org}/saml/acs` |
| **Metadata URL** (tripl's service-provider metadata, for IdPs that can read it) | `{APP_BASE_URL}/api/v1/auth/sso/{org}/saml/metadata` |

Configure the application at the IdP so that:

- **The NameID is the user's email address** (format `emailAddress`), or the
  email is sent in an attribute whose name you enter under **Email attribute**.
  Without an email attribute, a NameID in any other format (persistent,
  unspecified) is refused (`email_missing`), even if it looks like an address.
- **Assertions are signed**, with RSA or ECDSA and SHA-256 or stronger. A
  response signed only on the outside, or signed with SHA-1, is refused.
- **Assertions are not encrypted.** tripl does not support encrypted
  assertions and refuses them (`encrypted_assertion_unsupported`).
- Sign-in starts from tripl. **IdP-initiated sign-in** (clicking the app tile
  in the IdP's portal) is not supported: such a response is refused
  (`saml_unsolicited`). Point the IdP's app tile at tripl's sign-in page
  instead, where **Sign in with SSO** starts the sign-in.

tripl does not sign its authentication requests and has no service-provider
key or certificate; leave request signing off at the IdP.

**SAML needs tripl served over https** (`APP_BASE_URL` starting with
`https://`; `http://localhost` works for local trials). The IdP posts its
answer back cross-site, and the browser only sends the sign-in cookie that
proves the answer belongs to it when that cookie is `Secure`, which a browser
keeps only over https. On a plain-http address every SAML sign-in fails with
`invalid_state`.

Then fill in the IdP's values. The simplest way is to download the IdP's
**metadata XML**, paste it under **Import IdP metadata** and press
**Import**: tripl reads the entity ID, the HTTP-Redirect single sign-on URL
and the signing certificates from it and fills the fields in. Nothing is saved
until you press **Save changes**, and tripl never fetches a metadata URL; it
reads only what you paste. Or type them in:

| Field | Value |
|---|---|
| **IdP entity ID** | The IdP's issuer, exactly as it appears in its assertions (at most 507 characters). |
| **SSO URL** | The IdP's single sign-on URL for the HTTP-Redirect binding. `https` only. |
| **Signing certificates** | The IdP's signing certificate in PEM form (`-----BEGIN CERTIFICATE-----` ... `-----END CERTIFICATE-----`). Paste several, one after another, while the IdP rotates its key: an assertion signed with any of them is accepted. Remove the old one once the IdP has switched. |
| **NameID format** | `Email address` by default. Choose another only together with an **Email attribute**. |
| **Email attribute** | Optional. The attribute carrying the email, when the NameID is not an email address (for example `email`, or `http://schemas.xmlsoap.org/ws/2005/05/identity/claims/emailaddress` for Microsoft Entra ID). |

After saving, the page lists each certificate with its SHA-256 fingerprint and
expiry date, and flags one that has expired or expires within 30 days. A
certificate is public, so it is shown in full; compare the fingerprint with
the one your IdP shows. **Check settings** checks the saved settings: the
certificates parse and have not expired and the SSO URL uses `https`. It does
not contact the IdP.

A certificate is checked for expiry when you add it (or switch to SAML), not
on every save, so a certificate that expired since does not stop you from
turning single sign-on off or changing other settings. Changing the **IdP
entity ID**, switching the **Protocol**, or saving certificates that keep none
of the saved ones counts as a new identity provider: every member's link to
the old one is removed, and each member confirms the link again (signed in to
their tripl account) at their next SSO sign-in. Rotate keys by adding the new
certificate next to the old one first, so the links survive.

The IdP's address is taken as its email: the IdP is trusted to have verified
it, as it is the IdP's own directory. It must still be at one of the
organization's [verified domains](#verify-your-email-domains).

#### Okta

1. **Applications → Create App Integration → SAML 2.0**.
2. **Single sign-on URL** = the ACS URL (keep **Use this for Recipient URL and
   Destination URL** checked); **Audience URI (SP Entity ID)** = the entity ID.
3. **Name ID format** = `EmailAddress`, **Application username** = `Email`.
4. Leave **Assertion Encryption** set to `Unencrypted`; the response and the
   assertion signatures use `RSA-SHA256` by default, keep that.
5. After saving, open **Sign On → View SAML setup instructions** (or the
   **Metadata URL**), copy the metadata XML and import it in tripl.

#### Microsoft Entra ID

1. **Enterprise applications → New application → Create your own
   application → Integrate any other application you don't find in the
   gallery**, then **Single sign-on → SAML**.
2. **Basic SAML Configuration**: **Identifier (Entity ID)** = the entity ID,
   **Reply URL (Assertion Consumer Service URL)** = the ACS URL. Leave **Sign on
   URL** and **Relay State** empty.
3. **Attributes & Claims**: set **Unique User Identifier (Name ID)** to
   `user.mail` with the `Email address` format, or keep the default and enter
   `http://schemas.xmlsoap.org/ws/2005/05/identity/claims/emailaddress` as the
   **Email attribute** in tripl.
4. **SAML Certificates**: set **Signing Option** to **Sign SAML assertion** (or
   **Sign SAML response and assertion**) and the algorithm to `SHA-256`. Leave
   token encryption off.
5. Download **Federation Metadata XML** and import it in tripl.

#### Google Workspace

1. In the Admin console, **Apps → Web and mobile apps → Add app → Add custom
   SAML app**.
2. On **Google Identity Provider details**, download the metadata (or copy the
   SSO URL, entity ID and certificate) and import it in tripl.
3. **Service provider details**: **ACS URL** = the ACS URL, **Entity ID** = the
   entity ID, **Name ID format** = `EMAIL`, **Name ID** =
   `Basic Information > Primary email`. Leave **Signed response** unchecked: Google signs the
   assertion either way.
4. Turn the app **ON** for the organizational units whose people should sign in.

API: the same `GET` and `PUT /api/v1/orgs/{org}/sso` with `protocol: "saml"`
and the `saml_*` fields, `POST /api/v1/orgs/{org}/sso/saml/metadata-import`
and `POST /api/v1/orgs/{org}/sso/test`. See the
[agent API guide](../integrate/agent-api-guide.md) for the fields.

### Verify your email domains

Single sign-on accepts only addresses at the organization's **verified**
domains. Add a domain (for example `example.com`) under **Email domains**; the
page shows a DNS TXT record to publish:

| Name | Value |
|---|---|
| `_tripl-verification.example.com` | `tripl-verification=<token>` |

Publish it with your DNS provider, wait for it to propagate, and press
**Verify**. tripl looks the record up and marks the domain verified when one of
its values matches. A domain can be verified by only one organization on the
instance: adding or verifying a domain another organization has already
verified is refused with `409`. Removing a domain stops single sign-on for its
addresses.

API: `GET`/`POST /api/v1/orgs/{org}/sso/domains`,
`DELETE /api/v1/orgs/{org}/sso/domains/{id}` and
`POST /api/v1/orgs/{org}/sso/domains/{id}/verify`.

### Turn it on

**Enable single sign-on** is available once the provider is saved (for
OpenID Connect the issuer, client ID and client secret; for SAML the IdP
entity ID, SSO URL and a certificate) and at least one domain is verified (the
API refuses it otherwise). From then on, the sign-in page's **Sign in with
SSO** asks for the work email, finds the organization whose verified domain it
is, and sends the browser to the IdP. It comes back within ten minutes or not
at all.

- **OpenID Connect**: the redirect carries a single-use `state`, a `nonce` and
  a PKCE (S256) challenge. When the IdP sends the user back, tripl checks the
  ID token: its signature against the IdP's published keys (RS256 or ES256),
  issuer, audience (the client ID), expiry, and nonce. The email must be
  verified by the IdP.
- **SAML 2.0**: the redirect carries an authentication request with a random
  ID, and a single-use state bound to this browser. The IdP posts its response
  to the ACS URL, and tripl checks, failing closed: the assertion's signature
  against the configured certificates (SHA-256 or stronger); exactly one
  assertion, unencrypted; the issuer (the IdP entity ID); the response's
  destination and the subject confirmation's recipient (the ACS URL); that it
  answers this very request (`InResponseTo`); the audience (tripl's entity
  ID); the validity window (two minutes of clock skew); a success status; and
  that the assertion was not used before. Everything is read from the signed
  assertion only.

The email must belong to one of the organization's verified domains. Then:

- **A known identity** (the same IdP subject was linked to an account before)
  signs that account in.
- **A new address** gets an account created on the spot (just-in-time
  provisioning), with the address marked verified. It joins the organization as
  a **member**, so the organization's
  [default access to projects](../administer/admin-guide.md#default-access-to-projects) applies. It never
  becomes a platform admin. The account has no usable password until its owner
  sets one with **Forgot your password?**.
- **An address that already has an account** is not signed in straight away.
  The browser goes to a confirmation page, **Link your account to single
  sign-on?**, and the person must **sign in to that account first** (with its
  password, or however they usually sign in), then confirm. Signing in at the
  IdP alone is not enough: whoever runs the IdP of a verified domain could
  otherwise name any address of that domain and take the account over, a
  platform admin's included. Confirming links the identity to the account, adds
  the organization membership (as a member) if it is missing, marks the address
  verified and replaces the password session with a single sign-on session.
  Cancelling changes nothing, and the request expires after ten minutes. An
  existing account is therefore linked only for a DNS-verified domain and only
  after its owner confirms it.
- **An account whose address was never verified** (on a hosted instance, a
  sign-up that never confirmed its email) belongs to nobody yet: anyone can
  register any address there. The IdP's verified address is the first proof,
  so confirming the link needs no sign-in and **takes the account over clean**:
  its password stops working, and all of its sessions, API keys and pending
  reset or verification links are dropped. Whoever registered the address
  first keeps nothing. A platform admin's account is never treated this way.

A failed sign-in comes back to the sign-in page with a short reason (the
attempt expired, the IdP refused, the address is missing or not verified, the
domain is not the organization's, the token or SAML response did not verify,
the SAML assertion was encrypted, replayed or not requested by tripl, the
account was removed from the organization, too many attempts, or single
sign-on is off).
The IdP's own error text is never shown. Sign-in through the IdP has its own
rate limit (20 requests a minute per address, two per sign-in), separate from
password sign-in.

A session opened this way is marked as a single sign-on session for that
organization, whichever protocol it used. Signing in with a password still
works where single sign-on is not required.

**Switching the protocol** takes effect from the next sign-in. An identity is
recorded per IdP (the OpenID Connect issuer and subject, or the SAML IdP
entity ID and NameID), so identities linked through the other protocol do not
carry over: someone with an existing account goes through the
[link confirmation](#turn-it-on) once more. An account that single sign-on
created has no password, so its owner first sets one with **Forgot your
password?** to confirm the link. Switch while few people depend on it, or
expect those confirmations.

### Require single sign-on {#sso-required}

**Require single sign-on** makes the organization usable only from a session
that signed in through its IdP. Any other session gets
`403 This organization requires single sign-on` inside the organization (the
app shows a **Sign in with SSO** button instead), whether it signed in with a
password or through another organization's single sign-on. The `/auth/*`
routes stay open.

- **Owners' sessions are exempt (break-glass).** An organization owner can
  always sign in with a password and use the organization, so a broken IdP
  never locks the organization out: the owner signs in and fixes or turns off
  the setting. Keep owner passwords strong. The exemption covers browser
  sessions only, not API keys.
- **A platform admin's [read-only step-in](./platform-console.md#read-only-step-in)** is exempt.
- **API keys are revoked, owners' included.** Turning the setting on revokes
  every key bound to the organization that was not created from a single
  sign-on session of this organization, whoever holds it. The response and the
  audit entry say how many. Such keys are refused with `403` from then on as
  well.
- **New keys need a single sign-on session.** While the setting is on, keys for
  the organization are created only from a session that signed in through its
  IdP. An owner signed in with a password (break-glass) gets `403` there too.

Turning single sign-on off also stops requiring it.

### Members and identities

Removing a member from the organization also removes the single sign-on
identities that linked their account to this organization, besides the rest of
what [removing a member](../administer/admin-guide.md#members) takes away. Signing in through the
IdP again does **not** bring them back: the sign-in is refused
(`membership_removed`) until they accept a new invitation to the organization.

### Single sign-on audit

Every change is recorded in the organization's audit log, without secrets:
`org.sso.*` for the configuration, domains and the two switches, and
`user.sso_login`, `user.sso_provision` (a new account) and `user.sso_link` (an
existing account linked after confirmation; `reclaimed_unverified_account` says
whether it was an unverified account taken over) for sign-ins.

## Provisioning (SCIM 2.0) {#scim}

An organization can let its identity provider (IdP) manage who belongs to it
over **SCIM 2.0**: the IdP creates members, updates their names, deactivates
them when they leave, and pushes groups. A member's email address cannot be
changed over SCIM (see [below](#scim-usernames)). SCIM is set up per
organization under **Settings → Organization → Provisioning**, and only an
organization **owner** can see or change it (an admin gets a notice; the API
answers `403`). It pairs with [single sign-on](#single-sign-on): people SCIM
creates have no usable password and sign in through the IdP.

### Set it up

1. Verify at least one email domain under
   [single sign-on](#verify-your-email-domains). SCIM creates new accounts only
   at the organization's verified domains.
2. Open **Settings → Organization → Provisioning** and copy the **Base URL**:
   `<your tripl address>/scim/v2/<organization slug>`.
3. Click **Create token**. The token (it starts with `tripl_scim_`) is shown
   **once**: copy it into the IdP straight away. tripl keeps only a hash of it
   and lists it afterwards by its first characters, with when it was created
   and last used. Revoke a token there when it leaks or the IdP app is removed;
   the IdP's next request with it is refused. An organization can have several
   tokens, for example one per IdP app or while rotating.
4. Configure the IdP:
   - **Okta**: in the app's **Provisioning** tab, turn on SCIM provisioning;
     SCIM connector base URL = the base URL, unique identifier field for users
     = `userName`, supported provisioning actions **Push New Users**, **Push
     Profile Updates** and **Push Groups**, authentication mode **HTTP
     Header**, and paste the token as the Bearer token. Then turn on **Create
     Users**, **Update User Attributes** and **Deactivate Users** under **To
     App**.
   - **Microsoft Entra ID (Azure AD)**: in the enterprise application, open
     **Provisioning**, set the mode to **Automatic**, Tenant URL = the base URL
     and Secret Token = the token, then **Test Connection**. Map `userName` to
     the user's email address (usually `userPrincipalName` or `mail`).
   - **Any other SCIM 2.0 client**: send `Authorization: Bearer <token>` with
     `Content-Type: application/scim+json` (or `application/json`) to the base
     URL. `GET /ServiceProviderConfig`, `/ResourceTypes` and `/Schemas`
     describe what is supported.
5. Assign people and groups to the app in the IdP.

A token works only for its own organization, and only on the SCIM endpoints:
signed-in sessions and API keys are refused there. A suspended organization's
SCIM endpoint answers `403`.

### What is supported

| Resource | Operations | Attributes |
|---|---|---|
| Users | list, read, create, replace (`PUT`), update (`PATCH`), delete | `userName` (the email address, lowercased; cannot change), `name.givenName`, `name.familyName`, `name.formatted`, `displayName`, `externalId`, `active`; `emails` is returned as a copy of `userName`, and values sent in it are ignored |
| Groups | list, read, create, replace, update, delete | `displayName`, `members`, `externalId` |

Lists support `startIndex`/`count` paging (at most 200 per page). Users can be
filtered with `userName eq "…"` and `externalId eq "…"`. `PATCH` accepts the
shapes Okta and Entra ID send, including an operation without a `path`,
`active` as the string `"False"`, and removing a group member by the path
`members[value eq "<id>"]`. Bulk requests, sorting, ETags and password changes
are not supported. Errors follow SCIM's error format (RFC 7644 §3.12), for
unknown endpoints (`404`) and oversized bodies (`413`) too. A `PUT` that leaves
out `active` does not change whether the user is active; a `POST` that leaves it
out creates an active user.

#### Email addresses cannot change {#scim-usernames}

A user's `userName` is their tripl account's email address, which the account
keeps across every organization it belongs to. SCIM cannot change it: a `PUT`
or `PATCH` with a different `userName` answers `400` (`mutability`). To move
someone to a new address, provision the new address as a new user and
deactivate the old one. **Entra ID** sends such a change when a user's UPN (or
whichever attribute you mapped to `userName`) changes; that update then fails
in the provisioning log until the user is re-provisioned under the new address.

A user's SCIM `id` is their tripl account id. The IdP sees only accounts that
belong to this organization, or that it provisioned and later deactivated;
another organization's members are never visible.

### Which accounts SCIM creates or links

- **A new address at a verified domain**: tripl creates the account, marks the
  address verified and adds it to the organization as a **member**. It has no
  usable password; the person signs in through single sign-on. A SCIM account
  is never a platform admin.
- **Any address at any other domain**: refused (`400 invalidValue`, *email
  domain not verified for this organization*), whether an account exists for
  it or not, so SCIM cannot be used to find out which addresses have tripl
  accounts. The one exception is someone who is **already a member** of the
  organization: they are linked as they are.
- **An address at a verified domain that already has an account**: the
  account is linked and added to the organization as a member. Its password is
  not changed, and neither is its name on creation. But if nobody ever
  confirmed that address (an unverified hosted sign-up), tripl treats the
  account as a squatter's and **takes it over**, as a verified single sign-on
  sign-in does: its password is replaced with an unusable one, all its sessions
  and pending reset and verification links end, **all its API keys are
  revoked**, and the address is marked verified.

Later name updates (`displayName`, `name.*` in a `PUT` or `PATCH`) change the
account's name only for an address at one of the organization's verified
domains; for any other address they are only echoed back to the IdP.

Provisioning someone also lifts the block that keeps a
[removed member](#members-and-identities) from coming back through single
sign-on — except for someone an owner or admin removed by hand (below).

#### People removed by hand

When an owner or admin removes a member in tripl, the IdP cannot undo it. SCIM
shows the user as inactive, and a request to activate them (or to create them
again) answers `409` (`mutability`) until they accept a new invitation. Once
they are back, the IdP manages them as before.

### Deactivation {#scim-deprovisioning}

When the IdP deactivates a user (`active: false`) or deletes them, tripl
removes them from the organization exactly as [removing a
member](../administer/admin-guide.md#members) does: their API keys for the organization are
revoked, their project access and group memberships are dropped, and their
single sign-on identities for the organization are deleted. The account itself
is **kept** (it may belong to other organizations), and so is the SCIM link,
marked inactive: the IdP can still read the user (`active: false`), including
after a `DELETE`. Reactivating the user, or a `POST` for them after a `DELETE`,
adds them back as a **member** under the same `id`.

The organization's last **owner** cannot be deactivated: SCIM answers `409`
(`mutability`) and nothing changes. Transfer ownership first.

### Groups from the IdP

Groups the IdP pushes appear under **Settings › Organization ›
[Groups](../administer/admin-guide.md#groups)** with a **Managed by SCIM** badge. Their name and members
change, and they are deleted, only through SCIM: the page offers no rename,
member edit or delete for them, and the API answers `409` to each. Only their
description stays editable in tripl. Only organization members can be in them.

The IdP sees **every** group of the organization, including ones created in
tripl, and IdPs match groups by name. The first time the IdP writes to a group
(for example, pushes a group whose name matches one you created by hand), that
group becomes managed by SCIM from then on. Rename a hand-made group first if
you want to keep it out of the IdP's hands.

### Admin group

Under **Admin group** on the Provisioning page, an owner can pick one of the
organization's groups (usually one the IdP pushes). Its members become
organization **admins**; someone removed from it goes back to **member**. The
roles are recomputed on every change to the group's members. Owners are never
promoted or demoted by it, and no group maps to owner. Choose **No admin
group** to turn the mapping off.

### Provisioning audit

Creating and revoking tokens is recorded as `org.scim.token_create` and
`org.scim.token_revoke`. Every change SCIM makes (accounts created or linked,
memberships, deactivation, groups and roles) is recorded in the organization's
audit log with no acting user and `via: "scim"` plus the prefix of the token
that made it. SCIM requests are rate-limited per token, and failed
authentications per client address.

A token belongs to the owner who created it. When that person stops being an
owner (removed, deactivated, demoted, or after transferring ownership), their
tokens are revoked at once and recorded as `org.scim.token_revoke` with
`reason: "creator_no_longer_owner"`. Create a new token as a remaining owner and
update the IdP.

## Security notes

### Single sign-on (OIDC)

An organization owner can connect the organization to an OpenID Connect
identity provider (see [Single sign-on](#single-sign-on)).
The controls that matter for security:

| Property | Behaviour |
|---|---|
| Who configures it | Organization **owners** only, from a browser session. Admins, members and API keys get `403`. Every change is audited (`org.sso.*`) without the secret. |
| Outbound requests | Discovery, key (JWKS) and token requests go only to `https` URLs, follow no redirects, time out after 10 seconds and cap the response size. With `OUTBOUND_PUBLIC_HOSTS_ONLY=true` or on a hosted instance a host that resolves to a private, loopback or link-local address is refused when the issuer is saved, and checked again when the request is made, as for organization mail and AI endpoints; the request then connects to the very address that was checked (TLS and `Host` keep the hostname), so a name that re-resolves between the check and the connection (DNS rebinding) cannot reach an internal address. The owner's connection test answers a fixed text per error code, and it and domain verification are rate-limited. The discovery document's `issuer` must equal the configured issuer, and the token and key endpoints are taken from it. |
| Domains | Only addresses at a domain the organization proved with a DNS TXT record (`_tripl-verification.<domain>` = `tripl-verification=<token>`) are accepted. A domain can be verified by one organization per instance. |
| Login state | Each attempt stores a keyed HMAC digest of its `state` (like session tokens), a `nonce` and an encrypted PKCE (S256) verifier. The state is single use, bound to the organization and expires after 10 minutes. The return address (`next`) must be a relative path on the same origin. |
| ID token | Verified with the provider's published keys: `RS256` or `ES256` only (the algorithm comes from the key, `none` is refused), issuer, audience = client ID, `azp` = client ID whenever present (and required with several audiences), expiry, issued-at with 60 seconds of leeway, and the nonce. The token must carry `email` with `email_verified: true`, at one of the organization's verified domains. |
| Errors | A failed sign-in returns to `/auth?sso_error=<code>` with a fixed code. The provider's error text is never echoed. |
| New accounts | Created with a verified address, as organization **members**, never as platform admins. Their password is a scrypt hash of a random secret, so password sign-in takes the same time for them as for any account. |
| Existing accounts | Never linked or signed in automatically. The browser gets a single-use link request (10 minutes), and confirming it on `/sso/link` needs a **session of that account** (`401` otherwise): the provider's sign-in alone proves nothing about the account, since whoever runs a verified domain's provider can name any of its addresses. Only then is the identity linked, and the proving session is replaced by the single sign-on one. |
| Unverified accounts | An account whose address was never verified (a hosted sign-up) is not anyone's yet, so the provider's verified address takes it over clean: the password becomes unusable and every session, API key and pending reset or verification token of the account is dropped before linking. Platform admins are never treated this way. |
| Sessions | A session records how it signed in (`password` or `sso`) and, for single sign-on, which organization. |
| Requiring SSO | With **Require single sign-on**, a session that did not sign in through the organization's provider gets `403 This organization requires single sign-on` inside it. Organization owners' browser sessions are exempt (break-glass), as is a platform admin's read-only step-in. Turning it on revokes **every** organization API key that was not created from a single sign-on session of the organization, owners' included, and such keys are refused with `403`; new keys need such a session, for owners too. |
| Removing a member | Also deletes their single sign-on identities for the organization, and signing in through the provider again does not re-add them until they accept a new invitation. |
| Rate limits | Start and callback share their own bucket (20 a minute per address), apart from password sign-in; an empty bucket redirects to `/auth?sso_error=rate_limited`. Confirming a link is on the login bucket. |

### Single sign-on (SAML 2.0)

An organization can use SAML 2.0 instead of OpenID Connect (see
[Set up SAML 2.0](#saml)). Configuration, domains,
account resolution (new, existing and unverified accounts), sessions, requiring
SSO, removing a member and rate limits are exactly as in the table above; what
differs is how the IdP's answer is verified. XML signatures are checked with
`signxml` over `lxml` (no `xmlsec1`), and every check fails closed with a
generic `sso_error` code.

| Property | Behaviour |
|---|---|
| Trust | The IdP's signing certificates are configured by the owner (pasted, or read from pasted metadata; tripl never fetches metadata, so there is no outbound request). Several can be configured for key rotation. They are public and stored as they are; there is no SP key, and tripl's authentication requests are unsigned. A new certificate is checked (it parses, has not expired) when it is saved or when SAML is switched on; a stored one that has expired since does not block other changes, such as turning SSO off. |
| Trust anchor change | Switching the protocol, changing the IdP entity ID, or saving a certificate set that keeps none of the saved certificates deletes the organization's linked identities and pending link tickets of the old provider (the count is in the `org.sso.update` audit entry). Linked members then confirm the link again from their own session, so an owner who points SAML at a key they hold cannot sign in as an already-linked member. |
| Request binding | Each sign-in stores a random request ID on the single-use, 10-minute login state (the state's digest, as for OpenID Connect). The state goes to the IdP as `RelayState` and is bound to the browser by a cookie scoped to `/api/v1/auth/sso/`, `HttpOnly`, `Secure`, `SameSite=None` (the IdP's POST back is cross-site, so a `Lax` cookie would not be sent). A browser keeps a `Secure` cookie only over https (or on `localhost`), so SAML sign-in needs tripl served over https. The response's `InResponseTo` must equal the stored request ID; IdP-initiated (unsolicited) responses are refused (`saml_unsolicited`). |
| Parsing | The posted response is capped in size before decoding, and parsed with entity resolution, DTD loading and network access off and huge trees refused. Any `DOCTYPE` is refused, which rules out entity expansion (billion laughs) and external entities (XXE). |
| Signature | The **assertion** itself must carry a valid enveloped signature by one of the configured certificates; a signature over the response alone is not enough. RSA and ECDSA with SHA-256 or stronger only; SHA-1 is refused. Every later check reads the element the signature verification returned, never the posted document, so a signed assertion moved elsewhere and an unsigned one put in its place (XML signature wrapping) are refused. Exactly one assertion is accepted; encrypted assertions are not supported and are refused (`encrypted_assertion_unsupported`). |
| Assertion checks | Issuer = the configured IdP entity ID; the response's `Destination`, when present, and the bearer subject confirmation's `Recipient` = tripl's ACS URL; `InResponseTo` on both = the stored request ID; `NotBefore` / `NotOnOrAfter` of the conditions and the subject confirmation, with 120 seconds of clock skew; the audience restriction names tripl's entity ID; the status is `Success`. |
| Replay | Each accepted assertion ID is stored per organization until it expires; the same assertion a second time is refused (`saml_replay`), and the login state is single use as well. |
| Email and identity | The email comes from the configured attribute, or from the NameID only when its format is `emailAddress` (a persistent or unspecified NameID is never taken as an email: `email_missing`). It must be at one of the organization's verified domains. The identity is the pair (`saml:` + IdP entity ID, NameID); the prefix means an identity linked over OpenID Connect is never matched by SAML, even with an entity ID equal to the OIDC issuer, and the reverse. The entity ID is at most 507 characters. |

### Provisioning (SCIM 2.0)

An organization owner can let the organization's identity provider create,
update and deactivate its members and groups over SCIM (see
[Provisioning](#scim)). The controls that matter
for security:

| Property | Behaviour |
|---|---|
| Endpoint | `/scim/v2/{org}`, outside `/api/v1`. It accepts only a SCIM bearer token of that organization: browser sessions (and so CSRF-able requests) and API keys are refused, and a token of another organization gets `404`. A suspended or deleting organization answers `403`/`404` in SCIM's error format. |
| Tokens | Created and revoked by organization **owners** only, from a browser session (`/api/v1/orgs/{org}/scim/tokens`). A token starts with `tripl_scim_`, is shown once, and is stored only as a keyed HMAC digest, like session tokens; a prefix is kept for display. Revoked tokens are refused at once. A token works only while its creator is an owner of the organization: removing, deactivating or demoting that owner, or their transferring ownership, revokes their tokens (audited with `reason: "creator_no_longer_owner"`), and a token whose creator is no longer an owner is refused in any case. Creation and revocation are audited (`org.scim.token_create`, `org.scim.token_revoke`). |
| New accounts | Created only for an address at one of the organization's **verified** single sign-on domains; any other address is refused (`400 invalidValue`). The address is marked verified, the password is a scrypt hash of a random secret (unusable, so sign-in is through single sign-on), and the account is never a platform admin. |
| Existing accounts | Linked only when the address is at a verified domain of the organization, or the account is already a member; any other existing account gets the same `400 invalidValue` as an unknown address, so a token is no oracle for which addresses have accounts and cannot pull a stranger into an organization. A linked account's password and name are not changed on linking, **except** that an account at a verified domain whose address nobody ever confirmed (an unverified hosted sign-up) is taken over as by a verified SSO sign-in: its password is replaced with an unusable one, its sessions and pending reset/verification links are deleted, **all its API keys are revoked**, and its address is marked verified. Later `displayName`/`name.*` updates change the account's name only for an address at a verified domain. `userName` never changes (`400 mutability`). |
| Visibility | The IdP sees only accounts that are members of the organization or that it provisioned and later deactivated; another organization's users are never listed and answer `404` by id. |
| Deactivation | `active: false` or `DELETE` removes the organization membership with everything [removing a member](../administer/admin-guide.md#members) takes away (organization API keys revoked, project access, group memberships and single sign-on identities dropped). The account is kept. The last owner cannot be deactivated (`409`). A member an owner or admin removed by hand cannot be re-activated or re-created by the IdP (`409 mutability`) until they accept a new invitation; a `PUT` without `active` never re-activates. |
| Roles | SCIM never grants owner. The optional admin group mapping promotes the group's members to admin and demotes people removed from it to member; owners are never changed by it. |
| Managed groups | A group the IdP created, or any group the IdP has written to (it sees every group of the organization and matches by name, so a hand-made group with the same name is adopted on its first SCIM write), is managed by SCIM: renaming it, deleting it or changing its members through the groups API gets `409`; only its description stays editable. |
| Audit | Every SCIM write is recorded in the organization's audit log with no acting user and `via: "scim"` plus the token prefix. |
| Rate limits | Each token has its own bucket (600 requests a minute). Failed authentications (`401`) draw on a bucket per client address (30 a minute), then answer `429`. |
| Input limits | `startIndex` is capped at 10⁹; a body nested more than 32 levels deep, or too deeply to parse, is `400 invalidSyntax`; unknown endpoints, unsupported methods and oversized bodies answer in SCIM's error format. |

## API reference

Single sign-on (see [the admin guide](#single-sign-on))
is configured by the organization's **owners** only, from a browser session;
an admin, a member or any API key gets `403`:

| Method and path | Who | What |
|---|---|---|
| `GET /api/v1/orgs/{org}/sso` | owner, browser session | `configured`, `protocol` (`oidc` or `saml`), `enabled`, `sso_required`, `login_url` (where members start signing in); for OpenID Connect `issuer`, `client_id`, `client_secret_configured` (the secret itself is never returned), `scopes` and `redirect_uri` (to register at the provider); for SAML `saml_idp_entity_id`, `saml_idp_sso_url`, `saml_idp_certs` (PEM, public), `saml_name_id_format`, `saml_email_attribute`, the read-only `saml_sp_entity_id`, `saml_acs_url` and `saml_metadata_url` (to register at the IdP) and `saml_cert_info` (`[{"fingerprint_sha256", "not_after", "subject"}]`). Fields never set are empty strings (`saml_email_attribute` is `null`). A save that changes the protocol, the SAML entity ID, or replaces every SAML certificate unlinks the members' SSO identities of the old provider (they confirm the link again); the unchanged values a save echoes back are not re-validated. `saml_idp_entity_id` is at most 507 characters. |
| `PUT /api/v1/orgs/{org}/sso` | owner, browser session | The whole configuration: `{"protocol"?, "issuer", "client_id", "client_secret"?, "scopes"?, "saml_idp_entity_id"?, "saml_idp_sso_url"?, "saml_idp_certs"?, "saml_name_id_format"?, "saml_email_attribute"?, "enabled"?, "sso_required"?}`. `protocol` defaults to `oidc`. The fields of the chosen protocol are required and checked; the other protocol's may be `null`. An omitted `enabled` or `sso_required` is `false`; an omitted or `null` `client_secret` keeps the stored one, which is write-only. `scopes` defaults to `openid email profile` and must include `openid`. For SAML, `saml_idp_sso_url` must be `https`, `saml_idp_certs` holds one or more PEM certificates (several while the IdP rotates its key), `saml_name_id_format` defaults to `urn:oasis:names:tc:SAML:1.1:nameid-format:emailAddress`, and `saml_email_attribute` names the attribute carrying the email when the NameID is not one. `enabled: true` needs a saved provider and at least one verified domain. Turning `sso_required` on revokes the organization's API keys that were not created from a single sign-on session of it, owners' included; `revoked_api_keys` in the response says how many. While it is on, such keys are refused with `403` and new keys are created only from a single sign-on session of the organization (an owner's password session gets `403` too). Audited as `org.sso.*`, without the secret. |
| `POST /api/v1/orgs/{org}/sso/test` | owner, browser session | OpenID Connect: fetches the issuer's discovery document and checks the issuer and endpoints: `{"ok", "message", "error_code", "authorization_endpoint", "token_endpoint", "jwks_uri", ...}`. SAML: checks that the saved certificates parse and have not expired and that the SSO URL is `https`, without contacting the IdP. A failure's `message` is a fixed text per `error_code`. Rate-limited to 10 a minute, shared with domain verification. |
| `POST /api/v1/orgs/{org}/sso/saml/metadata-import` | owner, browser session | `{"xml"}`: pasted IdP metadata (an `EntityDescriptor`). Answers `{"saml_idp_entity_id", "saml_idp_sso_url", "saml_idp_certs"}` (the HTTP-Redirect single sign-on location and the signing certificates as PEM) without saving anything; send them with `PUT` to keep them. No URL is fetched. Metadata that does not parse, carries a `DOCTYPE`, or lacks an entity ID, a redirect location or a signing certificate is `422`. |
| `GET /api/v1/orgs/{org}/sso/domains` | owner, browser session | The claimed domains: `id`, `domain`, `verified`, `verified_at`, `txt_record_name`, `txt_record_value`, `created_at`. |
| `POST /api/v1/orgs/{org}/sso/domains` | owner, browser session | `{"domain"}`, lowercased. `201` with the domain and the TXT record to publish. A domain another organization has verified is `409`. |
| `DELETE /api/v1/orgs/{org}/sso/domains/{id}` | owner, browser session | Removes the claim. |
| `POST /api/v1/orgs/{org}/sso/domains/{id}/verify` | owner, browser session | Looks up the DNS TXT record `_tripl-verification.<domain>` and marks the domain verified when it contains `tripl-verification=<token>`. Rate-limited to 10 a minute, shared with the connection test. |

In an organization with `sso_required`, a request from a browser session that
did not sign in through the organization's provider answers
`403 {"detail": "This organization requires single sign-on", "sso_start": "/api/v1/auth/sso/<org>/start"}`
(organization owners and a platform admin's read-only step-in excepted). An API
key bound to it works only if it was created from a single sign-on session of
that organization; any other key is `403`.

SCIM provisioning (see [the admin guide](#scim)) is
set up by the organization's **owners** only, from a browser session; an admin,
a member or any API key gets `403`:

| Method and path | Who | What |
|---|---|---|
| `GET /api/v1/orgs/{org}/scim/tokens` | owner, browser session | The SCIM tokens, revoked ones included: `id`, `prefix`, `created_at`, `created_by_email`, `last_used_at`, `revoked_at`. The token itself is never returned here. |
| `POST /api/v1/orgs/{org}/scim/tokens` | owner, browser session | Creates a token: `{"id", "prefix", "token", "created_at"}`. `token` (starting `tripl_scim_`) is shown only in this response. Audited as `org.scim.token_create`. |
| `DELETE /api/v1/orgs/{org}/scim/tokens/{id}` | owner, browser session | Revokes the token; the identity provider's next request with it is refused. Audited as `org.scim.token_revoke`. |
| `GET /api/v1/orgs/{org}/scim/config` | owner, browser session | `{"base_url", "admin_group_id", "admin_group_name", "active_tokens"}`: the SCIM base URL to give the identity provider, the group whose members are made admins (`null` for none), and how many unrevoked tokens there are. |
| `PUT /api/v1/orgs/{org}/scim/config` | owner, browser session | `{"admin_group_id": "<group id>" \| null}`. The group must belong to the organization. |

The SCIM 2.0 protocol itself is served at `/scim/v2/{org}` (outside
`/api/v1`) for the identity provider: `ServiceProviderConfig`, `ResourceTypes`,
`Schemas`, `Users` and `Groups`. It takes only
`Authorization: Bearer tripl_scim_…` of that organization; sessions and API
keys are refused there, and a SCIM token works nowhere else. Agents and
scripts should use the `/api/v1` routes above instead.

Sign-in routes (unauthenticated):

| Method and path | Who | What |
|---|---|---|
| `GET /api/v1/auth/sso/discover?email=` | anyone | `{"orgs": [{"slug", "name", "login_url"}]}`: the organizations with single sign-on turned on whose verified domain the address is at. Rate-limited like `/auth/status`. |
| `GET /api/v1/auth/sso/{org}/start?next=` | anyone (a browser) | `302` to the organization's identity provider: for SAML, to its SSO URL with an unsigned `SAMLRequest` (HTTP-Redirect binding) and the state as `RelayState`. `next` is where to return afterwards and must be a relative path on this origin (`/…`, not `//…`). Start and callback share a rate limit of 20 a minute per address, apart from password sign-in. |
| `GET /api/v1/auth/sso/{org}/callback?code=&state=` | the identity provider's redirect | Finishes the sign-in and redirects: into the app with a session, to `/sso/link?ticket=…` when the address belongs to an existing account that has to confirm the link, or to `/auth?sso_error=<code>`: `sso_unavailable`, `invalid_state`, `idp_error`, `idp_denied`, `invalid_token`, `email_missing`, `email_not_verified`, `email_domain_not_allowed`, `membership_removed` (removed from the organization and not invited back), `rate_limited` or `sso_failed`. |
| `GET /api/v1/auth/sso/{org}/saml/metadata` | anyone | tripl's SAML service-provider metadata (XML): the entity ID (this URL), the ACS URL with the HTTP-POST binding, the email NameID format and `WantAssertionsSigned="true"`. Unsigned; there is no SP certificate. |
| `POST /api/v1/auth/sso/{org}/saml/acs` | the identity provider's form post (`SAMLResponse`, `RelayState`) | The SAML counterpart of the callback, with the same outcomes and the same `sso_error` codes, plus `saml_invalid` (the response failed a check: issuer, audience, recipient, destination, validity window), `idp_denied` also for a non-`Success` status, `email_missing` also when no email attribute is configured and the NameID format is not `emailAddress`, `saml_signature_invalid` (the assertion is unsigned, signed with SHA-1 or by a certificate not configured), `saml_replay` (the assertion was already used), `saml_unsolicited` (not an answer to a request tripl made: IdP-initiated sign-in is not supported) and `encrypted_assertion_unsupported`. |
| `GET /api/v1/auth/sso/link?ticket=` | anyone holding the ticket | `{"email", "org_slug", "org_name", "expires_at", "sign_in_required"}`: what confirming would link, without using the ticket; `sign_in_required` is true when this browser must first sign in to the account. `400` when the ticket is not live. |
| `POST /api/v1/auth/sso/link` | the ticket, from a browser session of the ticket's account | `{"ticket"}`: links the identity provider's account to the existing tripl account, adds the organization membership if missing, marks the address verified and replaces the session with a single sign-on session. Answers `{"next", "user"}`. Without a session of that account: `401` and the ticket stays usable. An account whose address was never verified needs no session and is taken over clean (its password, sessions and API keys are dropped). Single use, 10 minutes; `400` for an unknown, used or expired ticket; `403` for an account removed from the organization; `409` when the organization no longer uses single sign-on. |

## Rate limits

| Endpoint | Limiter | Default |
|---|---|---|
| `GET /api/v1/auth/sso/discover` | Shared status limiter | 30 / minute |
| `GET /api/v1/auth/sso/{org}/start` | Own SSO limiter (fixed, not configurable), shared by start, callback and ACS | 20 / minute |
| `GET /api/v1/auth/sso/{org}/callback` | Own SSO limiter (fixed, not configurable), shared by start, callback and ACS | 20 / minute |
| `POST /api/v1/auth/sso/{org}/saml/acs` | Own SSO limiter (fixed, not configurable), shared by start, callback and ACS | 20 / minute |
| `GET /api/v1/auth/google/start` | The same SSO limiter | 20 / minute |
| `GET /api/v1/auth/google/callback` | The same SSO limiter | 20 / minute |
| `GET /api/v1/auth/sso/link` | Shared status limiter | 30 / minute |
| `POST /api/v1/auth/sso/link` | `RATE_LIMIT_LOGIN_PER_MINUTE` | 5 / minute |
| `/scim/v2/{org}/*` | Own SCIM limiter, keyed per token (not per address) | 600 / minute |
| `/scim/v2/{org}/*`, failed authentication | Own limiter, keyed per client address; only `401` answers draw on it, then `429` | 30 / minute |

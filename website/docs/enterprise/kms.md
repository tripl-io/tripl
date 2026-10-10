---
title: Key management (KMS / BYOK)
sidebar_position: 5
---

# Key management (KMS / BYOK)

:::info Enterprise
This page describes a feature of the [Enterprise edition](../editions.md). Community encrypts stored secrets with `ENCRYPTION_KEY`; see [Security](../run/security.md#rotating-the-secrets).
:::

tripl stores third-party credentials: warehouse passwords and client keys,
alert-destination webhooks and tokens, tracker API tokens, AI and SMTP
secrets in settings, photo-storage service accounts, single sign-on client
secrets, the audit webhook's URL and signing secret and a license key saved in
Settings. Community encrypts them with one Fernet key, `ENCRYPTION_KEY`, held
in the server's environment.

An Enterprise instance can instead keep them under a key in your own key
management service, so the key that protects them never leaves it, access to
it is governed and audited there, and revoking it makes the stored secrets
unreadable:

| `KMS_PROVIDER` | Service | `KMS_KEY_ID` | Credentials |
|---|---|---|---|
| `aws` | AWS KMS (a symmetric key) | key id, key ARN or `alias/<name>` | the AWS SDK's chain: an instance or task role, `AWS_*` variables, a profile |
| `gcp` | Google Cloud KMS (a symmetric CryptoKey) | `projects/<p>/locations/<l>/keyRings/<r>/cryptoKeys/<k>` | Application Default Credentials: a workload identity, `GOOGLE_APPLICATION_CREDENTIALS` |
| `azure` | Azure Key Vault or Managed HSM | `https://<vault>.vault.azure.net/keys/<name>` (a version is optional) | `DefaultAzureCredential`: a managed identity, `AZURE_*` variables |
| `vault` | HashiCorp Vault or OpenBao, transit engine | the transit key's name | a token in `KMS_VAULT_TOKEN` or a file in `KMS_VAULT_TOKEN_FILE` |

Without `KMS_PROVIDER` an Enterprise instance encrypts exactly as Community.

## How it works: envelope encryption

Each server process generates a random 256-bit data key, asks the service to
wrap (encrypt) it under your key once, and encrypts secrets with the data key
locally. The wrapped data key is stored with every value; the plaintext data
key exists only in the process's memory. Reading a value unwraps its data key
through the service once per process, then reads locally, so the service sees
a handful of requests per process, not one per secret. A process wraps a new
data key every 24 hours (`KMS_DATA_KEY_TTL_SECONDS`).

A value is stored as

```
kms1:<provider>:<key-ref>:<wrapped-data-key>:<ciphertext>
```

- `kms1` — the format and its version;
- `<provider>` — `aws`, `gcp`, `azure` or `vault`;
- `<key-ref>` — base64url of the service's name for the key that wrapped the
  data key: the key ARN (also when `KMS_KEY_ID` is an alias), the CryptoKey,
  the Key Vault key's versioned id with the wrap algorithm, the transit key;
- `<wrapped-data-key>` — base64url of the service's wrapped data key;
- `<ciphertext>` — the secret, encrypted with the data key (Fernet: AES-128-CBC
  with HMAC-SHA256).

Where the service supports it, the wrap is bound to tripl (an AWS encryption
context and GCP additional authenticated data of `tripl-secret-data-key`), so
a data key wrapped for anything else is not accepted.

Values written before `KMS_PROVIDER` was set have no `kms1:` prefix: they are
still read with `ENCRYPTION_KEY`. **Keep `ENCRYPTION_KEY` set**: the server
requires it in production in any case, and it reads every value until the
rotation below has moved it.

## Configuration

Set these on the API and the worker (Compose passes them through):

| Variable | Default | Meaning |
|---|---|---|
| `KMS_PROVIDER` | unset | `aws`, `gcp`, `azure` or `vault` |
| `KMS_KEY_ID` | unset | the key, as in the table above |
| `KMS_TIMEOUT_SECONDS` | `10` | one request to the service |
| `KMS_DATA_KEY_TTL_SECONDS` | `86400` | how long a process encrypts with one data key |
| `KMS_AWS_REGION` | the SDK's | AWS region of the key |
| `KMS_AWS_ENDPOINT_URL` | unset | a VPC endpoint |
| `KMS_AZURE_ALGORITHM` | `RSA-OAEP-256` | `RSA-OAEP-256` or `RSA-OAEP` for a vault's RSA key, `A256KW` for a Managed HSM's AES key |
| `KMS_AZURE_OTHER_HOSTS` | unset | other vault hosts whose keys may unwrap, comma-separated: a rotation away from a key in another vault |
| `KMS_VAULT_ADDR` | unset | the Vault server, `https://vault.example.com:8200` (plain `http://` only to localhost) |
| `KMS_VAULT_TOKEN` | unset | a Vault token |
| `KMS_VAULT_TOKEN_FILE` | unset | a file holding the token, read again for every request (Vault Agent, a sidecar renewing it) |
| `KMS_VAULT_MOUNT` | `transit` | the transit engine's mount path |
| `KMS_VAULT_NAMESPACE` | unset | a Vault Enterprise namespace |
| `KMS_VAULT_CA_CERT` | system CAs | a CA bundle for the Vault server's certificate |

`compose.yaml` forwards every variable above, and nothing else. The provider
SDKs' own credentials (`AWS_*` or `AWS_PROFILE`, `GOOGLE_APPLICATION_CREDENTIALS`,
`AZURE_*`) are **not** forwarded, so out of the box only credentials the
containers get without a variable work: an instance or task role, instance
metadata, a workload or managed identity. `KMS_VAULT_TOKEN_FILE` and
`KMS_VAULT_CA_CERT` are paths **inside the container**, so the files need a
volume. Add both in a `compose.override.yaml` next to `compose.yaml`, which
`tripl install` and `tripl upgrade` leave alone
([deployment](../run/deployment.md#bring-it-up)):

```yaml
# compose.override.yaml
x-kms-environment: &kms-environment
  AWS_ACCESS_KEY_ID: ${AWS_ACCESS_KEY_ID:-}
  AWS_SECRET_ACCESS_KEY: ${AWS_SECRET_ACCESS_KEY:-}

x-kms-volumes: &kms-volumes
  - /etc/tripl/vault:/run/vault:ro   # KMS_VAULT_TOKEN_FILE=/run/vault/token

services:
  app:
    environment: *kms-environment
    volumes: *kms-volumes
  celery-worker:
    environment: *kms-environment
    volumes: *kms-volumes
  celery-beat:
    environment: *kms-environment
    volumes: *kms-volumes
  migrate:
    environment: *kms-environment
    volumes: *kms-volumes
```

Compose merges an override's `environment` and `volumes` into the service's
own, so these add to what `compose.yaml` sets. Keep only the lines your provider
needs.

The permissions the server's identity needs, and nothing more:

- **AWS**: `kms:Encrypt` and `kms:Decrypt` on the key.
- **GCP**: `roles/cloudkms.cryptoKeyEncrypterDecrypter` on the CryptoKey.
- **Azure**: wrap key and unwrap key on the key (the *Key Vault Crypto User*
  role).
- **Vault**: `update` on `<mount>/encrypt/<key>` and `<mount>/decrypt/<key>`.

The Enterprise image includes the AWS, Google Cloud and Azure SDKs. When the
package is installed into another environment, add the matching extra:
`tripl-enterprise[aws]`, `[gcp]` or `[azure]`. Vault needs none.

## Startup check

At startup the API and the worker wrap a fresh data key and unwrap it again,
past every cache. If `KMS_PROVIDER` is set and that fails (the settings are
incomplete, the SDK is missing, the service is unreachable, the credentials or
the key policy refuse it, the key is disabled), the process **refuses to
start** with a message naming the provider and the failure, for example:

```
Secret encryption startup check failed: KmsError: aws: encrypt failed (AccessDeniedException)
```

The message never contains a key, a token or a secret.

## Rotation

Rotation re-encrypts every stored secret under the key the instance wraps with
now. Run it after turning `KMS_PROVIDER` on (to move the values written under
`ENCRYPTION_KEY`), after changing `KMS_KEY_ID` to a new key, and after moving
to another provider:

```bash
# What would change; writes nothing.
docker compose exec app python -m tripl_enterprise.secrets rotate --dry-run

# Re-encrypt, 500 rows per transaction.
docker compose exec app python -m tripl_enterprise.secrets rotate --batch-size 500
```

It decrypts each value with whatever it was written under (`ENCRYPTION_KEY`,
an earlier key or provider) and encrypts it with the current key. A value
already under the current key is skipped, so the command is idempotent and
safe to re-run. Each batch commits on its own and its rows are locked while it
is rewritten, so it runs against a live instance, and an interrupted run keeps
what it did. It prints a count per kind of secret (rewritten, unchanged,
unreadable), never a value.

To retire an old key:

1. Deploy the new configuration (`KMS_KEY_ID`, or `KMS_PROVIDER`) to the API
   and the worker. New values are written under the new key.
2. Run `rotate` until a `--dry-run` reports `would rewrite 0`. A value saved
   by someone while the first run was going may still be under the old key; a
   second run moves it.
3. Disable the old key in the service (keep it a while before destroying it).

Moving between providers needs the old provider's connection settings to
stay in place during the rotation (`KMS_AWS_REGION`, the Vault address and
token); its key id comes from the stored values themselves.

When the service rotates the key's material in place (AWS automatic rotation,
a new Key Vault key version behind a versionless `KMS_KEY_ID`, `vault write
transit/keys/<key>/rotate`) the old versions keep decrypting and nothing needs
to be done. To stop depending on an old version, `rotate --force` re-encrypts
every value, including those already under the current key, with a data key
wrapped by the current version.

Exit status: `0` done; `1` some values could not be decrypted with any key the
instance accepts (they are left as they are, and the log names their rows: the
secret has to be entered again); `2` `KMS_PROVIDER` is not set or the service
is not usable.

## Failure modes

| Situation | What happens |
|---|---|
| The service is unreachable or refuses the credentials at startup | The API and the worker do not start; the log says why. |
| The service becomes unreachable while running | Values whose data key the process already unwrapped keep working, and so does writing new ones (a process keeps its data key past `KMS_DATA_KEY_TTL_SECONDS` until the service answers again). A value that needs a new unwrap fails with an error (a failed connection test, a failed alert delivery, an error on the page that reads it) rather than being treated as missing; it works again once the service is back. An audit webhook whose URL or secret needs a new unwrap holds back only that organization's deliveries, a minute at a time with no attempt counted, while every other organization's deliveries go on. |
| The key is disabled or deleted, or its grant revoked | Values under it cannot be read once processes restart: they behave as an unreadable secret (a connection error; settings fall back to the environment's value) until the key is restored or the secrets are entered again. |
| A stored value was altered or truncated, or names a provider the instance is not configured for | It is unreadable, as with a wrong key; nothing else is affected. |
| `ENCRYPTION_KEY` is changed or lost before the rotation finished | Values not yet rotated cannot be read. Run the rotation first, keep the key backed up. |
| A token names a key in another Azure vault | It is refused before any request: the instance's credential is only presented to the vault of `KMS_KEY_ID` and to `KMS_AZURE_OTHER_HOSTS`. |

Back up the key in your service as you would back up `ENCRYPTION_KEY`: without
it the stored secrets cannot be recovered and must be entered again.

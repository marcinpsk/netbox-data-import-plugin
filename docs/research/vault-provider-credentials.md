# Vault-backed inference provider credentials

Research date: 2026-08-17
NetBox baseline: v4.6.6

## Conclusion

Store only a **credential reference** in provider configuration. Resolve its current value in
the background worker immediately before an inference request. Do not put the value in a model,
form, serializer, profile export, session, job argument, proposal, result, or log.

Use a small credential-provider seam between inference configuration and the HTTP adapter. The
first implementation should support Vault KV v2. It should use Vault Proxy auto-auth when the
deployment can provide it. Vault Proxy manages machine authentication and token renewal without
giving the plugin an AppRole SecretID or a long-lived Vault token. HashiCorp recommends a
platform auth method when one is available. It recommends AppRole only when a trusted platform
auth method is not available. [Vault auth methods][vault-auth] [AppRole best practices][approle-best-practices]

Do not integrate with a NetBox secret-provider API. NetBox core removed its secret functionality
in version 3.0, and NetBox 4.6 has no plugin credential-provider interface. Its plugin API exposes
ordinary required and default settings through `PLUGINS_CONFIG`. [NetBox 3.0 release notes][netbox-secrets-removed]
[NetBox plugin configuration][netbox-plugin-config] [NetBox 4.6 PluginConfig source][netbox-plugin-source]

The older `netbox-vault-secrets` plugin is not a server-side credential service. Its browser
JavaScript connects directly to Vault, and its repository describes the project as work in
progress. It cannot supply a secret to a NetBox RQ worker. [netbox-vault-secrets][netbox-vault-secrets]
The maintained `netbox-secrets` plugin stores encrypted values in NetBox. It supports NetBox 4.6,
but it is not backed by Vault. [netbox-secrets][netbox-secrets]

## Current repository constraints

The current code has three persistence paths that must remain outside the secret-value flow:

- `ImportProfile` is a `NetBoxModel`, and the REST serializer exposes its configuration.
- `ExportProfileYamlView` exports profile fields and mappings.
- `ImportJobRunner.enqueue()` serializes job arguments. The runner also persists status and
  results in the native Job data and in `ImportJob` history.

These paths appear in `netbox_data_import/models.py`, `netbox_data_import/api/serializers.py`,
`netbox_data_import/views.py`, and `netbox_data_import/jobs.py`. The inference job must therefore
receive a provider configuration ID or stable key. It must resolve the credential itself. The
request-handling process must not resolve a secret and pass it to RQ.

`NetBoxDataImportConfig` does not currently declare plugin settings. NetBox supports
`default_settings`, `required_settings`, and `get_plugin_config()` for deployment configuration.
This is suitable for a non-secret file fallback and for Vault client connection settings. It is
not a secret store. [NetBox plugin configuration][netbox-plugin-config]

## Recommended configuration split

### Provider configuration

The database is authoritative when a provider row exists. A `PLUGINS_CONFIG` entry with the same
stable provider key is a whole-provider fallback only when no database row exists. Do not merge
the two sources field by field. This rule prevents a partial database row from silently inheriting
an unexpected endpoint or credential reference from a deployment file.

Both sources can contain ordinary provider data:

- stable provider key and display name
- inference adapter type
- API base URL
- model name
- request timeout and other transport policy
- enabled state
- credential reference

The reference is configuration metadata, not the API key. A Vault KV v2 reference needs this
logical shape:

```yaml
backend: vault_kv_v2
mount: <kv-mount-name>
path: <provider-secret-path>
field: <api-key-field>
```

Do not accept a Vault server URL, namespace, token, AppRole RoleID, AppRole SecretID, or TLS
verification override in this per-provider reference. Keep connection and machine identity data
in deployment configuration. This stops a database editor from turning credential resolution
into an arbitrary network request.

KV v2 retains versions and returns the latest version when no version is specified. An unpinned
reference therefore follows API-key rotation without a database update. The Vault API uses a
mount, secret path, and optional version as separate concepts. [Vault KV v2][vault-kv-v2]
[Vault KV v2 read API][vault-kv-v2-api]

### Vault client configuration

Keep the following deployment-owned values outside the database:

- Vault Proxy address or Vault server address
- optional Vault Enterprise namespace
- trusted CA bundle path and client certificate paths, if used
- connection and read timeouts
- authentication mode

Use end-to-end TLS and certificate verification for a remote Vault connection. HashiCorp says to
always use TLS in production. A loopback or private Unix-socket-style trust boundary to a local
Vault Proxy can be deployment-specific, but the remote Proxy-to-Vault connection still needs TLS.
[Vault production hardening][vault-hardening]

Do not add a `skip_tls_verify` provider option. A custom CA bundle is the safe solution for a
private CA.

## Credential-provider seam

The inference subsystem should depend on one interface with these responsibilities:

1. Validate a typed credential reference.
2. Resolve it through the selected backend.
3. Return secret material for the lifetime of one outbound request.
4. Classify failures without including response bodies or credential values.

A narrow conceptual interface is sufficient:

```text
CredentialProvider.resolve(reference) -> credential context
```

The inference transport opens the context, constructs the authorization header, sends one
request, and then releases all references to the returned value. Python cannot guarantee memory
zeroing for strings. The implementation must therefore minimize lifetime and copies instead of
claiming secure erasure.

The Vault implementation should:

- support KV v2 only at first
- read one named field from one configured path
- reject missing, empty, or non-string values
- omit the secret from all exception text
- use an allowlisted Vault address from deployment configuration
- send no source trace, device, contact, or operator data to Vault

Use the maintained `hvac` library if the implementation needs a Python Vault client. Its current
API exposes an explicit KV v2 `read_secret_version()` operation. Do not reproduce Vault auth,
namespace, and response-shape logic with ad hoc HTTP calls. [hvac][hvac] [hvac KV v2][hvac-kv-v2]

The seam must not expose Vault concepts to the OpenAI-compatible inference adapter. This permits
a later credential backend without changing proposal or inference logic.

## Authentication and token lifecycle

### Recommended baseline: Vault Proxy

Run Vault Proxy near every process group that executes inference jobs. Configure auto-auth for
the deployment platform and force the Proxy to use its auto-auth token. The worker then calls the
Vault API through the Proxy without possessing the token. Vault Proxy is the current HashiCorp
component for API proxy workflows. The older Vault Agent API proxy is deprecated. [Vault Proxy][vault-proxy]
[Vault Agent API proxy deprecation][vault-agent-proxy]

Enable the Proxy listener's `require_request_header` option and send `X-Vault-Request: true`.
HashiCorp documents this as an additional protection against server-side request forgery.
[Vault Proxy][vault-proxy]

Let Vault Proxy renew the machine token. Do not authenticate to Vault for every inference
request. HashiCorp states that repeated authentication is expensive and recommends reusing and
renewing a token. [AppRole best practices][approle-best-practices]

### Direct Vault access

Direct access is a valid deployment variant, but it makes the plugin responsible for auth login,
token renewal, and reauthentication. Prefer a platform machine identity. If the deployment has no
platform method, AppRole is the fallback. Its SecretID must arrive through a deployment secret
delivery mechanism and must never enter plugin configuration or a database row.

Do not make direct AppRole support part of the first implementation unless an operator cannot
deploy Vault Proxy. It adds a second secret that must be protected before the plugin can retrieve
the inference API key.

## Rotation and caching

- Resolve the latest KV v2 value in the worker at inference-request time.
- Do not cache API-key values in plugin code for the first implementation.
- Resolve once per outbound inference attempt, not once per row preview.
- Do not persist the resolved version or value in a Resolution Proposal.
- Let Vault Proxy manage auth token and lease caching.

Vault Proxy static-secret caching for KV v1 and KV v2 requires Vault Enterprise. Community Vault
deployments must tolerate a KV read for each outbound inference attempt or add a later, explicitly
bounded in-process cache. [Vault Proxy static secret caching][vault-proxy-cache]

Key rotation takes effect on the next resolution. An already running HTTP request continues with
the value it received. An inference `401` can invalidate a future cache, but the system must not
silently replay a possibly accepted inference request. The inference job owns retry and
idempotency policy.

## Permissions and audit

Give the NetBox worker identity only `read` capability on the exact KV v2 data path or a narrow
provider path prefix. Do not grant `list`, `create`, `update`, `delete`, metadata administration,
or access to unrelated secrets. Vault policies deny access by default. [Vault policies][vault-policies]

The Ask AI action must enforce its own NetBox permission before it queues work. The Vault token
represents the NetBox service, not the operator. A user must not be able to use the credential
resolver as a general secret-reading endpoint.

Enable Vault audit devices. Vault records API requests and responses, and hashes most string
values with HMAC-SHA256 by default. Keep hashing enabled for sensitive fields. Do not log Vault
response dictionaries in the plugin, even at debug level. [Vault audit devices][vault-audit]

Plugin audit and job state can record:

- provider configuration ID
- proposal job ID
- credential backend name
- resolution outcome category
- request start and completion times

They must not record the secret value, authorization header, Vault token, Vault response body,
or inference request headers. Treat the Vault path and field as restricted configuration metadata.

## Failure behavior

Credential failures must affect only the Resolution Proposal request. Device, rack, Cable, and
Source Trace preview and import workflows must remain available.

| Failure | Classification | Required behavior |
| --- | --- | --- |
| Vault is unreachable, sealed, or times out | Retryable infrastructure failure | Keep the target unresolved. Record a redacted failure. Retry only under the inference job policy. |
| Vault returns `401` or `403` | Deployment or policy failure | Fail closed. Do not try another credential source. Ask an operator to repair auth or policy. |
| Secret path or field is absent | Invalid credential reference | Fail closed. Identify the provider configuration, not the secret path, in user-visible output. |
| Secret value is empty or has the wrong type | Invalid secret material | Fail closed. Never send an inference request. |
| Inference endpoint rejects the key | Provider authentication failure | Keep the proposal unresolved. Do not expose the endpoint response body without redaction. |
| Vault rotates during an inference request | No mid-request action | The next attempt resolves the latest value. Do not replay the current request automatically. |

Validate configuration shape at application startup, but do not require Vault to be available at
startup. A mandatory startup read would make all NetBox functionality depend on Vault. Provide an
explicit administrator-only connection test. The test resolves the configured reference and
reports only success or a redacted failure category.

## Threat considerations

- **Secret persistence:** The highest local risk is accidental serialization through RQ, native
  Job data, `ImportJob`, sessions, profile YAML, API serializers, or proposal state. Resolve only
  in the worker and pass only a provider ID through those boundaries.
- **Log disclosure:** HTTP client exceptions and debug logging can include headers or response
  bodies. Use a redacting transport boundary and controlled error types.
- **SSRF and reference abuse:** Keep the Vault address deployment-owned. Validate mount, path,
  and field syntax. Do not offer a general Vault browser or arbitrary URL fetch.
- **Excess privilege:** Use one read-only policy for the smallest useful provider path. Do not use
  a root or administrator token.
- **Worker compromise:** A compromised inference worker can use its live service identity to read
  allowed secrets. Separate the inference queue and process identity later if the deployment
  needs a smaller blast radius.
- **Stale fallback:** Never use a previously resolved value, a raw file value, or an environment
  API key when Vault fails unless the operator explicitly selects a different credential backend.
- **TLS interception:** Trust an explicit CA bundle. Do not disable certificate verification.

## Unresolved operator decisions

1. Can each NetBox worker deployment run a local Vault Proxy, or must the plugin connect directly?
2. Which platform auth method is available to Vault Proxy? If none is available, how will an
   AppRole SecretID be delivered and rotated?
3. Which Vault namespace, KV v2 mount, path convention, and field name will operators use?
4. Will the web process also reach Vault for an administrator connection test, or will that test
   run as a background job on the inference worker?
5. Does the deployment use Vault Enterprise static-secret caching? This affects request volume,
   not the credential-provider interface.
6. Which NetBox permission controls provider configuration and the administrator connection test?
7. Should provider credential references be visible to all users who can view provider
   configuration, or only to users who can change it?
8. What are the required Vault and inference timeouts, retry limits, and proposal failure retention
   periods?

[approle-best-practices]: https://developer.hashicorp.com/vault/docs/auth/approle/approle-pattern
[hvac]: https://github.com/hvac/hvac
[hvac-kv-v2]: https://python-hvac.org/en/stable/usage/secrets_engines/kv.html
[netbox-plugin-config]: https://netbox.readthedocs.io/en/stable/plugins/development/#pluginconfig
[netbox-plugin-source]: https://github.com/netbox-community/netbox/blob/v4.6.6/netbox/netbox/plugins/__init__.py#L39-L79
[netbox-secrets]: https://github.com/Onemind-Services-LLC/netbox-secrets
[netbox-secrets-removed]: https://netbox.readthedocs.io/en/stable/release-notes/version-3.0/#breaking-changes
[netbox-vault-secrets]: https://github.com/ffddorf/netbox-vault-secrets
[vault-agent-proxy]: https://developer.hashicorp.com/vault/docs/agent-and-proxy/agent/apiproxy
[vault-audit]: https://developer.hashicorp.com/vault/docs/audit
[vault-auth]: https://developer.hashicorp.com/vault/docs/auth
[vault-hardening]: https://developer.hashicorp.com/vault/docs/concepts/production-hardening
[vault-kv-v2]: https://developer.hashicorp.com/vault/docs/secrets/kv/kv-v2
[vault-kv-v2-api]: https://developer.hashicorp.com/vault/api-docs/secret/kv/kv-v2#read-secret-version
[vault-policies]: https://developer.hashicorp.com/vault/docs/concepts/policies
[vault-proxy]: https://developer.hashicorp.com/vault/docs/agent-and-proxy/proxy
[vault-proxy-cache]: https://developer.hashicorp.com/vault/docs/agent-and-proxy/proxy/caching/static-secret-caching

# Configuration

## Inference backend credentials

Configure the Vault connection before you add an Inference Backend. The recommended setup uses a
Vault Proxy. The Proxy authenticates to Vault and renews its token. The NetBox web and worker
processes call the Proxy without holding a Vault credential.

```python
PLUGINS_CONFIG = {
    "netbox_data_import": {
        "vault": {
            "address": "https://vault-proxy.example.invalid:8100",
            "auth_method": "proxy",
            "ca_bundle": "/etc/ssl/certs/vault-proxy-ca.pem",
            "connect_timeout": 5,
            "read_timeout": 10,
        },
    },
}
```

Configure `vault.address` with HTTPS for both `proxy` and `token` authentication. The connection
carries either a Vault token or a resolved inference API key. TLS can terminate at the Proxy, but
the NetBox-to-Proxy connection must remain encrypted through the Proxy's HTTPS listener.

The example uses a private CA. Mount that CA bundle in the NetBox web and worker processes. Set
`ca_bundle` to its path. This adds the private CA to certificate verification. It does not disable
verification. The Inference Backend `api_root` is separate: it can use HTTP only for an exact local
endpoint in `inference_backend_origin_allowlist`.

If Vault Proxy uses AppRole, give the RoleID and SecretID to the Proxy deployment. For local
development, its gitignored `.env` file can supply them. For production, use the deployment's
secret store. Do not give these values to the NetBox process, and do not put them in
`PLUGINS_CONFIG`.

The Proxy needs an AppRole auto-auth method, a listener, and API proxying. This is the essential
Vault Proxy configuration:

```hcl
vault {
  address = "https://vault.example.invalid:8200"
}

auto_auth {
  method {
    type = "approle"

    config = {
      role_id_file_path = "/run/secrets/vault-role-id"
      secret_id_file_path = "/run/secrets/vault-secret-id"
      remove_secret_id_file_after_reading = true
    }
  }
}

listener "tcp" {
  address = "0.0.0.0:8100"
  tls_cert_file = "/run/secrets/vault-proxy-cert.pem"
  tls_key_file = "/run/secrets/vault-proxy-key.pem"
  require_request_header = true
}

api_proxy {
  use_auto_auth_token = "force"
}
```

A SecretID stays usable until it expires, and the RoleID sits beside it, so a reader of the Proxy
filesystem can mint new Vault tokens from the pair. `remove_secret_id_file_after_reading` deletes
the file after auto-auth reads it, but it only works on a path the Proxy can write. The Compose
secrets below mount `/run/secrets` read-only: Vault logs the failed removal and keeps
authenticating, so the SecretID stays readable for the life of the container.

Pick one of two delivery models. Either accept the retained file and limit it: short SecretID TTL,
regular rotation, and no other reader of the Proxy container. Or make the removal real: write the
SecretID to a path the Proxy owns, such as a tmpfs file, or set
`secret_id_response_wrapping_path` so the file holds a single-use wrapping token instead of the
SecretID itself.

The TCP example lets a sibling container reach the Proxy. Keep both containers on an isolated
container network. Do not publish the listener outside the isolated container network. The
`X-Vault-Request` header is SSRF protection, not client authentication. If NetBox connects to a
separate Proxy, restrict network ingress and authenticate each client, for example with mTLS.

Mount the RoleID, SecretID, Proxy certificate, and Proxy key at the configured file paths. Mount the
CA that signed the Proxy certificate in the NetBox web and worker processes. A local Docker Compose
deployment can source the AppRole values from its gitignored `.env` file and expose them only to the
Proxy as Compose secrets:

```dotenv
NBDI_VAULT_PROXY_ADDRESS=https://vault-proxy.example.invalid:8100
NBDI_VAULT_ADDRESS=https://vault.example.invalid:8200
NBDI_VAULT_NAMESPACE=
NBDI_VAULT_ROLE_ID=replace-with-vault-role-id
NBDI_VAULT_SECRET_ID=replace-with-vault-secret-id
```

Give the AppRole only `read` access to the required KV v2 data paths. Set `token_num_uses` to `0`,
because Vault auto-auth does not support limited-use tokens. See HashiCorp's
[AppRole auto-auth documentation](https://developer.hashicorp.com/vault/docs/agent-and-proxy/autoauth/methods/approle)
and [Vault Proxy API documentation](https://developer.hashicorp.com/vault/docs/agent-and-proxy/proxy/apiproxy).
The plugin sends `X-Vault-Request: true`, so the Proxy listener can require this header as shown.

The plugin does not perform an AppRole login directly. It supports `auth_method: "proxy"` as shown
above. It also supports `auth_method: "token"`, which reads a token from each NetBox process that
resolves credentials. Set `VAULT_TOKEN` in both the web and worker process environments. Direct
token authentication uses the same HTTPS and CA-bundle requirements.

The **Credential reference** field on an Inference Backend tells the plugin which value to read
from Vault KV v2. It has this JSON shape:

```json
{
  "backend": "vault_kv_v2",
  "mount": "secret",
  "path": "inference/backend",
  "field": "api_key"
}
```

`mount` is the KV v2 mount. `path` is the secret path in that mount. `field` is the key that holds
the inference API key. This reference contains no Vault address, Vault credential, or inference API
key.

Save the AI backend, then open its detail page and select **Run connection test**. The foreground
test resolves the credential and sends a small request to the configured
`{api_root}/chat/completions` endpoint. This request can consume provider tokens and can have a
provider cost.

The test also tries `GET {api_root}/models`. If the endpoint returns a compatible model list, the
detail page shows up to 100 model ids. Select one to open the normal edit form with that value. Review
the value and save the form. Model discovery is optional. If the endpoint does not support it, enter
the exact model id manually.

The foreground test has one overall time limit. The limit is the backend connect timeout plus its read
timeout. Credential resolution, model discovery, and the completion request share this time.

## Native primary contacts

Map the source contact column to the `primary_contact` target field. Then configure these fields on the Import Profile:

- **Primary Contact Role** selects the NetBox Contact Role for the assignment. A role is required when a row contains contact data.
- **Primary Contact Lookup Field** selects `Email address` or `Name`. Matching is case-insensitive.

Email lookup validates each source value as an email address. A new Contact uses the source value for both its name and email. Name lookup creates a Contact with only its name. Use email lookup when Contact names can change.

During a non-preview sync, the plugin creates or reuses the Contact and assigns it to the imported Device with primary priority. If the configured role already has one primary assignment, the plugin updates that assignment when the source contact changes. It does not delete the old Contact.

The sync also migrates a `primary_contact` value held in the device import record. It removes only `primary_contact` and preserves the other extra source columns. A failed contact sync rolls back the Device update and keeps the legacy value.

The importing user needs the applicable native NetBox permissions:

- `tenancy.view_contact` to reuse an existing Contact
- `tenancy.add_contact` to create a Contact
- `tenancy.add_contactassignment` to create an assignment
- `tenancy.change_contactassignment` to change an assignment

## Import progress

When you submit the import setup form, the page shows a loading indicator while it reads the workbook and generates the preview. You can leave an unsubmitted preview and return to it later. Open **Run Import** and select **Resume preview**.

After you confirm a preview, the plugin queues a native NetBox background Job and opens its progress page. The page uses NetBox's HTMX support to show the number of processed source rows and update the progress bar automatically.

You can leave the progress page while the Job runs. Open **Run Import** and select **Resume import** to return to the latest active Job. The direct progress URL also restores a completed result or a refreshed preview after a safe validation failure.

## Device import record

The plugin keeps one import record per imported Device: the source ID, the profile that wrote it,
the source columns no mapping consumes, and any IP the import could not assign to a native NetBox
field. The Device page shows this record in the **Import Data** card.

Releases before 1.6 kept the same data in a plugin-managed `data_import_source` custom field. The
upgrade migration copies every payload into the new table, clears the key from Devices and Racks,
and deletes the custom field. The migration does not reverse. A payload that names a deleted import
profile is dropped, and the migration logs how many.

The per-profile custom field (**Custom field name** in the Import Profile's adapter
configuration, for example `external_id`) is separate. The plugin still writes the source ID to
it and never deletes it. See [Linking to the source system](#linking-to-the-source-system) to
turn that ID into a button on the object page.

## Linking to the source system

NetBox's native **Custom Links** turn the source ID into a button on the object page. The plugin
needs no setting for this.

### Link from the per-profile custom field

Set **Custom field name** in the Import Profile's adapter configuration, for example
`external_id`. The plugin writes the source ID of each imported row into that custom field, on
Devices and on Racks. Assign the custom field to both object types.

Add the link under **Customization > Custom Links**:

| Field | Value |
| --- | --- |
| Object types | `DCIM > device`, `DCIM > rack` |
| Link text | `{% if object.cf.external_id %}Locate asset in the source system{% endif %}` |
| Link URL | `https://assets.example.invalid/search?q={{ object.cf.external_id }}` |

To find the URL, search for one ID in the source system and replace the search value with
`{{ object.cf.external_id }}`.

An empty link text hides the button, so an object that no import touched shows nothing.

NetBox renders both templates in a sandbox. It refuses a URL scheme that `ALLOWED_URL_SCHEMES` does
not list.

### Create the link for every object type the field covers

The script below reads which object types the custom field covers, then creates one Custom Link for
all of them. It skips an object type that supports custom fields but not custom links. Four NetBox
types are in that group: `circuits.circuitgroupassignment`, `extras.eventrule`, `extras.webhook`,
and `tenancy.contactassignment`.

The API token needs `extras.view_customfield`, `core.view_objecttype`, and `extras.add_customlink`.

```python
import requests

NETBOX = "https://netbox.example.invalid"
TOKEN = "0123456789abcdef0123456789abcdef01234567"
CUSTOM_FIELD = "external_id"
LINK_NAME = "Locate asset in the source system"
SEARCH_URL = "https://assets.example.invalid/search?q="

session = requests.Session()
session.headers.update({"Authorization": f"Token {TOKEN}", "Accept": "application/json"})


def get(path, **params):
    """Return the result list of one API endpoint."""
    response = session.get(f"{NETBOX}/api/{path}", params=params, timeout=30)
    response.raise_for_status()
    return response.json()["results"]


fields = get("extras/custom-fields/", name=CUSTOM_FIELD)
if not fields:
    raise SystemExit(f"NetBox has no custom field named {CUSTOM_FIELD}")
covered = set(fields[0]["object_types"])

linkable = {
    f"{entry['app_label']}.{entry['model']}"
    for entry in get("core/object-types/", limit=0)
    if "custom_links" in entry["features"]
}
targets = sorted(covered & linkable)
for skipped in sorted(covered - linkable):
    print(f"Skipping {skipped}: it supports custom fields but not custom links")

response = session.post(
    f"{NETBOX}/api/extras/custom-links/",
    json={
        "name": LINK_NAME,
        "object_types": targets,
        "enabled": True,
        "link_text": "{% if object.cf." + CUSTOM_FIELD + " %}" + LINK_NAME + "{% endif %}",
        "link_url": SEARCH_URL + "{{ object.cf." + CUSTOM_FIELD + " }}",
        "new_window": True,
    },
    timeout=30,
)
response.raise_for_status()
print(f"Created custom link {response.json()['id']} for {', '.join(targets)}")
```

The script creates the link. To change an existing link, send `PATCH` to
`/api/extras/custom-links/{id}/` with the same body.

### Link without a custom field

A profile that names no custom field still records the source ID in the Device import record. A
Custom Link can read that record. The plugin keeps no equivalent record for a Rack, so this variant
covers Devices only.

| Field | Value |
| --- | --- |
| Object types | `DCIM > device` |
| Link text | `{% if object.data_import_source and object.data_import_source.source_id %}Locate asset in the source system{% endif %}` |
| Link URL | `https://assets.example.invalid/search?q={{ object.data_import_source.source_id }}` |

Keep both tests in the link text. A Device that the plugin never imported has no
`data_import_source`. A link text that reads `object.data_import_source.source_id` without the first
test raises, and NetBox then shows a disabled warning button on every such Device.

## Source Adapter

An Import Profile selects a **Source Adapter**: the source format it reads. The adapter is required
and cannot change after the profile is created, because every mapping and policy row on the profile
belongs to that format. A different source format needs a new Import Profile.

The selected adapter declares the profile's remaining settings, which the profile stores together as
its adapter configuration. The flat workbook adapter declares the sheet name, the source ID column,
the custom field name, the update and create switches, the extra-data switch, the primary contact
role and lookup field, and the preview view mode. The trace workbook adapter currently declares no
settings. It reads the fixed `Trace From To` and optional `Trace List` workbook sheets.

The flat workbook and trace workbook adapters are selectable. An adapter becomes selectable when a
Target Module that consumes its output ships, so the plugin never offers a source format it cannot
import.

A trace import opens the **Trace Review Workspace** directly. If a source Device label does not
match exactly one visible NetBox Device in the selected Site, select **Choose Device**. The Import
Profile stores that choice. A later trace file reuses it when the source Device label has the same
letters after case and whitespace normalization. The choice applies to all ports on that source
Device.

Rack, Location, and U position are search hints. They can change the candidate order, but they never
select a Device. A Rack or Location hint is used only when you can view that NetBox object.

The trace workbook layout is not configurable in this release. A later adapter setting can map other
sheet names and columns to the same Source Trace values. Device choices do not depend on Excel column
names, so they remain reusable across that change.

An object reference inside the adapter configuration uses a natural key, never a database id. The
primary contact role is referenced by its name, so a profile exported as YAML imports into a
different NetBox instance.

Releases before 1.6 kept these settings as separate Import Profile columns. The upgrade migration
stamps the flat workbook adapter on every existing profile, copies each column into the adapter
configuration, and drops the columns.

The exported profile YAML carries the adapter and its configuration:

```yaml
profile:
  name: My Profile
  source_adapter: flat_workbook
  adapter_config:
    sheet_name: Data
    source_id_column: Id
    primary_contact_role: Primary Contact
```

When imported, `adapter_config` replaces the profile's stored adapter configuration. It is not
merged. A setting that is absent from the mapping resets to the adapter's declared default. Include
each setting that you want to preserve in a hand-written or partial YAML file.

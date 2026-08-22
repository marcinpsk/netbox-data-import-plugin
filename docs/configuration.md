# Configuration

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

The per-profile custom field (**Custom field name** in the Import Profile's adapter configuration,
for example `cans_id`) is separate. The plugin still writes the source ID to it and never deletes it.

## Source Adapter

An Import Profile selects a **Source Adapter**: the source format it reads. The adapter is required
and cannot change after the profile is created, because every mapping and policy row on the profile
belongs to that format. A different source format needs a new Import Profile.

The selected adapter declares the profile's remaining settings, which the profile stores together as
its adapter configuration. The flat workbook adapter declares the sheet name, the source ID column,
the custom field name, the update and create switches, the extra-data switch, the primary contact
role and lookup field, and the preview view mode. The trace workbook adapter declares no settings.

Only the flat workbook adapter is selectable today. An adapter becomes selectable when a Target
Module that consumes its output ships, so the plugin never offers a source format it cannot import.

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

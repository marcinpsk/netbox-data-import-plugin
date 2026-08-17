# NetBox Cable Profile research

Research date: 2026-08-17
NetBox baseline: v4.6.6

## Conclusion

**Cable Profile** is the exact NetBox term. It is an optional field on each Cable. It defines
the connector and position mapping used to trace a single physical cable. It supports built-in
single, trunk, breakout, and shuffle shapes. It does not define reusable Cable defaults such as
type, status, color, length, or tenant. [The NetBox Cable documentation][cable-docs] describes
the field and its tracing role. The v4.6.6 source stores it as a `CableProfileChoices` value and
maps every accepted value to a concrete profile class. [Cable model][cable-model]

A Cable Profile is not a database model. An operator cannot create one through the NetBox UI,
REST API, or supported `FIELD_CHOICES` configuration. The choices and their tracing algorithms
are part of NetBox code. [Profile choices][profile-choices] [Profile implementations][profile-code]
[Configurable field choices][field-choices]

The architecture should keep the NetBox name **Cable Profile**. If the importer needs reusable
defaults for new Cables, those defaults are a separate plugin concept. They must not be called a
Cable Profile.

## Related NetBox concepts

| Concept | Form in NetBox 4.6 | Can an operator create it? | Purpose |
| --- | --- | --- | --- |
| Cable | Database object | Yes | A direct physical connection between two sets of terminations. |
| Cable Profile | Fixed choice on Cable | No | Connector and lane mapping within one Cable. |
| Cable Type | Fixed choice on Cable | No new choice | Physical medium or classification, such as copper or fiber. |
| Cable Status | Fixed choice on Cable | No new choice | The stored values are `connected`, `planned`, and `decommissioning`. |
| CableTermination | Internal database association | Not independently | Links a Cable end to a termination. NetBox derives its connector and positions from the Cable Profile. |
| CablePath | Private derived database state | No | Caches an ordered path through Cables and pass-through ports. It is not an operator-managed Trace. |
| CableBundle | Database object | Yes | Groups Cables for organization. It supplies no Cable defaults and does not model strands inside one cable. |
| DeviceType and component templates | Database objects | Yes | Instantiate Interfaces, Front Ports, Rear Ports, and mappings on a new Device. They do not template Cables. |
| ModuleTypeProfile | Database object | Yes | Defines optional JSON-schema attributes for Module Types. It is unrelated to Cables. |

The v4.6.6 Cable model defines Cable type, status, profile, label, color, length, tenant, and
bundle as separate fields. [Cable fields][cable-fields] Cable type, profile, status, and length
unit are fixed source choices. The documented configurable-choice list does not include any
Cable field. [DCIM choices][dcim-choices] [Configurable field choices][field-choices]

`CableTermination.connector` and `CableTermination.positions` are implementation data. When a
Cable is saved, NetBox numbers each termination and derives its positions from the selected
profile. [Termination generation][termination-generation] The REST representation of a
CableTermination is read-only. [Cable serializers][cable-serializers]

`CablePath` stores the traced physical path and its active, complete, and split flags. NetBox
marks the model private. The importer should change Cables and their terminations, then let
NetBox derive CablePath state. [CablePath model][cable-path]

DeviceType and ModuleType component templates are reusable hardware definitions. They can create
ports when a new Device or Module is instantiated. They do not create Cable records or apply
Cable defaults. [DeviceType behavior][device-type] [Component templates][component-templates]
`ModuleTypeProfile` is a different, operator-created profile for ModuleType classification and
JSON-schema attributes. [Module Type Profile documentation][module-type-profile]

## Implication for `CableClass`

Do not assume that a source `CableClass` is a NetBox Cable Profile.

- If `CableClass` describes physical medium or classification, map it to `Cable.type`.
- If it describes connector and lane topology, map it to `Cable.profile`.
- If it combines both meanings, derive and review both target fields separately.
- Use an explicit source-to-target mapping because both target fields have fixed choices.
- If no built-in Cable Profile represents the source topology, leave the row unresolved. The
  importer cannot create a new NetBox Cable Profile at runtime.

For an ordinary one-to-one cable segment, NetBox permits either no profile or the built-in
single-connector, single-position profile. The specification should choose one policy. A Cable
Profile matters for a multi-position, trunk, breakout, or shuffle Cable. It does not represent
the complete end-to-end Source Trace formed from several Cable segments and pass-through ports.

The current stable Cable documentation calls the default status “Active,” while the v4.6.6 tagged
source uses the stored value `connected` and label “Connected.” The documentation site publishes no
v4.6.6 page, so `[cable-docs]` points at the current stable release and can describe a later
version than the rest of this document. Import logic should read target choices from the running
NetBox version rather than hard-code documentation labels.
[Cable documentation (current stable)][cable-docs] [Status choices (v4.6.6)][status-choices]

[cable-docs]: https://netbox.readthedocs.io/en/stable/models/dcim/cable/
[cable-model]: https://github.com/netbox-community/netbox/blob/v4.6.6/netbox/dcim/models/cables.py#L182-L212
[cable-fields]: https://github.com/netbox-community/netbox/blob/v4.6.6/netbox/dcim/models/cables.py#L76-L150
[profile-choices]: https://github.com/netbox-community/netbox/blob/v4.6.6/netbox/dcim/choices.py#L1689-L1761
[profile-code]: https://github.com/netbox-community/netbox/blob/v4.6.6/netbox/dcim/cable_profiles.py#L13-L61
[field-choices]: https://netbox.readthedocs.io/en/stable/configuration/data-validation/#field_choices
[dcim-choices]: https://github.com/netbox-community/netbox/blob/v4.6.6/netbox/dcim/choices.py#L1687-L1915
[termination-generation]: https://github.com/netbox-community/netbox/blob/v4.6.6/netbox/dcim/models/cables.py#L461-L515
[cable-serializers]: https://github.com/netbox-community/netbox/blob/v4.6.6/netbox/dcim/api/serializers_/cables.py#L40-L87
[cable-path]: https://github.com/netbox-community/netbox/blob/v4.6.6/netbox/dcim/models/cables.py#L709-L762
[device-type]: https://github.com/netbox-community/netbox/blob/v4.6.6/netbox/dcim/models/devices.py#L64-L77
[component-templates]: https://github.com/netbox-community/netbox/blob/v4.6.6/netbox/dcim/models/device_component_templates.py#L49-L170
[module-type-profile]: https://netbox.readthedocs.io/en/stable/models/dcim/moduletypeprofile/
[status-choices]: https://github.com/netbox-community/netbox/blob/v4.6.6/netbox/dcim/choices.py#L1884-L1894

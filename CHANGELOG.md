# CHANGELOG

<!-- version list -->

## v2.0.0 (2026-09-05)

### Bug Fixes

- Answer a preview write whose profile is deleted before its lock
  ([`7a48781`](https://github.com/marcinpsk/netbox-data-import-plugin/commit/7a487810c21edf0f4bee95a5344a0081b100cbeb))

- Bind the placement-lock guard to the queryset it checks
  ([`aef0434`](https://github.com/marcinpsk/netbox-data-import-plugin/commit/aef04345c436576d68016c1447d6677c182325c2))

- Block a dangling profile reference instead of reporting a row decision
  ([`bb35647`](https://github.com/marcinpsk/netbox-data-import-plugin/commit/bb356475fa2a659b54800d1f0127c4333e225d3d))

- Carry the CableClass decisions through the profile YAML
  ([`1295f52`](https://github.com/marcinpsk/netbox-data-import-plugin/commit/1295f52afeff27a4ee56f771bbe5de7d47baef85))

- Close five adversarial review findings on the Cable planner and the trace reader
  ([`7f7b808`](https://github.com/marcinpsk/netbox-data-import-plugin/commit/7f7b808969ae4a852c70e602da086adf6bbf6e78))

- Close three adversarial review findings on trace orientation and planning
  ([`aaf4b75`](https://github.com/marcinpsk/netbox-data-import-plugin/commit/aaf4b757b1fcbc5ea01c8bb5758c17088c892c16))

- Close three review findings on the Cable planner and the trace reader
  ([`7e5982f`](https://github.com/marcinpsk/netbox-data-import-plugin/commit/7e5982f5b3ae8ad34b1fa0490b1541546007c9e1))

- Derive the quick-action Device Type slug the way the importer does
  ([`ecc0318`](https://github.com/marcinpsk/netbox-data-import-plugin/commit/ecc03185b25749b5bf5206b2f34bc1f38e243465))

- Hold every placement reference the replan reads, and index port mappings
  ([`7beba1f`](https://github.com/marcinpsk/netbox-data-import-plugin/commit/7beba1fb02ca3cd306107c1da81b3338203a7fe7))

- Hold the import profile for every policy write
  ([`d991948`](https://github.com/marcinpsk/netbox-data-import-plugin/commit/d9919485c11dab7d0a8d3b0ece46fcfca97c8b7c))

- Lock the Device Type a review retains, not only the resolved one
  ([`e22cb79`](https://github.com/marcinpsk/netbox-data-import-plugin/commit/e22cb792dd06e7f8753ca87827cb1074c4d62248))

- Name the inline mapping actions for a screen reader
  ([`2fca690`](https://github.com/marcinpsk/netbox-data-import-plugin/commit/2fca6903f0f817ec3e466ecf8151c628b9fa7280))

- Narrow the AST types the placement-lock guard reads
  ([`11872c2`](https://github.com/marcinpsk/netbox-data-import-plugin/commit/11872c23c5a527fee0d0d83c901be5538e643799))

- Refuse a Device Type this import cannot size, and correct two preview affordances
  ([`233e7db`](https://github.com/marcinpsk/netbox-data-import-plugin/commit/233e7db319267c1646200bf93742773de989119f))

- State the Logical Cable deletion identity once, and date PortMapping correctly
  ([`66a3041`](https://github.com/marcinpsk/netbox-data-import-plugin/commit/66a3041945c6172e0eb5a657fdf42fa08c04308c))

- Take every placement lock in one primary-key ordered pass
  ([`4025882`](https://github.com/marcinpsk/netbox-data-import-plugin/commit/402588251cf1f8d64b110dd67107a10880ccfee2))

### Continuous Integration

- Gate mypy and spec-table drift locally, and sort the CableClass columns
  ([`fd1ebee`](https://github.com/marcinpsk/netbox-data-import-plugin/commit/fd1ebeefd53f7619435515a7f9b74ed884ff3a4d))

### Features

- Never create a Device Type, and require one that already exists
  ([`7150eca`](https://github.com/marcinpsk/netbox-data-import-plugin/commit/7150eca6cd6c135920383d246840cd646b24c57f))

### Refactoring

- Answer the runtime gate from a stated declaration table
  ([`a6c4920`](https://github.com/marcinpsk/netbox-data-import-plugin/commit/a6c492096ddab25c7699fcd2ebb7e734e6d738fc))

- Keep block position out of the duplicate selection key
  ([`e1bd84c`](https://github.com/marcinpsk/netbox-data-import-plugin/commit/e1bd84cc0e07ca3d5fbb81cdc43bbcc28e549023))

- Read the declaration table from a context variable
  ([`07bcddf`](https://github.com/marcinpsk/netbox-data-import-plugin/commit/07bcddf632491e5b693aa8a810242b81c07af1ef))

- Remove the unreachable Manufacturer quick-create action
  ([`1b59076`](https://github.com/marcinpsk/netbox-data-import-plugin/commit/1b59076a8e437c067255752b275ee635b6911a70))

- State the declaration table instead of replacing the constant
  ([`12cb2f3`](https://github.com/marcinpsk/netbox-data-import-plugin/commit/12cb2f363c45357d1d910ef5b2545f926c2d366e))

### Testing

- Assert the operator log keeps the refused permission name
  ([`73df89b`](https://github.com/marcinpsk/netbox-data-import-plugin/commit/73df89b2255841f4b903aa7faa16cbb1d005c00d))

### Breaking Changes

- An Import Profile no longer creates missing Device Types or Manufacturers. Add the Device Type in
  NetBox, or map it, before importing a row that needs it.


## v1.12.0 (2026-09-04)

### Bug Fixes

- Close four review findings on the preview and the trace reader
  ([`e09c963`](https://github.com/marcinpsk/netbox-data-import-plugin/commit/e09c963d1b100d84db4470decffc23324c3664c0))

- Close the CodeRabbit findings on the trace adapter
  ([`f71fa12`](https://github.com/marcinpsk/netbox-data-import-plugin/commit/f71fa12b1d34cd386abc16874e9300e0788aa12d))

- Close the trace adapter review findings
  ([`e063d63`](https://github.com/marcinpsk/netbox-data-import-plugin/commit/e063d63a5301b7bcfb876ce6237867d6fa8bdf60))

- Identify a trace termination by the kind its PortClass claims
  ([`f801f44`](https://github.com/marcinpsk/netbox-data-import-plugin/commit/f801f4422da11e9515460253114a0884fbec888b))

- Keep corroboration a later sheet states, and release the workbook on a raise
  ([`cd92e45`](https://github.com/marcinpsk/netbox-data-import-plugin/commit/cd92e45d67c898facea43fd8c2877c3bfd3353b9))

- Keep the placement baseline on a row refused for an identity conflict
  ([`0c10665`](https://github.com/marcinpsk/netbox-data-import-plugin/commit/0c10665c822c0cfb120a8e87a4277a90d755fcea))

- Offer only runnable adapters in the REST schema
  ([`5d98524`](https://github.com/marcinpsk/netbox-data-import-plugin/commit/5d985245b49c45d290f3f2ce4a3b8bf5efaf8b83))

- Recheck the placement baseline under a row lock
  ([`cfa9954`](https://github.com/marcinpsk/netbox-data-import-plugin/commit/cfa995468b3db670f5b3f7ed2cebe577e511615c))

- Roll back the placement transaction when validation fails
  ([`6bf2b83`](https://github.com/marcinpsk/netbox-data-import-plugin/commit/6bf2b83da3a4fe7cc2625143cce80649ffb10949))

- State each adapter label in the REST create schema
  ([`b958e3d`](https://github.com/marcinpsk/netbox-data-import-plugin/commit/b958e3daaabd1f1e292b09c3a84255d9ec5da249))

- Two preview refusals that state less than they know
  ([`846055b`](https://github.com/marcinpsk/netbox-data-import-plugin/commit/846055b4a5cf95f7c27f875c40f557a039d166c0))

### Documentation

- State the sheet-pairing rule the adapter implements
  ([`584526d`](https://github.com/marcinpsk/netbox-data-import-plugin/commit/584526d78e83a45346478ead7ba0423b72719a57))

### Features

- Add the trace workbook Source Adapter
  ([`ce10724`](https://github.com/marcinpsk/netbox-data-import-plugin/commit/ce1072497259c2be9d5cef0848df4ca1abade37a))

### Refactoring

- Offer a preview row action from one table
  ([`5e59946`](https://github.com/marcinpsk/netbox-data-import-plugin/commit/5e59946c54785a39087fed2eba9528e692f61e83))

### Testing

- Guard the Source Adapter import boundary
  ([`ebfda81`](https://github.com/marcinpsk/netbox-data-import-plugin/commit/ebfda8163fa24a0556f5bb562d94237f2ad289d6))

- Pin the adapter the REST write-back must preserve
  ([`2a3b1f9`](https://github.com/marcinpsk/netbox-data-import-plugin/commit/2a3b1f9ca75444dda65413f759c0a2f4083b7837))


## v1.11.3 (2026-09-03)

### Bug Fixes

- Refuse an out-of-scope add before it takes the policy lock
  ([`c9230e8`](https://github.com/marcinpsk/netbox-data-import-plugin/commit/c9230e84d7f889ad4083c7740ba5b0475ae5a7fb))

- Scope the parent profile an add view attaches its row to
  ([`a81fd76`](https://github.com/marcinpsk/netbox-data-import-plugin/commit/a81fd768f1e0aa769a6f13f87ccda3b8dc72ab03))


## v1.11.2 (2026-09-02)

### Bug Fixes

- Check the wording table by its keys, and let the test own its expectations
  ([`4eddd15`](https://github.com/marcinpsk/netbox-data-import-plugin/commit/4eddd15bcee1dad46b26021e1779c59fbfb34730))

- Keep a failed sync row disabled once another write made the preview stale
  ([`ea0fd7f`](https://github.com/marcinpsk/netbox-data-import-plugin/commit/ea0fd7f75df2a735503d4d7a67be0c3b3011ba40))

- Keep a refused row's Contact off the Device it named
  ([`d1d41d9`](https://github.com/marcinpsk/netbox-data-import-plugin/commit/d1d41d996769b2e4a23abb35dda59d4c9c6841af))

- Keep the Contact a save created linked to its row
  ([`6944c4e`](https://github.com/marcinpsk/netbox-data-import-plugin/commit/6944c4e7f8d57177752602391a65d14a6df879f6))

- Recalculate the preview when the last pending sync fails
  ([`30ddb81`](https://github.com/marcinpsk/netbox-data-import-plugin/commit/30ddb81ad406c6edb749482c4b4c6c09cf02fe45))

- Refuse a selected Contact whose lookup value moved
  ([`85f6b87`](https://github.com/marcinpsk/netbox-data-import-plugin/commit/85f6b878c30035481fa98ffe7a2e0665751bf3f1))

- Report a Contact write by what it changed, not by whether one ran
  ([`fa4906c`](https://github.com/marcinpsk/netbox-data-import-plugin/commit/fa4906cb15f512607c1c7499c30dcd5c957c8a36))

### Chores

- **deps**: Bump the github-actions group with 2 updates
  ([`8873f62`](https://github.com/marcinpsk/netbox-data-import-plugin/commit/8873f62ffd1b513f878121e2ce4f1c5c7134316b))

- **deps-dev**: Bump ruff from 0.16.3 to 0.16.4
  ([`a98c65f`](https://github.com/marcinpsk/netbox-data-import-plugin/commit/a98c65fecaa230fe283070a35a298b992180a300))

### Documentation

- State the profile ownership contract the forms actually have, and widen two assertions
  ([`6523863`](https://github.com/marcinpsk/netbox-data-import-plugin/commit/652386382b6c348c839ff050c0abc7edc2e90669))

### Testing

- State the current constraint in the device module test docstring
  ([`a0f22b5`](https://github.com/marcinpsk/netbox-data-import-plugin/commit/a0f22b5c44b6fb852d45bc0338b4a009071d774a))


## v1.11.1 (2026-09-01)


## v1.11.0 (2026-08-31)

### Bug Fixes

- Actually move the parsing out of the engine, and cover what layer 3 added
  ([`c732523`](https://github.com/marcinpsk/netbox-data-import-plugin/commit/c73252314784c215064d4edb053fe7e8814b67c7))

- Address import engine review findings
  ([`776bc0a`](https://github.com/marcinpsk/netbox-data-import-plugin/commit/776bc0ad423b2255f7699c5f7f0fc734746677a4))

- Address import engine review findings
  ([`62274e2`](https://github.com/marcinpsk/netbox-data-import-plugin/commit/62274e25ee340e23839ca9570ebd05c4292c3f12))

- Align import planning with writer behavior
  ([`4746157`](https://github.com/marcinpsk/netbox-data-import-plugin/commit/4746157137902d2e688888a40e40182c97ac832d))

- Apply saved Source Resolutions when the coordinator plans
  ([`1638013`](https://github.com/marcinpsk/netbox-data-import-plugin/commit/1638013a3bfc734d3be0eabe6cd8f83a8354d0bb))

- Bind row sync state to its request
  ([`130f628`](https://github.com/marcinpsk/netbox-data-import-plugin/commit/130f628e1fa02fefdd2ffb4f1992a09b0c4058a0))

- Bound the address token so a cell cannot set the scan length
  ([`5441f80`](https://github.com/marcinpsk/netbox-data-import-plugin/commit/5441f80695e662775088411ced6d195b14970867))

- Bound transform work across workbooks
  ([`c199254`](https://github.com/marcinpsk/netbox-data-import-plugin/commit/c199254f6befef93924de5a7c3f391fb85301076))

- Check the declared type of each scalar plan field
  ([`d62d8fc`](https://github.com/marcinpsk/netbox-data-import-plugin/commit/d62d8fc94f985ddb1ce19cb42c5b987753a5ea2e))

- Clarify contact resolution validation
  ([`226afd5`](https://github.com/marcinpsk/netbox-data-import-plugin/commit/226afd575c4bb5c03fd0507f33ee70b570155d3e))

- Close final import review findings
  ([`01e1bf5`](https://github.com/marcinpsk/netbox-data-import-plugin/commit/01e1bf51a6387c7510b5053cf13a16ba0986620b))

- Close final import review findings
  ([`565e2ef`](https://github.com/marcinpsk/netbox-data-import-plugin/commit/565e2ef56f05bf0e695dad8ef8a451719d634ace))

- Close import review follow-ups
  ([`13f7fa6`](https://github.com/marcinpsk/netbox-data-import-plugin/commit/13f7fa6d1e061260fd31813d80abbec2192139ec))

- Close remaining import cutover review findings
  ([`a23867a`](https://github.com/marcinpsk/netbox-data-import-plugin/commit/a23867a7f64f1a248790c9f65ab70d8fb88b4624))

- Close remaining import review findings
  ([`e824708`](https://github.com/marcinpsk/netbox-data-import-plugin/commit/e824708fc1ea2bb9736a366646fa1f95875fd3c3))

- Close the follow-up review findings on the audit record and the plan
  ([`67eb385`](https://github.com/marcinpsk/netbox-data-import-plugin/commit/67eb385baa5cffdaa7d874c991294e9f2682e947))

- Close the review findings on the plan and audit models
  ([`60a2609`](https://github.com/marcinpsk/netbox-data-import-plugin/commit/60a2609e21b55fc2cbb50c79580c5b6ae9d80d97))

- Close the review findings on the plan and the target modules
  ([`1891df0`](https://github.com/marcinpsk/netbox-data-import-plugin/commit/1891df052383c0f6d907b28155e933bd60bc38b6))

- Close the second round of review findings on the audit record
  ([`5135b70`](https://github.com/marcinpsk/netbox-data-import-plugin/commit/5135b70b55a0c4f7959b111eece751c3a33404a7))

- Enforce plan and execution contracts
  ([`0faf3df`](https://github.com/marcinpsk/netbox-data-import-plugin/commit/0faf3df25bed59490b3188549a1d003b9a6c82ec))

- Follow the squashed profile cutover on develop
  ([`55c7b16`](https://github.com/marcinpsk/netbox-data-import-plugin/commit/55c7b1679860b6bdef2009760406f13f1ac81a48))

- Freeze import plan values recursively
  ([`da7074c`](https://github.com/marcinpsk/netbox-data-import-plugin/commit/da7074c52a908502866acd3de83d51b60b6b77c8))

- Harden import cutover edge cases
  ([`baa60cc`](https://github.com/marcinpsk/netbox-data-import-plugin/commit/baa60cc731cf7ac802e96396cc4f870d6d24c6c5))

- Isolate conflict resolution requests
  ([`e6b449c`](https://github.com/marcinpsk/netbox-data-import-plugin/commit/e6b449c35d5b5fe37476b28dfb6b407c15ce700b))

- Let a device row use the rack its own batch creates
  ([`9ff36d7`](https://github.com/marcinpsk/netbox-data-import-plugin/commit/9ff36d74d8cab381f627080d6c228665ac3b2bd3))

- Normalize device type resolver inputs
  ([`ca5972f`](https://github.com/marcinpsk/netbox-data-import-plugin/commit/ca5972f45041ba0b33684cd3ebc5b673e5f2bdd1))

- Normalize persisted ignored source IDs
  ([`6033a51`](https://github.com/marcinpsk/netbox-data-import-plugin/commit/6033a51441c7f7851bdbf44b8824daefb439e950))

- Normalize target module source values
  ([`708f240`](https://github.com/marcinpsk/netbox-data-import-plugin/commit/708f2408ddec02d7a3c00f42e7e1416d0df7f230))

- Preserve device ignore precedence
  ([`e4f3dcc`](https://github.com/marcinpsk/netbox-data-import-plugin/commit/e4f3dcc1093e9ccf30dd7ba49e9a6227bfd7f2e7))

- Preserve device rejection precedence
  ([`4646f08`](https://github.com/marcinpsk/netbox-data-import-plugin/commit/4646f08d6732130269ea8be54f229799b5ea8917))

- Preserve import audit job links
  ([`00b3ca4`](https://github.com/marcinpsk/netbox-data-import-plugin/commit/00b3ca41873b230abc6b4bc8352ab195b607d798))

- Preserve import review contracts
  ([`32b6c82`](https://github.com/marcinpsk/netbox-data-import-plugin/commit/32b6c82e72c0ae15b5a82457b5781f58fe4e6acb))

- Preserve safe bulk transform matching
  ([`ff59d4d`](https://github.com/marcinpsk/netbox-data-import-plugin/commit/ff59d4db5ee564cfdad7b96eba0bba2d2869981d))

- Preview a device against the rack this batch leaves behind
  ([`b22458f`](https://github.com/marcinpsk/netbox-data-import-plugin/commit/b22458f8796a02020e2eb19d92ac26dde4e2bff7))

- Refuse a retired adapter before suggesting a contact
  ([`ac1c164`](https://github.com/marcinpsk/netbox-data-import-plugin/commit/ac1c164c122463d3f3598cc36e2e0dc73a8c52de))

- Regenerate audit migrations after cutover
  ([`114275a`](https://github.com/marcinpsk/netbox-data-import-plugin/commit/114275a3a8b98b9ff6239a2655d7ddd724e3dd9b))

- Report the import target and the source-ID field as device work
  ([`e6080e0`](https://github.com/marcinpsk/netbox-data-import-plugin/commit/e6080e01b128fcd5a21c747cec5c968cd1fac6a8))

- Resolve final import review findings
  ([`bbe96c6`](https://github.com/marcinpsk/netbox-data-import-plugin/commit/bbe96c68b9bcd85ea3985e0db38c9bd9b044c223))

- Resolve import audit review findings
  ([`7d728e2`](https://github.com/marcinpsk/netbox-data-import-plugin/commit/7d728e2c99b99ecdadb6c8055710f41ea78a6a48))

- Resolve import engine review findings
  ([`e7460ad`](https://github.com/marcinpsk/netbox-data-import-plugin/commit/e7460adb475b79810e7e08fcba0c8568e9db7d73))

- Resolve import review findings
  ([`e112ea0`](https://github.com/marcinpsk/netbox-data-import-plugin/commit/e112ea0b30f414c2d83cdaa64bb2a010e1d8574e))

- Resolve latest import engine review findings
  ([`744ba8e`](https://github.com/marcinpsk/netbox-data-import-plugin/commit/744ba8ead84de29886883b4f5ec605736535314a))

- Resolve latest import review findings
  ([`1d568e3`](https://github.com/marcinpsk/netbox-data-import-plugin/commit/1d568e37cd647358f91d1dc7cbb53d093ebfa268))

- Resolve latest import review findings
  ([`68e748f`](https://github.com/marcinpsk/netbox-data-import-plugin/commit/68e748facf3ca3b80e99c9694d648d42b15c4019))

- Resolve release review findings
  ([`e8e3ca3`](https://github.com/marcinpsk/netbox-data-import-plugin/commit/e8e3ca3aa3a139c8cbae36aee3aabed022e8e52b))

- Resolve remaining import review findings
  ([`c893a02`](https://github.com/marcinpsk/netbox-data-import-plugin/commit/c893a024d9e89e89c89404253aa2e1e7bf2a6cd3))

- Restack audit migrations after profile cutover
  ([`b1d23f6`](https://github.com/marcinpsk/netbox-data-import-plugin/commit/b1d23f63675f29847698457f0509dce3720aa426))

- Scope null normalization to source values
  ([`b9fbce5`](https://github.com/marcinpsk/netbox-data-import-plugin/commit/b9fbce5915a34cacdc62fa9479cefd5e8b1aceb5))

- Serialize conflict resolution requests
  ([`b984a67`](https://github.com/marcinpsk/netbox-data-import-plugin/commit/b984a679b51247953305b66ca1c2feaaa0fba6e9))

- Stop the Device module renaming a device it matched
  ([`bad1582`](https://github.com/marcinpsk/netbox-data-import-plugin/commit/bad15827fdf7d351ada3693318467e2748956d5c))

- Unify device identity matching
  ([`60d7ac5`](https://github.com/marcinpsk/netbox-data-import-plugin/commit/60d7ac5d1357ff2b74694c939a0e1a06038b7a60))

- Validate import plan schema versions
  ([`b65e06b`](https://github.com/marcinpsk/netbox-data-import-plugin/commit/b65e06b7eb64d7023f0742e798589e28b4ff4e9e))

- Validate saved resolution target fields
  ([`9470b93`](https://github.com/marcinpsk/netbox-data-import-plugin/commit/9470b931a8cb82167ede81c1e2a7511685ac75d1))

- Validate the plan's scalar fields at construction
  ([`d6a4b08`](https://github.com/marcinpsk/netbox-data-import-plugin/commit/d6a4b089aec909d5cdfa59bc5741a2d97ef329c6))

### Build System

- Add mypy, and the two stub packages this project actually needs
  ([`355b105`](https://github.com/marcinpsk/netbox-data-import-plugin/commit/355b1051eee284c0b7dc91fcd0d2a596180e7ab9))

### Code Style

- Give each model comment its reason in one line
  ([`06b989b`](https://github.com/marcinpsk/netbox-data-import-plugin/commit/06b989bfab948db53f9e7bc3ff9739cb7d650cf1))

- Give this branch's parallel-setup comments one line each
  ([`32b8f1b`](https://github.com/marcinpsk/netbox-data-import-plugin/commit/32b8f1bc479d81dfd62dede038fcf3533e72efe0))

- State the plan scalar-check reason in one line
  ([`d69d43b`](https://github.com/marcinpsk/netbox-data-import-plugin/commit/d69d43bac9cff445debf9eb22f9baeaa62fb1b74))

### Documentation

- Correct NetBox minimum version
  ([`e950e3b`](https://github.com/marcinpsk/netbox-data-import-plugin/commit/e950e3b29843eae8c0486edf7c292f2aaa6ca2bc))

- Correct RE2 Unicode migration guidance
  ([`cf86682`](https://github.com/marcinpsk/netbox-data-import-plugin/commit/cf866825f4a97a388ecedec0c0dc0e38b03e43a3))

- Cut test rationale to the one non-obvious line
  ([`17e7965`](https://github.com/marcinpsk/netbox-data-import-plugin/commit/17e7965e6bac44fdd17894d8aeed9866f4d990ab))

- Drop two comments that restate the code
  ([`c30bfe1`](https://github.com/marcinpsk/netbox-data-import-plugin/commit/c30bfe1c376accf242b864a4d6058fc784baced3))

- One-line the comments and docstrings the review flagged
  ([`f8e2780`](https://github.com/marcinpsk/netbox-data-import-plugin/commit/f8e2780d98894f6a1bf680bd6b4c0e9aaa036701))

### Features

- Add the source document and the Import Execution audit record
  ([`2d509c5`](https://github.com/marcinpsk/netbox-data-import-plugin/commit/2d509c5dc8d98f441e58f0b6f7cdbfbc73da6b75))

- Add the target-neutral Import Plan model
  ([`ebccbd2`](https://github.com/marcinpsk/netbox-data-import-plugin/commit/ebccbd2b587720e1c7d728bf8eb3173b113a351c))

- Apply one device change under its own lock and preconditions
  ([`97d5c64`](https://github.com/marcinpsk/netbox-data-import-plugin/commit/97d5c6404244e13cdb0a2ad0256a4da134072411))

- Apply one rack change under its own lock and preconditions
  ([`413e731`](https://github.com/marcinpsk/netbox-data-import-plugin/commit/413e7316da119bdd869c55f5d870b6f2dd2d5110))

- Cut import workflow over to import engine
  ([`c4ba988`](https://github.com/marcinpsk/netbox-data-import-plugin/commit/c4ba988a08d9c01aa7a4145c108dfd37ade31189))

- Decide a device's primary contact in the plan
  ([`5ab99ef`](https://github.com/marcinpsk/netbox-data-import-plugin/commit/5ab99ef79f305e1f7d57cafd5a7b382d316d7ec2))

- Execute an accepted plan as one audited transaction
  ([`6794c07`](https://github.com/marcinpsk/netbox-data-import-plugin/commit/6794c076b27620712d14fde2dd440088aad9fd8c))

- Move flat-workbook parsing behind the Source Adapter seam
  ([`1d05170`](https://github.com/marcinpsk/netbox-data-import-plugin/commit/1d05170d57dcf48c864105decff5620da0aee743))

- Place a device's addresses through the Device module
  ([`979b94a`](https://github.com/marcinpsk/netbox-data-import-plugin/commit/979b94a70a26e5bbc02973480a3d5c5da241b77a))

- Plan a device through the operator's saved field review
  ([`69eed00`](https://github.com/marcinpsk/netbox-data-import-plugin/commit/69eed006c821392c0734a384938494dec484bd8d))

- Plan an import through the target-neutral coordinator
  ([`b21dbdd`](https://github.com/marcinpsk/netbox-data-import-plugin/commit/b21dbdd58251e540b79a51aa5a026703ac87a005))

- Plan device placement, and share the source-word tables
  ([`9377530`](https://github.com/marcinpsk/netbox-data-import-plugin/commit/937753070fe3c72c099c8d229dc81856d0051398))

- Plan devices through a Target Module
  ([`0838d34`](https://github.com/marcinpsk/netbox-data-import-plugin/commit/0838d34da5f4628713c6452a85664f97a06bd9f4))

- Plan racks through a Target Module
  ([`50a596d`](https://github.com/marcinpsk/netbox-data-import-plugin/commit/50a596d7aac757a9b7575d39156b388a1bc9012e))

- Read target state through one permission-scoped accessor
  ([`0732caa`](https://github.com/marcinpsk/netbox-data-import-plugin/commit/0732caaf3cafb9ba23545cc2d8f3caeeff5d8b4b))

- Record and read the source that wrote a device
  ([`6409285`](https://github.com/marcinpsk/netbox-data-import-plugin/commit/6409285725c2c1d672415032c3d1e5ae9f966fe8))

### Performance Improvements

- Defer preview recalculation after review actions
  ([`f543efb`](https://github.com/marcinpsk/netbox-data-import-plugin/commit/f543efbf0442ce7b0c92a34469b177bd36cfcf19))

### Refactoring

- Share the Device Type identity resolver with the target modules
  ([`58a64ca`](https://github.com/marcinpsk/netbox-data-import-plugin/commit/58a64ca5ef08fa1701bb53328dc64afc2ded6e14))

- State the held-address invariant once, on the target
  ([`02e4626`](https://github.com/marcinpsk/netbox-data-import-plugin/commit/02e4626143934db6657e7a23b971504f1b3f6260))

### Testing

- Correct the permission-test docstrings
  ([`565c352`](https://github.com/marcinpsk/netbox-data-import-plugin/commit/565c3522a18de752ee73f28481da51534f3133ac))

- Cover the IP paths the coverage gate found untested
  ([`3027076`](https://github.com/marcinpsk/netbox-data-import-plugin/commit/30270767f21b4cc1a5810b6146fe0be55dbac9a4))

- Cover the plan and catalog lookups the gate found untested
  ([`67ddbf2`](https://github.com/marcinpsk/netbox-data-import-plugin/commit/67ddbf2a716db18a259e774936a1354eef703020))

- Cover the preview's IP normalization fallback
  ([`f1927af`](https://github.com/marcinpsk/netbox-data-import-plugin/commit/f1927af16639457d5db18817b2323aeb93ed2ebc))

- Guard the one-line comment rule with a shrinking record
  ([`e3c79ca`](https://github.com/marcinpsk/netbox-data-import-plugin/commit/e3c79ca50ab4d5d46afb1cf1653dddf5ec53f858))

- Guard the views-to-engine seam, and stop the comment record fighting branches
  ([`e52171e`](https://github.com/marcinpsk/netbox-data-import-plugin/commit/e52171eb811d73f14a44afc22b864cd8c57cbc74))

- Reuse the workbook bytes helper
  ([`cf00291`](https://github.com/marcinpsk/netbox-data-import-plugin/commit/cf00291cda36e9dccfb698328aaa01e398fdcb77))

- Share the stacked permission helper
  ([`d1ab562`](https://github.com/marcinpsk/netbox-data-import-plugin/commit/d1ab562417e34721e70f8937ed1efba178416f4f))

- Stop a permission test from passing on a failed login
  ([`c41fdd5`](https://github.com/marcinpsk/netbox-data-import-plugin/commit/c41fdd5a6d09128b101587a6a159c718f87bdf8b))


## v1.10.0 (2026-08-28)

### Bug Fixes

- Count only the differences the import applies
  ([`453772c`](https://github.com/marcinpsk/netbox-data-import-plugin/commit/453772c11e70872f303f121865f0dc76431a5185))

- Decide an ignored address once for the preview and the writer
  ([`9be0391`](https://github.com/marcinpsk/netbox-data-import-plugin/commit/9be0391ea7c9121bc17e0227d3d324b3bcdf9893))

### Features

- Settle a serial collision from the conflict comparison
  ([`65c7e66`](https://github.com/marcinpsk/netbox-data-import-plugin/commit/65c7e66958b5e71954a35592c6d6ea54ed5af77b))

### Testing

- Unpack only the map each field-difference test reads
  ([`fdecf5e`](https://github.com/marcinpsk/netbox-data-import-plugin/commit/fdecf5e6a35e94d7a7a7eeb08dfcb985d278d3de))


## v1.9.0 (2026-08-28)

### Bug Fixes

- Apply the findings from the adversarial review
  ([`c1d26f3`](https://github.com/marcinpsk/netbox-data-import-plugin/commit/c1d26f3605c4d3ece6a26813451ce365e03f484f))

- Check the split part that names the device, not the second one
  ([`77f0c14`](https://github.com/marcinpsk/netbox-data-import-plugin/commit/77f0c141449771ff5d8eb82217b7cb68f4823625))

- Keep the writer and the preview reading one set of zero-U fields
  ([`2b47397`](https://github.com/marcinpsk/netbox-data-import-plugin/commit/2b47397500c21310584a17212e7619d5aa1f2de9))

- Name the field behind a row's conflict count
  ([`61021bf`](https://github.com/marcinpsk/netbox-data-import-plugin/commit/61021bf8cc6bd7578961b82f09992b0250dfdbd8))

- Stop offering a rack position a zero-U device type cannot hold
  ([`4747192`](https://github.com/marcinpsk/netbox-data-import-plugin/commit/4747192a42e284ebe9b26303f68e9cd21b70fb98))

### Features

- Compare every row a preview conflict names
  ([`6ce6445`](https://github.com/marcinpsk/netbox-data-import-plugin/commit/6ce6445cd9ec7796a63eb659787c4a54bcff6247))

### Testing

- Drive the split modal through real Bootstrap
  ([`0d33fd7`](https://github.com/marcinpsk/netbox-data-import-plugin/commit/0d33fd74d88031a0baf0955ba3027fedf5f152df))


## v1.8.0 (2026-08-28)

### Bug Fixes

- Assign IP fields when the import creates a device
  ([`417b9b7`](https://github.com/marcinpsk/netbox-data-import-plugin/commit/417b9b7578c7dc4188e72bc7a41660e704580cc1))

- Honour an ignored IP difference and refuse a truncated address
  ([`73b2164`](https://github.com/marcinpsk/netbox-data-import-plugin/commit/73b216423a586dedc7372ec7a6efe6b8c5eea414))

- Import the address onto the interface the preview named
  ([`eae2b49`](https://github.com/marcinpsk/netbox-data-import-plugin/commit/eae2b49dc5bba9ae8203d35e36114eb7e8ecc79b))

- Resolve the saved import target in the operator's own scope
  ([`9ea9599`](https://github.com/marcinpsk/netbox-data-import-plugin/commit/9ea9599bbd864a2be1a6dd77b1bfacfb3d20e3d9))

- Scope an address by VRF and family, and name its interface in the preview
  ([`7ff2a96`](https://github.com/marcinpsk/netbox-data-import-plugin/commit/7ff2a96a71ec9db3deba80dd8166117cce8f6496))

- Scope the saved import target in the two remaining row actions
  ([`835e497`](https://github.com/marcinpsk/netbox-data-import-plugin/commit/835e49743979fcd74e7850bdc77043c547648e30))

- Show and recover the IP a row carries, and reach the split from a matched row
  ([`4aa0e17`](https://github.com/marcinpsk/netbox-data-import-plugin/commit/4aa0e17bcb39f7da640794ba6bd5bf708dd511e3))

### Code Style

- Group the restricted-client test imports
  ([`d9ff411`](https://github.com/marcinpsk/netbox-data-import-plugin/commit/d9ff4117a37cad1b2b72a323c0d1fa99872764a1))

- Keep the comment guard docstring to its runtime contract
  ([`5684a0c`](https://github.com/marcinpsk/netbox-data-import-plugin/commit/5684a0cb2972f2f66c6bf2b9195d4bd8ed6c52af))

- State the IP token boundary in one line
  ([`11fd295`](https://github.com/marcinpsk/netbox-data-import-plugin/commit/11fd295498f87cddf6ec727abc1b36ce270ee9c9))

- State the sync transaction reason in one line
  ([`7dc2399`](https://github.com/marcinpsk/netbox-data-import-plugin/commit/7dc2399b3337e901ba11abfeb3a8ce578e267911))

### Features

- Sync an address onto a device interface, and sync airflow
  ([`eff29c4`](https://github.com/marcinpsk/netbox-data-import-plugin/commit/eff29c4e89df5b8226fafd1863f4cee5af819b45))

### Testing

- Guard the one-line comment rule with a shrinking record
  ([`e15f081`](https://github.com/marcinpsk/netbox-data-import-plugin/commit/e15f0819860d395591aaaa96b855a36b3e1fac14))


## v1.7.0 (2026-08-27)

### Bug Fixes

- Bind a serial decision to the serial the preview named
  ([`2ba9f33`](https://github.com/marcinpsk/netbox-data-import-plugin/commit/2ba9f330754db45d7c1da569a85fb4b64febb6ff))

- Check a replacement device name under the lock it writes with
  ([`9933e56`](https://github.com/marcinpsk/netbox-data-import-plugin/commit/9933e563709847fef7cc4926a3725b9dfb1020a3))

- Give a duplicate serial somewhere to go, and carry the sync write in a transaction
  ([`57f43eb`](https://github.com/marcinpsk/netbox-data-import-plugin/commit/57f43ebd93f749ea1c696f11970f83fff182f498))

- Judge a serial collision by the rows the import will create
  ([`f6f336a`](https://github.com/marcinpsk/netbox-data-import-plugin/commit/f6f336a55905c1bc907ad3ebd8d518756ca88352))

- Log how long the serial recheck holds the profile lock
  ([`5c61fce`](https://github.com/marcinpsk/netbox-data-import-plugin/commit/5c61fce370862193184121e213f095aa0c5164da))

- Only settle a serial collision that is still live
  ([`c73eb97`](https://github.com/marcinpsk/netbox-data-import-plugin/commit/c73eb9793836f59245552b707ff30eb5f0f9d097))

### Chores

- **deps**: Bump the github-actions group with 3 updates
  ([#113](https://github.com/marcinpsk/netbox-data-import-plugin/pull/113),
  [`57c9c63`](https://github.com/marcinpsk/netbox-data-import-plugin/commit/57c9c63d65b4d1108283112bee03a7ef8c603683))

### Features

- Name the NetBox device a preview row matched
  ([`8de70a2`](https://github.com/marcinpsk/netbox-data-import-plugin/commit/8de70a290fd036c28ac35811cfbe707a00e93205))

### Refactoring

- Give the two preview row decisions one set of preconditions
  ([`abfb3c2`](https://github.com/marcinpsk/netbox-data-import-plugin/commit/abfb3c259f2f84efcf81bd9901a69663267b2268))

### Testing

- Assert the matched badge carries the device link itself
  ([`21248c6`](https://github.com/marcinpsk/netbox-data-import-plugin/commit/21248c6274ea4a1b37ccd5e91823a66796fa058b))

- Audit the policy locks like any other atomic block
  ([`6096467`](https://github.com/marcinpsk/netbox-data-import-plugin/commit/609646709a4f06ef77f496ba53093ce64e76c407))

- Read the preview row and the badge target without literals
  ([`c96d411`](https://github.com/marcinpsk/netbox-data-import-plugin/commit/c96d411c5c37a50a4070331998157d950ba4d3a5))


## v1.6.1 (2026-08-26)

### Bug Fixes

- Close the three review follow-ups deferred from #103
  ([#115](https://github.com/marcinpsk/netbox-data-import-plugin/pull/115),
  [`0893a85`](https://github.com/marcinpsk/netbox-data-import-plugin/commit/0893a850c675499219562ce048d864d76c3d774b))


## v1.6.0 (2026-08-25)

### Continuous Integration

- Audit the workflows with zizmor and guard the release job
  ([#100](https://github.com/marcinpsk/netbox-data-import-plugin/pull/100),
  [`7e8e680`](https://github.com/marcinpsk/netbox-data-import-plugin/commit/7e8e680b6a75428b545fe417ea2cd6fc72dbd95c))

### Features

- Add the target-field catalog, adapter registry, and profile cut…
  ([#103](https://github.com/marcinpsk/netbox-data-import-plugin/pull/103),
  [`9cce873`](https://github.com/marcinpsk/netbox-data-import-plugin/commit/9cce873398f8114fb726ef5a6de76574abf051a4))


## v1.5.2 (2026-08-18)

### Bug Fixes

- Drop --offline from the release build command
  ([#99](https://github.com/marcinpsk/netbox-data-import-plugin/pull/99),
  [`8d8ea20`](https://github.com/marcinpsk/netbox-data-import-plugin/commit/8d8ea205efd0a5850036be372dd5bd96a0cdaf14))

- Placement, contact picker, preview interaction, and device import storage
  ([#87](https://github.com/marcinpsk/netbox-data-import-plugin/pull/87),
  [`ad1bcbe`](https://github.com/marcinpsk/netbox-data-import-plugin/commit/ad1bcbec890d1fc2559e3b9e8a277b2d207ea14a))

### Chores

- Mount the postgres 18 data parent directory
  ([#76](https://github.com/marcinpsk/netbox-data-import-plugin/pull/76),
  [`a138987`](https://github.com/marcinpsk/netbox-data-import-plugin/commit/a13898764a38a8330e27148836e9088ae2f50550))


## v1.5.1 (2026-08-17)

### Bug Fixes

- Improve contact resolution and preview actions
  ([#75](https://github.com/marcinpsk/netbox-data-import-plugin/pull/75),
  [`1a612cc`](https://github.com/marcinpsk/netbox-data-import-plugin/commit/1a612cce76f5fb8840f381686854540762ce0dc5))


## v1.5.0 (2026-08-14)

### Chores

- **deps**: Bump the github-actions group with 2 updates
  ([#73](https://github.com/marcinpsk/netbox-data-import-plugin/pull/73),
  [`84c67d7`](https://github.com/marcinpsk/netbox-data-import-plugin/commit/84c67d77bf5083ffe72e7b18e17b996803231fb9))

- **deps-dev**: Bump pytest-django from 4.12.0 to 4.13.0
  ([#71](https://github.com/marcinpsk/netbox-data-import-plugin/pull/71),
  [`28ce416`](https://github.com/marcinpsk/netbox-data-import-plugin/commit/28ce416b6b497bc71c291a0176c9ff44e8df05aa))

- **deps-dev**: Bump ruff from 0.16.1 to 0.16.2
  ([#72](https://github.com/marcinpsk/netbox-data-import-plugin/pull/72),
  [`09fbf70`](https://github.com/marcinpsk/netbox-data-import-plugin/commit/09fbf707069291fcc544e20931e9543f56787f77))

### Features

- Sync native contacts and improve import progress
  ([#74](https://github.com/marcinpsk/netbox-data-import-plugin/pull/74),
  [`da2e026`](https://github.com/marcinpsk/netbox-data-import-plugin/commit/da2e0269cf0663d0bc953ac5bcad075330b385c8))


## v1.4.2 (2026-08-11)

### Bug Fixes

- Check that PR titles follow Conventional Commits
  ([#70](https://github.com/marcinpsk/netbox-data-import-plugin/pull/70),
  [`6ee531f`](https://github.com/marcinpsk/netbox-data-import-plugin/commit/6ee531f19b3b5eeb64fa8c3a63c15ce7925032cf))

### Chores

- Add CODEOWNERS to auto-request @marcinpsk on PRs
  ([`d613def`](https://github.com/marcinpsk/netbox-data-import-plugin/commit/d613def827b2ab4cbe248854f23ed1763eb9a837))

- Cover .github/CODEOWNERS in REUSE.toml
  ([#57](https://github.com/marcinpsk/netbox-data-import-plugin/pull/57),
  [`15ea082`](https://github.com/marcinpsk/netbox-data-import-plugin/commit/15ea0822dc92dadc4f4714c8867ffb9e017af044))

- **deps**: Bump actions/checkout from 6.0.3 to 7.0.0 in the github-actions group
  ([#49](https://github.com/marcinpsk/netbox-data-import-plugin/pull/49),
  [`e3ad795`](https://github.com/marcinpsk/netbox-data-import-plugin/commit/e3ad795c74f2855cd859403242fa6154338fd910))

- **deps**: Bump actions/setup-python from 6.2.0 to 6.3.0 in the github-actions group
  ([#52](https://github.com/marcinpsk/netbox-data-import-plugin/pull/52),
  [`fdc723a`](https://github.com/marcinpsk/netbox-data-import-plugin/commit/fdc723a88ea8a9847a52f425f38b160d4f2a031a))

- **deps**: Bump github/codeql-action from 4.35.5 to 4.36.0 in the github-actions group
  ([#41](https://github.com/marcinpsk/netbox-data-import-plugin/pull/41),
  [`4e24fa2`](https://github.com/marcinpsk/netbox-data-import-plugin/commit/4e24fa2339c2c61d2c8488e95fe5e2759e0fbb44))

- **deps**: Bump github/codeql-action from 4.36.1 to 4.36.2 in the github-actions group
  ([#44](https://github.com/marcinpsk/netbox-data-import-plugin/pull/44),
  [`38496de`](https://github.com/marcinpsk/netbox-data-import-plugin/commit/38496de6ce9e270b2b906cac7b51f5c4181ed72c))

- **deps**: Bump gitpython from 3.1.50 to 3.1.54 in the uv group across 1 directory
  ([#61](https://github.com/marcinpsk/netbox-data-import-plugin/pull/61),
  [`daec75c`](https://github.com/marcinpsk/netbox-data-import-plugin/commit/daec75c91825c79c18f21a8a416cda1a16ab5061))

- **deps**: Bump pymdown-extensions from 10.21.3 to 11.0 in the uv group across 1 directory
  ([#65](https://github.com/marcinpsk/netbox-data-import-plugin/pull/65),
  [`27f3092`](https://github.com/marcinpsk/netbox-data-import-plugin/commit/27f3092645f6a74a4229e2465be8afdb0ddf8bf6))

- **deps**: Bump pymdown-extensions from 11.0 to 11.0.1 in the uv group across 1 directory
  ([#68](https://github.com/marcinpsk/netbox-data-import-plugin/pull/68),
  [`4349205`](https://github.com/marcinpsk/netbox-data-import-plugin/commit/4349205be5e849e7ee42fc00ec3bf9c8dc6ada2f))

- **deps**: Bump the github-actions group with 2 updates
  ([#64](https://github.com/marcinpsk/netbox-data-import-plugin/pull/64),
  [`2d4337b`](https://github.com/marcinpsk/netbox-data-import-plugin/commit/2d4337b2c15953992800a036544eecf88a685b42))

- **deps**: Bump the github-actions group with 3 updates
  ([#67](https://github.com/marcinpsk/netbox-data-import-plugin/pull/67),
  [`34e88ba`](https://github.com/marcinpsk/netbox-data-import-plugin/commit/34e88ba3059def08fcd80c44bf85e095e7597906))

- **deps**: Bump the github-actions group with 3 updates
  ([#54](https://github.com/marcinpsk/netbox-data-import-plugin/pull/54),
  [`d978287`](https://github.com/marcinpsk/netbox-data-import-plugin/commit/d978287e007bb1ecd402e7efd18a4395f6a9a3dd))

- **deps**: Bump the github-actions group with 3 updates
  ([#42](https://github.com/marcinpsk/netbox-data-import-plugin/pull/42),
  [`9bbf1cc`](https://github.com/marcinpsk/netbox-data-import-plugin/commit/9bbf1cc137e9c238b7240d774cec65a5a0849d41))

- **deps**: Bump the github-actions group with 6 updates
  ([#60](https://github.com/marcinpsk/netbox-data-import-plugin/pull/60),
  [`2aab48e`](https://github.com/marcinpsk/netbox-data-import-plugin/commit/2aab48edee137fdecb47fe3abf237b09f540b04d))

- **deps-dev**: Bump build from 1.5.0 to 1.5.1
  ([#56](https://github.com/marcinpsk/netbox-data-import-plugin/pull/56),
  [`bd0e27d`](https://github.com/marcinpsk/netbox-data-import-plugin/commit/bd0e27dba8261dff41489451c5f9f7e337e3d0bc))

- **deps-dev**: Bump mkdocs-material from 9.7.6 to 9.7.7
  ([#58](https://github.com/marcinpsk/netbox-data-import-plugin/pull/58),
  [`876ba7e`](https://github.com/marcinpsk/netbox-data-import-plugin/commit/876ba7e17f3e5c315aa26653101e84e119db5db1))

- **deps-dev**: Bump pre-commit from 4.6.0 to 4.6.1
  ([#62](https://github.com/marcinpsk/netbox-data-import-plugin/pull/62),
  [`f8c0577`](https://github.com/marcinpsk/netbox-data-import-plugin/commit/f8c0577ac754462fe970801867395e8b74651165))

- **deps-dev**: Bump pytest from 9.0.3 to 9.1.0
  ([#46](https://github.com/marcinpsk/netbox-data-import-plugin/pull/46),
  [`b55d2f8`](https://github.com/marcinpsk/netbox-data-import-plugin/commit/b55d2f8115d6ff987133eb02e9f6c664db08e064))

- **deps-dev**: Bump pytest from 9.1.0 to 9.1.1
  ([#48](https://github.com/marcinpsk/netbox-data-import-plugin/pull/48),
  [`6c834c5`](https://github.com/marcinpsk/netbox-data-import-plugin/commit/6c834c53cb8917ad1568fade765aeeec6564dea2))

- **deps-dev**: Bump python-semantic-release from 10.5.3 to 10.6.1
  ([#53](https://github.com/marcinpsk/netbox-data-import-plugin/pull/53),
  [`fda8f48`](https://github.com/marcinpsk/netbox-data-import-plugin/commit/fda8f484dbf8f6683e156f793765fea2fd0cb767))

- **deps-dev**: Bump ruff from 0.15.13 to 0.15.14
  ([#40](https://github.com/marcinpsk/netbox-data-import-plugin/pull/40),
  [`36b0e63`](https://github.com/marcinpsk/netbox-data-import-plugin/commit/36b0e63e40e4768d6828d7458190c5980d483f44))

- **deps-dev**: Bump ruff from 0.15.14 to 0.15.15
  ([#43](https://github.com/marcinpsk/netbox-data-import-plugin/pull/43),
  [`c6d2e85`](https://github.com/marcinpsk/netbox-data-import-plugin/commit/c6d2e85dfafd4f8b08761677e227119ce1ef2c6c))

- **deps-dev**: Bump ruff from 0.15.15 to 0.15.16
  ([#45](https://github.com/marcinpsk/netbox-data-import-plugin/pull/45),
  [`2f5a5ba`](https://github.com/marcinpsk/netbox-data-import-plugin/commit/2f5a5ba39ac36293ec7456f1defc80d9a645aa71))

- **deps-dev**: Bump ruff from 0.15.16 to 0.15.17
  ([#47](https://github.com/marcinpsk/netbox-data-import-plugin/pull/47),
  [`d15a0d3`](https://github.com/marcinpsk/netbox-data-import-plugin/commit/d15a0d3a84acde811ae0d97d30d012ac36f15b48))

- **deps-dev**: Bump ruff from 0.15.17 to 0.15.18
  ([#50](https://github.com/marcinpsk/netbox-data-import-plugin/pull/50),
  [`91c1f7b`](https://github.com/marcinpsk/netbox-data-import-plugin/commit/91c1f7bdf24308f67f3df02f1c5666e1fd64b294))

- **deps-dev**: Bump ruff from 0.15.18 to 0.15.20
  ([#51](https://github.com/marcinpsk/netbox-data-import-plugin/pull/51),
  [`9b0668d`](https://github.com/marcinpsk/netbox-data-import-plugin/commit/9b0668d89b79ccdc5f51c525f8d94c2ce246b89f))

- **deps-dev**: Bump ruff from 0.15.20 to 0.15.21
  ([#55](https://github.com/marcinpsk/netbox-data-import-plugin/pull/55),
  [`9bc572f`](https://github.com/marcinpsk/netbox-data-import-plugin/commit/9bc572f6b36adaa2bc319deb3a82b8f393825ff3))

- **deps-dev**: Bump ruff from 0.15.21 to 0.15.22
  ([#59](https://github.com/marcinpsk/netbox-data-import-plugin/pull/59),
  [`9b7352d`](https://github.com/marcinpsk/netbox-data-import-plugin/commit/9b7352da7b883c6a96e479fed1a95a55d5a63b95))

- **deps-dev**: Bump ruff from 0.15.22 to 0.16.0
  ([#63](https://github.com/marcinpsk/netbox-data-import-plugin/pull/63),
  [`c32a6c7`](https://github.com/marcinpsk/netbox-data-import-plugin/commit/c32a6c728c0971131a8ca6dcac0a53101867ae66))

- **deps-dev**: Bump ruff from 0.16.0 to 0.16.1
  ([#66](https://github.com/marcinpsk/netbox-data-import-plugin/pull/66),
  [`3265559`](https://github.com/marcinpsk/netbox-data-import-plugin/commit/3265559e5682ef572783c0255cae9e3ef615ad44))


## v1.4.1 (2026-05-21)

### Bug Fixes

- Allow extra_json:* target_field values in profile import
  ([#39](https://github.com/marcinpsk/netbox-data-import-plugin/pull/39),
  [`fe8f7cc`](https://github.com/marcinpsk/netbox-data-import-plugin/commit/fe8f7ccbfc6d3f08331309277faba39299694d2d))

### Chores

- **deps**: Bump github/codeql-action from 4.35.4 to 4.35.5 in the github-actions group
  ([#38](https://github.com/marcinpsk/netbox-data-import-plugin/pull/38),
  [`550fe8a`](https://github.com/marcinpsk/netbox-data-import-plugin/commit/550fe8a4bf4939548e82181e7417d0ef076a4c45))


## v1.4.0 (2026-05-19)

### Features

- **sync**: Rack sync, face dependency guard, and atomic placement sync
  ([#36](https://github.com/marcinpsk/netbox-data-import-plugin/pull/36),
  [`04bc9d7`](https://github.com/marcinpsk/netbox-data-import-plugin/commit/04bc9d718a605467bd12c9c7b6de0288fcebacc8))


## v1.3.1 (2026-05-16)

### Bug Fixes

- **preview**: Show split button for all unlinked device rows regardless of name separator
  ([#35](https://github.com/marcinpsk/netbox-data-import-plugin/pull/35),
  [`e4ea3ab`](https://github.com/marcinpsk/netbox-data-import-plugin/commit/e4ea3ab8c50808ea39b98f44b9d37f0361811843))


## v1.3.0 (2026-05-16)

### Features

- **split-modal**: Field preview and conflict detection
  ([#34](https://github.com/marcinpsk/netbox-data-import-plugin/pull/34),
  [`5175f55`](https://github.com/marcinpsk/netbox-data-import-plugin/commit/5175f55bd031efad0795e83fafda202f1818df4d))


## v1.2.3 (2026-05-15)

### Bug Fixes

- **engine**: Skip name-based auto-match for duplicate device names
  ([#31](https://github.com/marcinpsk/netbox-data-import-plugin/pull/31),
  [`d25c4cc`](https://github.com/marcinpsk/netbox-data-import-plugin/commit/d25c4ccb191bcdd5ec984b988674dfe5aa10d136))


## v1.2.2 (2026-05-15)

### Bug Fixes

- Surface missing device role on preview and form validation
  ([#30](https://github.com/marcinpsk/netbox-data-import-plugin/pull/30),
  [`e60893b`](https://github.com/marcinpsk/netbox-data-import-plugin/commit/e60893b7817d1e07ae37db1df1aaf7977cabf09d))


## v1.2.1 (2026-05-15)

### Bug Fixes

- Error badge count ([#27](https://github.com/marcinpsk/netbox-data-import-plugin/pull/27),
  [`c639b14`](https://github.com/marcinpsk/netbox-data-import-plugin/commit/c639b14d92116a1ed36230f907b3ce20f83138dd))

### Chores

- **deps**: Bump github/codeql-action from 4.35.3 to 4.35.4 in the github-actions group
  ([#26](https://github.com/marcinpsk/netbox-data-import-plugin/pull/26),
  [`0eabe8e`](https://github.com/marcinpsk/netbox-data-import-plugin/commit/0eabe8e001c0b5cf00af3c877785eee4cb74ac07))


## v1.2.0 (2026-05-12)

### Chores

- **deps**: Bump actions/upload-artifact from 7.0.0 to 7.0.1 in the github-actions group
  ([#13](https://github.com/marcinpsk/netbox-data-import-plugin/pull/13),
  [`5e5a5b0`](https://github.com/marcinpsk/netbox-data-import-plugin/commit/5e5a5b0b9a133c41ca61f2153d7fd321bf8e76de))

- **deps**: Bump github/codeql-action from 4.35.2 to 4.35.3 in the github-actions group
  ([#25](https://github.com/marcinpsk/netbox-data-import-plugin/pull/25),
  [`b512a37`](https://github.com/marcinpsk/netbox-data-import-plugin/commit/b512a3719206f75c99117a9f0f49cd15a481600f))

- **deps**: Bump the github-actions group with 2 updates
  ([#15](https://github.com/marcinpsk/netbox-data-import-plugin/pull/15),
  [`37e11eb`](https://github.com/marcinpsk/netbox-data-import-plugin/commit/37e11eb400daa3bd6a29263839d171853d7e8a56))

### Features

- Per-row sync — ⚡ Sync to NetBox button on import preview
  ([#20](https://github.com/marcinpsk/netbox-data-import-plugin/pull/20),
  [`e2761eb`](https://github.com/marcinpsk/netbox-data-import-plugin/commit/e2761eba1f643e1301d3340d76451c043c53e8dc))

- Rack type mapping, dark theme fixes, unignore bug fix, modal UX
  ([#14](https://github.com/marcinpsk/netbox-data-import-plugin/pull/14),
  [`77bcb1e`](https://github.com/marcinpsk/netbox-data-import-plugin/commit/77bcb1e86eaa7ee6cac5abde5d3c232414ee85e1))


## v1.0.3 (2026-04-14)

### Bug Fixes

- Netbox import export ([#12](https://github.com/marcinpsk/netbox-data-import-plugin/pull/12),
  [`8b167aa`](https://github.com/marcinpsk/netbox-data-import-plugin/commit/8b167aa2bd29a91b53169ccda1f4bd913a332a9d))


## v1.0.2 (2026-04-09)

### Bug Fixes

- Packaging issue ([#11](https://github.com/marcinpsk/netbox-data-import-plugin/pull/11),
  [`2a3632c`](https://github.com/marcinpsk/netbox-data-import-plugin/commit/2a3632c3fc96934c76fb07855baab8c41de68296))

### Chores

- Update dependabot
  ([`ac9f4ac`](https://github.com/marcinpsk/netbox-data-import-plugin/commit/ac9f4ac1761502692be98446d5644de9cc22c654))

- Update pyproject
  ([`d5655de`](https://github.com/marcinpsk/netbox-data-import-plugin/commit/d5655de50df1109b686da31d503c9e6d157bc13f))

- **deps**: Bump github/codeql-action from 4.33.0 to 4.34.1 in the github-actions group
  ([#8](https://github.com/marcinpsk/netbox-data-import-plugin/pull/8),
  [`caa5ced`](https://github.com/marcinpsk/netbox-data-import-plugin/commit/caa5ced70d976075e617d7566328d4fc31c59866))

- **deps**: Bump pypa/gh-action-pypi-publish from 1.13.0 to 1.14.0 in the github-actions group
  ([#10](https://github.com/marcinpsk/netbox-data-import-plugin/pull/10),
  [`5db5d56`](https://github.com/marcinpsk/netbox-data-import-plugin/commit/5db5d564f77a801993c00bb50fce2a27ad43cdac))

- **deps**: Bump the github-actions group across 1 directory with 3 updates
  ([#7](https://github.com/marcinpsk/netbox-data-import-plugin/pull/7),
  [`9ce0b22`](https://github.com/marcinpsk/netbox-data-import-plugin/commit/9ce0b226883ed21a4ddf82afa35411004ab61463))

- **deps**: Bump the github-actions group with 2 updates
  ([#9](https://github.com/marcinpsk/netbox-data-import-plugin/pull/9),
  [`943480d`](https://github.com/marcinpsk/netbox-data-import-plugin/commit/943480d52f05b36a6f1b90dcea226fa9bb85d3c5))


## v1.0.1 (2026-03-04)

### Bug Fixes

- Devcontainer script hardening and refactoring
  ([#1](https://github.com/marcinpsk/netbox-data-import-plugin/pull/1),
  [`4487b76`](https://github.com/marcinpsk/netbox-data-import-plugin/commit/4487b7654be3186331f76ffb782132bbc8827ce0))

### Chores

- **deps**: Bump actions/download-artifact from 4.1.8 to 8.0.0
  ([#2](https://github.com/marcinpsk/netbox-data-import-plugin/pull/2),
  [`c89e5b7`](https://github.com/marcinpsk/netbox-data-import-plugin/commit/c89e5b73cd9bdb915e67e9e2e0618cfa2236b684))

- **deps**: Bump actions/upload-artifact from 6.0.0 to 7.0.0
  ([#3](https://github.com/marcinpsk/netbox-data-import-plugin/pull/3),
  [`b61d858`](https://github.com/marcinpsk/netbox-data-import-plugin/commit/b61d858ac05c964a46c27a30055f7906fb3ff415))

- **deps**: Bump astral-sh/setup-uv from 7.3.0 to 7.3.1
  ([#4](https://github.com/marcinpsk/netbox-data-import-plugin/pull/4),
  [`bd637a7`](https://github.com/marcinpsk/netbox-data-import-plugin/commit/bd637a7c77333b914a019456b8f28bd7f271eb5e))

- **deps**: Bump github/codeql-action from 4.32.4 to 4.32.5
  ([#5](https://github.com/marcinpsk/netbox-data-import-plugin/pull/5),
  [`a18dc71`](https://github.com/marcinpsk/netbox-data-import-plugin/commit/a18dc71ff210c220c8a9a7f91d61b2392a7c8f2c))


## v1.0.0 (2026-03-03)

- Initial Release

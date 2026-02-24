<!--
SPDX-License-Identifier: Apache-2.0
Copyright (C) 2025 Marcin Zieba <marcinpsk@gmail.com>
-->
# NetBox Data Import Plugin

A NetBox plugin for importing device inventory and rack layouts from external DCIM systems via configurable field mappings.

## Overview

This plugin allows you to:

- Define **import profiles** that configure how your source data (Excel/CSV) maps to NetBox objects
- Configure **field mappings** per column with transformation rules
- Define **class/role mappings** to translate your source system's device categories to NetBox roles
- **Preview** the import before applying changes
- **Run imports** and track results per object

Currently supports importing from Excel (`.xlsx`) files in the CANS format, with the mapping configuration stored in NetBox so it can be reused and adapted for other source formats.

## Requirements

- NetBox ≥ 4.2.0
- Python ≥ 3.12

## Installation

```bash
pip install netbox-data-import
```

Add to `PLUGINS` in your NetBox configuration:

```python
PLUGINS = ["netbox_data_import"]
```

## License

Apache-2.0 — see [LICENSE](LICENSE).

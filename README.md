<!--
SPDX-License-Identifier: Apache-2.0
Copyright (C) 2025 Marcin Zieba <marcinpsk@gmail.com>
-->
# NetBox Data Import Plugin

[![CI](https://img.shields.io/github/actions/workflow/status/marcinpsk/netbox-data-import-plugin/test.yaml?branch=main&label=tests)](https://github.com/marcinpsk/netbox-data-import-plugin/actions/workflows/test.yaml)
[![Coverage](https://img.shields.io/endpoint?url=https://marcinpsk.github.io/netbox-data-import-plugin/coverage/badge.json)](https://marcinpsk.github.io/netbox-data-import-plugin/coverage/)
[![License](https://img.shields.io/github/license/marcinpsk/netbox-data-import-plugin)](LICENSE)
[![Python](https://img.shields.io/badge/python-3.12%2B-blue)](https://www.python.org/)
[![NetBox](https://img.shields.io/badge/NetBox-%E2%89%A54.3.0-blue)](https://github.com/netbox-community/netbox)

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

# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Marcin Zieba <marcinpsk@gmail.com>
"""Column headers whose values must never reach a tracked file.

`test_contact_sync.py` reads the operator's local workbook, collects every value under these
headers, and fails if one appears in a tracked file. Each entry is the column label exactly as
the source system spells it, so a header renamed here silently stops guarding that column.
Add a spelling, never replace one: an old file still carries the old label.
"""

PRIVATE_HEADERS = frozenset(
    {
        "Asset Tag",
        "Asset_Tag",
        "Asset_Tag_Archived",
        "Audit Notes",
        "CANS",
        "City",
        "Company",
        "Contact",
        "Contact Number",
        "Country",
        "Department",
        "Department Director",
        "Description",
        "Dir. Department",
        "Equipment Notes",
        "Express Service Code",
        "Hostname",
        "IDRAC Default Password",
        "IDRAC MAC Address",
        "IP Address (IPv4)",
        "Id",
        "JIRA ID",
        "Location",
        "MAC Address",
        "Management IP Address",
        "Name",
        "Owner",
        "Primary Contact",
        "Project",
        "Purchase Order",
        "Purchase Price",
        "RACK",
        "Rack",
        "Room",
        "Serial Number",
        "Service Provider",
        "Service Tag",
        "SolarWinds ID",
        "VP Department",
        "Wave ID",
    }
)

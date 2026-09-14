# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Marcin Zieba <marcinpsk@gmail.com>
"""Configuration guidance preserves the deployment security contracts."""

from pathlib import Path

from django.test import SimpleTestCase


CONFIGURATION_GUIDE = Path(__file__).resolve().parents[2] / "docs" / "configuration.md"


class VaultTransportGuidanceTest(SimpleTestCase):
    """The Vault examples keep secrets encrypted without requiring a public CA."""

    def test_the_vault_proxy_example_uses_tls_and_supports_a_private_ca(self):
        guide = CONFIGURATION_GUIDE.read_text()
        vault_guide = guide.partition("## Inference backend credentials")[2].partition("## Native primary contacts")[0]

        self.assertIn('"address": "https://', vault_guide)
        self.assertIn('"ca_bundle":', vault_guide)
        self.assertIn("tls_cert_file", vault_guide)
        self.assertIn("tls_key_file", vault_guide)
        self.assertNotIn("tls_disable = true", vault_guide)
        self.assertIn("NBDI_VAULT_PROXY_ADDRESS=https://", vault_guide)
        self.assertIn("both `proxy` and `token`", vault_guide)
        self.assertIn("NetBox-to-Proxy connection must remain encrypted", vault_guide)
        self.assertIn("Inference Backend `api_root` is separate", vault_guide)
        self.assertIn("NetBox web and worker processes", vault_guide)

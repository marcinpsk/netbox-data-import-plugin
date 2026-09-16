# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Marcin Zieba <marcinpsk@gmail.com>
"""Configuration guidance preserves the deployment security contracts."""

from pathlib import Path

from django.test import SimpleTestCase


CONFIGURATION_GUIDE = Path(__file__).resolve().parents[2] / "docs" / "configuration.md"
VAULT_RESEARCH = Path(__file__).resolve().parents[2] / "docs" / "research" / "vault-provider-credentials.md"


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
        # A retained AppRole SecretID lets any filesystem reader mint new Vault tokens.
        self.assertNotIn("remove_secret_id_file_after_reading = false", vault_guide)
        # The documented Compose secrets are a read-only mount, so removal cannot be promised.
        self.assertIn("mount `/run/secrets` read-only", vault_guide)
        self.assertIn("logs the failed removal", vault_guide)
        self.assertIn("secret_id_response_wrapping_path", vault_guide)
        self.assertIn("NBDI_VAULT_PROXY_ADDRESS=https://", vault_guide)
        self.assertIn("both `proxy` and `token`", vault_guide)
        self.assertIn("NetBox-to-Proxy connection must remain encrypted", vault_guide)
        self.assertIn("Do not publish the listener outside the isolated container network", vault_guide)
        self.assertIn("restrict network ingress and authenticate each client", vault_guide)
        self.assertIn("Inference Backend `api_root` is separate", vault_guide)
        self.assertIn("NetBox web and worker processes", vault_guide)
        self.assertIn("small request", vault_guide)
        self.assertIn("GET {api_root}/models", vault_guide)
        self.assertIn("Model discovery is optional", vault_guide)
        self.assertIn("exact model id manually", vault_guide)

    def test_the_research_note_requires_the_same_protected_proxy_hop(self):
        """The research note informs the operator guide, so it must not offer a weaker hop."""
        # The note is hard-wrapped, so each claim is matched against its unwrapped text.
        research = " ".join(VAULT_RESEARCH.read_text().split())

        self.assertIn("The NetBox-to-Proxy hop carries the resolved inference key", research)
        self.assertIn("HTTPS listener with certificate verification", research)
        self.assertIn("Reject a plain HTTP listener and an unauthenticated one", research)
        self.assertIn("refuses a Vault address whose scheme is not `https`", research)
        self.assertIn("a Unix socket is not a deployment option here", research)
        self.assertIn("It is not transport security", research)
        self.assertNotIn("can be deployment-specific", research)

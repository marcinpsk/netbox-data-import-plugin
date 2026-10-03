# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Marcin Zieba <marcinpsk@gmail.com>
"""Configuration guidance preserves the deployment security contracts."""

import ast
import re
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from core.models import ObjectType
from dcim.models import Device, DeviceRole, DeviceType, Manufacturer, Site
from django.test import SimpleTestCase, TestCase
from extras.models import CustomField, CustomLink

from netbox_data_import.models import DeviceImportSource, ImportProfile


CONFIGURATION_GUIDE = Path(__file__).resolve().parents[2] / "docs" / "configuration.md"
VAULT_RESEARCH = Path(__file__).resolve().parents[2] / "docs" / "research" / "vault-provider-credentials.md"


def _string_expression(node, names):
    """Return the value of one ``+`` chain of string literals and documented constants."""
    if isinstance(node, ast.Constant):
        return node.value
    if isinstance(node, ast.Name):
        return names[node.id]
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        return _string_expression(node.left, names) + _string_expression(node.right, names)
    raise AssertionError(f"the documented script builds link_url in an unsupported way: {ast.dump(node)}")


def _custom_link_recipe():
    """Return the custom field name and every Link URL template the Custom Link recipe documents."""
    guide = CONFIGURATION_GUIDE.read_text(encoding="utf-8")
    recipe = guide.partition("## Linking to the source system")[2].partition("## Source Adapter")[0]
    link_urls = re.findall(r"^\*\*Link URL\*\*:\n\n```jinja\n(.+)\n```$", recipe, re.MULTILINE)

    script = re.search(r"^```python\n(.*?)^```$", recipe, re.MULTILINE | re.DOTALL).group(1)
    tree = ast.parse(script)
    names = {
        target.id: statement.value.value
        for statement in tree.body
        if isinstance(statement, ast.Assign) and isinstance(statement.value, ast.Constant)
        for target in statement.targets
        if isinstance(target, ast.Name)
    }
    for node in ast.walk(tree):
        if isinstance(node, ast.Dict):
            for key, value in zip(node.keys, node.values, strict=True):
                if isinstance(key, ast.Constant) and key.value == "link_url":
                    link_urls.append(_string_expression(value, names))
    return {"custom_field": names["CUSTOM_FIELD"], "link_urls": link_urls}


class VaultTransportGuidanceTest(SimpleTestCase):
    """The Vault examples keep secrets encrypted without requiring a public CA."""

    def test_the_vault_proxy_example_uses_tls_and_supports_a_private_ca(self):
        guide = CONFIGURATION_GUIDE.read_text(encoding="utf-8")
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
        research = " ".join(VAULT_RESEARCH.read_text(encoding="utf-8").split())

        self.assertIn("The NetBox-to-Proxy hop carries the resolved inference key", research)
        self.assertIn("HTTPS listener with certificate verification", research)
        self.assertIn("Reject a plain HTTP listener and an unauthenticated one", research)
        self.assertIn("refuses a Vault address whose scheme is not `https`", research)
        self.assertIn("a Unix socket is not a deployment option here", research)
        self.assertIn("It is not transport security", research)
        self.assertNotIn("can be deployment-specific", research)


class CustomLinkRecipeTest(TestCase):
    """Every documented Custom Link keeps a delimiter-bearing source ID in one query value."""

    SOURCE_ID = "A&B#C D/E+F%G=H?I%26J"

    def setUp(self):
        self.profile = ImportProfile.objects.create(name="Custom Link Doc Profile")
        site = Site.objects.create(name="Custom Link Doc Site", slug="custom-link-doc-site")
        manufacturer = Manufacturer.objects.create(name="Custom Link Doc Make", slug="custom-link-doc-make")
        device_type = DeviceType.objects.create(
            manufacturer=manufacturer,
            model="Custom Link Doc Model",
            slug="custom-link-doc-model",
            u_height=1,
        )
        role = DeviceRole.objects.create(name="Custom Link Doc Role", slug="custom-link-doc-role")
        self.recipe = _custom_link_recipe()
        custom_field = CustomField.objects.create(name=self.recipe["custom_field"], type="text")
        custom_field.object_types.set([ObjectType.objects.get_for_model(Device)])
        self.device = Device.objects.create(
            name="custom-link-doc-device",
            site=site,
            device_type=device_type,
            role=role,
            custom_field_data={self.recipe["custom_field"]: self.SOURCE_ID},
        )
        DeviceImportSource.objects.create(device=self.device, profile=self.profile, source_id=self.SOURCE_ID)

    def _rendered_link(self, link_url):
        """Render one documented Link URL the way NetBox renders a Custom Link."""
        link = CustomLink(name="Locate asset", link_text="Locate asset", link_url=link_url)
        return urlparse(link.render({"object": self.device})["link"])

    def test_every_documented_link_url_survives_a_delimiter_bearing_source_id(self):
        for link_url in self.recipe["link_urls"]:
            with self.subTest(link_url=link_url):
                rendered = self._rendered_link(link_url)
                self.assertEqual(parse_qs(rendered.query), {"q": [self.SOURCE_ID]})
                self.assertEqual(rendered.fragment, "")

    def test_the_recipe_documents_both_link_variants_and_the_script(self):
        link_urls = self.recipe["link_urls"]
        self.assertEqual(len(link_urls), 3, link_urls)
        custom_field_source = f"object.cf.{self.recipe['custom_field']}"
        self.assertEqual(
            sorted(re.search(r"\{\{ (\S+)", link_url).group(1) for link_url in link_urls),
            sorted([custom_field_source, custom_field_source, "object.data_import_source.source_id"]),
        )

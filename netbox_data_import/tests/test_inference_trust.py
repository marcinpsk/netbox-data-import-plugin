# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Marcin Zieba <marcinpsk@gmail.com>
"""The `api_root` trust boundary: allowlist, scheme rules, resolution and redirects (specification 8.3)."""

from django.test import SimpleTestCase

from netbox_data_import.inference_trust import (
    InvalidInferenceConfiguration,
    assert_resolved_address_allowed,
    resolve_addresses,
    validate_api_root,
    validate_origin,
)

ALLOWLIST = ("https://backend.example.invalid:443",)
LOCAL_ALLOWLIST = ("http://127.0.0.1:11434",)


class OriginFormatTest(SimpleTestCase):
    """An origin names a scheme, a host and a port, and nothing else."""

    def rejects(self, entry):
        """Return the message raised for one origin string."""
        with self.assertRaises(InvalidInferenceConfiguration) as caught:
            validate_origin(entry, setting="allowlist")
        return str(caught.exception)

    def test_an_exact_origin_round_trips(self):
        self.assertEqual(validate_origin("https://a.example.invalid:443", setting="x"), "https://a.example.invalid:443")

    def test_the_origin_is_normalized_to_lower_case(self):
        self.assertEqual(validate_origin("HTTPS://A.Example.Invalid:443", setting="x"), "https://a.example.invalid:443")

    def test_a_trailing_slash_is_rejected(self):
        self.assertIn("path", self.rejects("https://a.example.invalid:443/"))

    def test_a_query_string_is_rejected(self):
        self.assertIn("path", self.rejects("https://a.example.invalid:443?x=1"))

    def test_a_userinfo_component_is_rejected(self):
        self.assertIn("credential", self.rejects("https://user:pw@a.example.invalid:443"))

    def test_an_unsupported_scheme_is_rejected(self):
        self.assertIn("scheme", self.rejects("ftp://a.example.invalid:21"))

    def test_an_unusable_port_is_rejected_as_configuration(self):
        """urlsplit defers the port cast, so reading it has to fail as a typed configuration error."""
        for entry in ("https://host:abc", "https://host:99999", "https://host:-1", "https://host:0"):
            with self.subTest(entry=entry):
                with self.assertRaises(InvalidInferenceConfiguration) as caught:
                    validate_origin(entry, setting="allowlist")

                self.assertIn("port", str(caught.exception))

    def test_a_non_string_entry_is_rejected(self):
        self.assertIn("string", self.rejects(443))


class MalformedUrlSyntaxTest(SimpleTestCase):
    """`urlsplit` raises a bare ValueError, which callers catching this module's type would miss."""

    MALFORMED = ("https://[bad", "https://[::1", "https://[]:443", "http://[oops]:80")

    def test_every_entry_point_raises_the_configuration_error(self):
        for value in self.MALFORMED:
            with self.subTest(value=value):
                with self.assertRaises(InvalidInferenceConfiguration):
                    validate_origin(value, setting="allowlist")
                with self.assertRaises(InvalidInferenceConfiguration):
                    validate_api_root(value, ALLOWLIST)

    def test_the_message_names_the_setting_and_the_value(self):
        with self.assertRaises(InvalidInferenceConfiguration) as caught:
            validate_origin("https://[bad", setting="allowlist")

        self.assertIn("allowlist", str(caught.exception))


class ApiRootAllowlistTest(SimpleTestCase):
    """`api_root` names a destination NetBox itself calls, so the allowlist governs it."""

    def test_an_allowlisted_root_is_accepted(self):
        validate_api_root("https://backend.example.invalid:443", allowlist=ALLOWLIST, authentication="bearer")

    def test_a_path_under_an_allowlisted_origin_is_accepted(self):
        """The client appends /chat/completions, so a root may carry a base path."""
        validate_api_root("https://backend.example.invalid:443/v1", allowlist=ALLOWLIST, authentication="bearer")

    def test_a_root_outside_the_allowlist_is_rejected(self):
        with self.assertRaises(InvalidInferenceConfiguration) as caught:
            validate_api_root("https://elsewhere.example.invalid:443", allowlist=ALLOWLIST, authentication="bearer")

        self.assertIn("allowlist", str(caught.exception))

    def test_a_trailing_slash_is_rejected(self):
        """Section 8.2 requires an exact API root without a trailing slash."""
        with self.assertRaises(InvalidInferenceConfiguration) as caught:
            validate_api_root("https://backend.example.invalid:443/v1/", allowlist=ALLOWLIST, authentication="bearer")

        self.assertIn("trailing slash", str(caught.exception))

    def test_a_query_or_fragment_is_rejected(self):
        """The client appends /chat/completions as text, so a query would swallow the suffix."""
        for value in (
            "https://backend.example.invalid:443/v1?key=x",
            "https://backend.example.invalid:443/v1#section",
            "https://backend.example.invalid:443?key=x",
        ):
            with self.subTest(api_root=value):
                with self.assertRaises(InvalidInferenceConfiguration) as caught:
                    validate_api_root(value, allowlist=ALLOWLIST, authentication="bearer")

                self.assertIn("query", str(caught.exception))

    def test_an_empty_allowlist_accepts_nothing(self):
        with self.assertRaises(InvalidInferenceConfiguration):
            validate_api_root("https://backend.example.invalid:443", allowlist=(), authentication="bearer")

    def test_bearer_authentication_requires_https(self):
        """An allowlisted http origin is still refused when it is not a local endpoint."""
        allowlist = ("http://backend.example.invalid:80",)

        with self.assertRaises(InvalidInferenceConfiguration) as caught:
            validate_api_root("http://backend.example.invalid:80", allowlist=allowlist, authentication="bearer")

        self.assertIn("https", str(caught.exception))

    def test_http_is_allowed_for_an_approved_local_endpoint(self):
        """An allowlist entry that literally names a local address is the approval."""
        validate_api_root("http://127.0.0.1:11434", allowlist=LOCAL_ALLOWLIST, authentication="bearer")


class ResolvedAddressTest(SimpleTestCase):
    """Resolution is rechecked, so an allowlisted name cannot point at an internal address."""

    def test_a_public_address_is_accepted(self):
        assert_resolved_address_allowed(
            "https://backend.example.invalid:443", allowlist=ALLOWLIST, addresses=("93.184.216.34",)
        )

    def test_a_private_address_is_rejected(self):
        with self.assertRaises(InvalidInferenceConfiguration) as caught:
            assert_resolved_address_allowed(
                "https://backend.example.invalid:443", allowlist=ALLOWLIST, addresses=("10.0.0.5",)
            )

        self.assertIn("private", str(caught.exception))

    def test_a_loopback_address_is_rejected(self):
        with self.assertRaises(InvalidInferenceConfiguration):
            assert_resolved_address_allowed(
                "https://backend.example.invalid:443", allowlist=ALLOWLIST, addresses=("127.0.0.1",)
            )

    def test_a_link_local_address_is_rejected(self):
        with self.assertRaises(InvalidInferenceConfiguration):
            assert_resolved_address_allowed(
                "https://backend.example.invalid:443", allowlist=ALLOWLIST, addresses=("169.254.1.1",)
            )

    def test_the_cloud_metadata_address_is_rejected(self):
        with self.assertRaises(InvalidInferenceConfiguration) as caught:
            assert_resolved_address_allowed(
                "https://backend.example.invalid:443", allowlist=ALLOWLIST, addresses=("169.254.169.254",)
            )

        self.assertIn("metadata", str(caught.exception))

    def test_an_ipv6_unique_local_address_is_rejected(self):
        with self.assertRaises(InvalidInferenceConfiguration):
            assert_resolved_address_allowed(
                "https://backend.example.invalid:443", allowlist=ALLOWLIST, addresses=("fd00::1",)
            )

    def test_one_disallowed_address_among_several_rejects_the_whole_name(self):
        """A name that answers with both a public and a private address is a rebinding risk."""
        with self.assertRaises(InvalidInferenceConfiguration):
            assert_resolved_address_allowed(
                "https://backend.example.invalid:443", allowlist=ALLOWLIST, addresses=("93.184.216.34", "10.0.0.5")
            )

    def test_a_local_address_is_accepted_for_an_approved_local_endpoint(self):
        assert_resolved_address_allowed("http://127.0.0.1:11434", allowlist=LOCAL_ALLOWLIST, addresses=("127.0.0.1",))

    def test_resolving_no_address_is_rejected(self):
        with self.assertRaises(InvalidInferenceConfiguration):
            assert_resolved_address_allowed("https://backend.example.invalid:443", allowlist=ALLOWLIST, addresses=())

    def test_a_name_that_does_not_resolve_fails_as_configuration(self):
        """DNS is the one external boundary here, so its OSError has to reach a typed failure."""
        import socket

        def refuse(*args, **kwargs):
            raise socket.gaierror("Name or service not known")

        original = socket.getaddrinfo
        socket.getaddrinfo = refuse
        try:
            with self.assertRaises(InvalidInferenceConfiguration) as caught:
                resolve_addresses("https://backend.example.invalid:443")
        finally:
            socket.getaddrinfo = original

        self.assertIn("backend.example.invalid", str(caught.exception))

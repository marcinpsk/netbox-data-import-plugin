# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Marcin Zieba <marcinpsk@gmail.com>
"""The `api_root` trust boundary: allowlist, scheme rules, resolution and redirects (specification 8.3)."""

import io
import json
import os
import pathlib
import signal
import subprocess
import sys
import time

from tempfile import TemporaryDirectory
from unittest.mock import patch
from urllib.parse import urlsplit

from django.test import SimpleTestCase

from netbox_data_import.inference_transport import WallClockDeadline, WallClockDeadlineExceeded
from netbox_data_import import _dns_worker, inference_trust
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

    def test_api_roots_with_surrounding_spaces_are_trimmed(self):
        root = "https://backend.example.invalid:443/v1"
        for value in (root + " ", " " + root):
            with self.subTest(value=value):
                self.assertEqual(validate_api_root(value, allowlist=ALLOWLIST), root)

    def test_a_root_outside_the_allowlist_is_rejected(self):
        with self.assertRaises(InvalidInferenceConfiguration) as caught:
            validate_api_root("https://elsewhere.example.invalid:443", allowlist=ALLOWLIST, authentication="bearer")

        self.assertIn("allowlist", str(caught.exception))

    def test_a_trailing_slash_is_rejected(self):
        """Section 8.2 requires an exact API root without a trailing slash."""
        with self.assertRaises(InvalidInferenceConfiguration) as caught:
            validate_api_root("https://backend.example.invalid:443/v1/", allowlist=ALLOWLIST, authentication="bearer")

        self.assertIn("trailing slash", str(caught.exception))

    def test_a_bare_query_or_fragment_delimiter_is_rejected(self):
        """`urlsplit` reports an empty query for a trailing `?`, so the truthiness check misses it."""
        for value in (
            "https://backend.example.invalid:443/v1?",
            "https://backend.example.invalid:443/v1#",
            " https://backend.example.invalid:443/v1? ",
            " https://backend.example.invalid:443/v1# ",
        ):
            with self.subTest(value=value), self.assertRaises(InvalidInferenceConfiguration) as caught:
                validate_api_root(value, allowlist=ALLOWLIST, authentication="bearer")

            self.assertIn("query or fragment", str(caught.exception))

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

    def test_http_is_allowed_for_an_approved_ipv6_local_endpoint(self):
        """An IPv6 loopback is as local as 127.0.0.1, so the same bearer approval applies."""
        validate_api_root("http://[::1]:11434", allowlist=("http://[::1]:11434",), authentication="bearer")


class OriginReparseTest(SimpleTestCase):
    """An origin is re-parsed by `is_local_endpoint` and by every allowlist comparison."""

    def test_every_origin_re_parses_to_the_host_and_port_it_names(self):
        """An origin that does not survive a round trip silently changes the host later readers see."""
        for entry, host, port in (
            ("https://a.example.invalid:443", "a.example.invalid", 443),
            ("http://127.0.0.1:11434", "127.0.0.1", 11434),
            ("http://[::1]:11434", "::1", 11434),
            ("http://[fd00::1]:8080", "fd00::1", 8080),
        ):
            with self.subTest(entry=entry):
                parts = urlsplit(validate_origin(entry, setting="x"))
                self.assertEqual(parts.hostname, host)
                self.assertEqual(parts.port, port)


class DnsWorkerScriptTest(SimpleTestCase):
    """Run the worker as the transport runs it: the real script, in its own interpreter."""

    @staticmethod
    def _run_script(request):
        return subprocess.run(
            [sys.executable, *inference_trust.DNS_WORKER_COMMAND],
            input=request,
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )

    def test_a_resolvable_host_answers_with_the_same_addresses_as_the_in_process_path(self):
        answer = self._run_script(json.dumps(("localhost", 80, 30)))

        self.assertEqual(answer.returncode, 0, answer.stderr)
        self.assertEqual(json.loads(answer.stdout), list(resolve_addresses("http://localhost:80")))

    def test_an_unusable_request_exits_nonzero_and_writes_nothing(self):
        answer = self._run_script(json.dumps(["only-one-value"]))

        self.assertEqual(answer.returncode, 1)
        self.assertEqual(answer.stdout, "")


class DnsWorkerTest(SimpleTestCase):
    """Call main() in this process, which is the only way coverage can measure a child-only module."""

    @staticmethod
    def _run_worker(request):
        stdin, stdout = io.StringIO(request), io.StringIO()
        with patch.object(sys, "stdin", stdin), patch.object(sys, "stdout", stdout):
            try:
                status = _dns_worker.main()
                armed = signal.getitimer(signal.ITIMER_REAL)[0]
            finally:
                # main() leaves the alarm running for a child that exits, so this process disarms it.
                signal.setitimer(signal.ITIMER_REAL, 0)
        return status, stdout.getvalue(), armed

    def test_the_worker_arms_its_own_alarm_before_it_resolves(self):
        status, _output, armed = self._run_worker(json.dumps(("localhost", 80, 30)))

        self.assertEqual(status, 0)
        self.assertGreater(armed, 0)
        self.assertLessEqual(armed, 30)

    def test_an_unusable_request_answers_with_nothing(self):
        status, output, _armed = self._run_worker(json.dumps(["only-one-value"]))

        self.assertEqual(status, 1)
        self.assertEqual(output, "")


class ResolvedAddressTest(SimpleTestCase):
    """Resolution is rechecked, so an allowlisted name cannot point at an internal address."""

    def test_deadline_resolution_matches_in_process_order_and_deduplication(self):
        for api_root in ("http://127.0.0.1:80", "http://localhost:80"):
            with self.subTest(api_root=api_root):
                in_process = resolve_addresses(api_root)
                bounded = resolve_addresses(api_root, deadline=WallClockDeadline.after(5))

                self.assertEqual(bounded, in_process)

        self.assertEqual(resolve_addresses("http://127.0.0.1:80"), ("127.0.0.1",))

    def test_a_helper_the_worker_started_dies_with_it_at_the_deadline(self):
        worker_probe = (
            "import json,pathlib,subprocess,sys,time; "
            "json.load(sys.stdin); "
            "helper=subprocess.Popen([sys.executable,'-I','-S','-c','import time; time.sleep(60)']); "
            "pathlib.Path(sys.argv[1]).write_text(str(helper.pid), encoding='utf-8'); "
            "time.sleep(60)"
        )
        with TemporaryDirectory() as temporary:
            pid_file = pathlib.Path(temporary) / "helper"
            command = ("-I", "-S", "-c", worker_probe, str(pid_file))
            with patch.object(inference_trust, "DNS_WORKER_COMMAND", command):
                with self.assertRaises(WallClockDeadlineExceeded):
                    resolve_addresses("http://127.0.0.1:80", deadline=WallClockDeadline.after(1))

            self.assertTrue(pid_file.exists(), "the worker never started its helper")
            helper = int(pid_file.read_text(encoding="utf-8"))
            try:
                for _ in range(150):
                    if not pathlib.Path(f"/proc/{helper}").exists():
                        break
                    time.sleep(0.02)

                self.assertFalse(
                    pathlib.Path(f"/proc/{helper}").exists(),
                    "the helper outlived the worker's deadline",
                )
            finally:
                if pathlib.Path(f"/proc/{helper}").exists():
                    os.kill(helper, signal.SIGKILL)

    def test_resolution_without_a_deadline_stays_in_process(self):
        with TemporaryDirectory() as temporary:
            marker = pathlib.Path(temporary) / "spawned"
            command = ("-I", "-S", "-c", "import pathlib,sys; pathlib.Path(sys.argv[1]).touch()", str(marker))
            with patch.object(inference_trust, "DNS_WORKER_COMMAND", command):
                addresses = resolve_addresses("http://127.0.0.1:80")

            self.assertEqual(addresses, ("127.0.0.1",))
            self.assertFalse(marker.exists())

    def test_deadline_resolution_keeps_the_unresolvable_host_message(self):
        with self.assertRaises(InvalidInferenceConfiguration) as caught:
            resolve_addresses(
                "https://backend.example.invalid:443",
                deadline=WallClockDeadline.after(5),
            )

        self.assertEqual(
            str(caught.exception),
            "'api_root' host 'backend.example.invalid' could not be resolved.",
        )

    def test_garbage_worker_output_is_a_typed_failure(self):
        command = ("-I", "-S", "-c", "print('not json')")
        with patch.object(inference_trust, "DNS_WORKER_COMMAND", command):
            with self.assertRaises(InvalidInferenceConfiguration) as caught:
                resolve_addresses("http://127.0.0.1:80", deadline=WallClockDeadline.after(5))

        self.assertIn("could not be resolved", str(caught.exception))

    def test_well_formed_output_that_is_not_addresses_is_a_typed_failure(self):
        for payload in ('{"error": "nope"}', "[42]", '["not-an-address"]'):
            with self.subTest(payload=payload):
                command = ("-I", "-S", "-c", f"print({payload!r})")
                with patch.object(inference_trust, "DNS_WORKER_COMMAND", command):
                    with self.assertRaises(InvalidInferenceConfiguration) as caught:
                        resolve_addresses("http://127.0.0.1:80", deadline=WallClockDeadline.after(5))

                self.assertIn("could not be resolved", str(caught.exception))

    def test_zero_exit_without_usable_output_is_a_typed_failure(self):
        command = ("-I", "-S", "-c", "pass")
        with patch.object(inference_trust, "DNS_WORKER_COMMAND", command):
            with self.assertRaises(InvalidInferenceConfiguration) as caught:
                resolve_addresses("http://127.0.0.1:80", deadline=WallClockDeadline.after(5))

        self.assertIn("could not be resolved", str(caught.exception))

    def test_missing_python_executable_is_a_typed_failure(self):
        with patch.object(inference_trust.sys, "executable", ""):
            with self.assertRaises(InvalidInferenceConfiguration) as caught:
                resolve_addresses("http://127.0.0.1:80", deadline=WallClockDeadline.after(5))

        self.assertIn("Python executable is unavailable", str(caught.exception))

    def test_dns_worker_environment_does_not_contain_the_vault_token(self):
        probe = (
            "import json,os,sys; json.load(sys.stdin); "
            "json.dump(['not-an-address'] if 'VAULT_TOKEN' in os.environ else ['127.0.0.1'], sys.stdout)"
        )
        command = ("-I", "-S", "-c", probe)
        with patch.dict(os.environ, {"VAULT_TOKEN": "secret"}):
            with patch.object(inference_trust, "DNS_WORKER_COMMAND", command):
                addresses = resolve_addresses("http://127.0.0.1:80", deadline=WallClockDeadline.after(5))

        self.assertEqual(addresses, ("127.0.0.1",))

    def test_dns_worker_expires_after_its_parent_exits(self):
        worker = inference_trust.DNS_WORKER_COMMAND[-1]
        child_probe = (
            "import os,pathlib,runpy,socket,sys,time; "
            "pathlib.Path(sys.argv[2]).write_text(str(os.getpid())); "
            "socket.getaddrinfo=lambda *args,**kwargs: time.sleep(4); "
            "runpy.run_path(sys.argv[1],run_name='__main__')"
        )
        parent_probe = (
            "import json,os,subprocess,sys; "
            "child=subprocess.Popen([sys.executable,'-I','-S','-c',sys.argv[1],sys.argv[2],sys.argv[3]],"
            "stdin=subprocess.PIPE,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,text=True); "
            "child.stdin.write(json.dumps(('localhost',80,1.0))); child.stdin.close(); os._exit(0)"
        )
        with TemporaryDirectory() as temporary:
            pid_file = pathlib.Path(temporary) / "pid"
            parent = subprocess.run(
                [sys.executable, "-I", "-S", "-c", parent_probe, child_probe, worker, str(pid_file)],
                check=False,
                timeout=5,
            )
            self.assertEqual(parent.returncode, 0)
            for _ in range(100):
                if pid_file.exists():
                    break
                time.sleep(0.02)
            self.assertTrue(pid_file.exists(), "the child did not enter the blocking resolver")
            pid = int(pid_file.read_text(encoding="utf-8"))
            # The alarm may already have reaped the worker, so only its absence is asserted.
            for _ in range(150):
                if not pathlib.Path(f"/proc/{pid}").exists():
                    break
                time.sleep(0.02)

            try:
                self.assertFalse(
                    pathlib.Path(f"/proc/{pid}").exists(),
                    "the orphaned DNS worker survived its alarm",
                )
            finally:
                if pathlib.Path(f"/proc/{pid}").exists():
                    os.kill(pid, signal.SIGKILL)

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
        with self.assertRaises(InvalidInferenceConfiguration) as caught:
            assert_resolved_address_allowed(
                "https://backend.example.invalid:443", allowlist=ALLOWLIST, addresses=("127.0.0.1",)
            )

        self.assertIn("loopback", str(caught.exception))

    def test_a_link_local_address_is_rejected(self):
        with self.assertRaises(InvalidInferenceConfiguration) as caught:
            assert_resolved_address_allowed(
                "https://backend.example.invalid:443", allowlist=ALLOWLIST, addresses=("169.254.1.1",)
            )

        self.assertIn("link-local", str(caught.exception))

    def test_the_cloud_metadata_address_is_rejected(self):
        with self.assertRaises(InvalidInferenceConfiguration) as caught:
            assert_resolved_address_allowed(
                "https://backend.example.invalid:443", allowlist=ALLOWLIST, addresses=("169.254.169.254",)
            )

        self.assertIn("metadata", str(caught.exception))

    def test_cloud_metadata_ipv6_spellings_are_rejected_for_an_approved_local_endpoint(self):
        for address in ("fd00:ec2::254", "fd00:ec2:0:0:0:0:0:254"):
            with self.subTest(address=address), self.assertRaisesRegex(InvalidInferenceConfiguration, "metadata"):
                assert_resolved_address_allowed(LOCAL_ALLOWLIST[0], allowlist=LOCAL_ALLOWLIST, addresses=(address,))

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

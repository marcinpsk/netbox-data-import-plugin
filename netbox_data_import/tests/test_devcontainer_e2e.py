# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2025 Marcin Zieba <marcinpsk@gmail.com>
"""Regression tests for the development-container E2E helper."""

import importlib.util
import json
import os
import sys
import urllib.request
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest import TestCase
from unittest.mock import patch


def load_e2e_module():
    """Load the standalone E2E script without requiring Playwright at import time."""
    script_path = Path(__file__).resolve().parents[2] / ".devcontainer" / "scripts" / "test-e2e.py"
    spec = importlib.util.spec_from_file_location("devcontainer_test_e2e", script_path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    playwright = ModuleType("playwright")
    sync_api = ModuleType("playwright.sync_api")
    sync_api.sync_playwright = lambda: None

    with patch.dict(sys.modules, {"playwright": playwright, "playwright.sync_api": sync_api}):
        spec.loader.exec_module(module)

    return module


class _IdentityLocator:
    def __init__(self, text="", count=0, on_click=None):
        self.text = text
        self.result_count = count
        self.on_click = on_click

    def inner_text(self, timeout=None):
        return self.text

    def count(self):
        return self.result_count

    def wait_for(self, state=None):
        return None

    def click(self):
        if self.on_click:
            self.on_click()
        return None

    def fill(self, value):
        return None

    @property
    def first(self):
        return self

    def filter(self, has_text=None):
        return self


class _IdentityPage:
    def __init__(self, base_url):
        self.base_url = base_url
        self.url = ""
        self.visited = []

    def goto(self, url):
        self.url = url
        self.visited.append(url)

    def fill(self, selector, value):
        return None

    def click(self, selector):
        if selector == 'button[type="submit"]':
            self.url = f"{self.base_url}/"

    def wait_for_url(self, url, timeout=None):
        return None

    def wait_for_load_state(self, state, timeout=None):
        return None

    def wait_for_selector(self, selector, state=None, timeout=None):
        return None

    def locator(self, selector):
        if selector == "h1.page-title":
            return _IdentityLocator("example-device-old")
        return _IdentityLocator()


class _IdentityContext:
    def __init__(self, page):
        self.page = page

    def new_page(self):
        return self.page

    def cookies(self):
        return []


class _IdentityBrowser:
    def __init__(self, page):
        self.page = page

    def new_context(self, viewport=None):
        return _IdentityContext(self.page)

    def close(self):
        return None


class _PlaywrightContext:
    def __init__(self, page):
        self.playwright = SimpleNamespace(chromium=SimpleNamespace(launch=lambda headless: _IdentityBrowser(page)))

    def __enter__(self):
        return self.playwright

    def __exit__(self, exc_type, exc_value, traceback):
        return None


class _SuccessfulPage(_IdentityPage):
    def __init__(self, base_url):
        super().__init__(base_url)
        self.created_module_ids = iter((401, 402))

    def locator(self, selector):
        if selector == "h1.page-title":
            return _IdentityLocator("example-device")
        if selector == 'button[name="_create"]':
            return _IdentityLocator(on_click=self._finish_install)
        if selector in {"text=Transceiver 0", "text=Transceiver 35", "text=TEST-MODULE"}:
            return _IdentityLocator(count=1)
        if selector.startswith('a[href*="module_bay='):
            return _IdentityLocator(count=1)
        if selector in {"text=swp0", "text=swp5"}:
            return _IdentityLocator(count=1)
        return _IdentityLocator()

    def _finish_install(self):
        module_id = next(self.created_module_ids)
        self.url = f"{self.base_url}/dcim/modules/{module_id}/"


class _APIResponse:
    def __init__(self, body=b""):
        self.body = body

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        return None

    def read(self):
        return self.body


class DevelopmentContainerE2ETest(TestCase):
    """Exercise fail-closed behavior through the E2E helper interface."""

    def test_device_identity_mismatch_stops_before_mutation(self):
        module = load_e2e_module()
        base_url = "http://127.0.0.1:9"
        page = _IdentityPage(base_url)
        settings = {
            "NETBOX_E2E_DEVICE_ID": "101",
            "NETBOX_E2E_DEVICE_NAME": "example-device",
            "NETBOX_E2E_MANUFACTURER_ID": "201",
            "NETBOX_E2E_MODULE_TYPE_MODEL": "TEST-MODULE",
            "NETBOX_E2E_BAY_ZERO_ID": "301",
            "NETBOX_E2E_BAY_FIVE_ID": "302",
        }

        with (
            patch.dict(os.environ, settings),
            patch.object(module, "sync_playwright", new=lambda: _PlaywrightContext(page)),
        ):
            passed, failed = module.run_tests(base_url)

        self.assertEqual(passed, [])
        self.assertEqual([name for name, _ in failed], ["librenms-sync page loads"])
        self.assertEqual(
            page.visited,
            [f"{base_url}/login/", f"{base_url}/dcim/devices/101/librenms-sync/"],
        )

    def test_cleanup_deletes_only_modules_created_by_this_run(self):
        module = load_e2e_module()
        base_url = "http://netbox.example.invalid"
        page = _SuccessfulPage(base_url)
        deleted_module_ids = []
        settings = {
            "NETBOX_E2E_DEVICE_ID": "101",
            "NETBOX_E2E_DEVICE_NAME": "example-device",
            "NETBOX_E2E_MANUFACTURER_ID": "201",
            "NETBOX_E2E_MODULE_TYPE_MODEL": "TEST-MODULE",
            "NETBOX_E2E_BAY_ZERO_ID": "301",
            "NETBOX_E2E_BAY_FIVE_ID": "302",
        }

        def urlopen(request, timeout=None):
            if request.get_method() == "DELETE":
                deleted_module_ids.append(int(request.full_url.rstrip("/").rsplit("/", 1)[-1]))
                return _APIResponse()
            body = json.dumps(
                {
                    "results": [{"id": 400}, {"id": 401}, {"id": 402}],
                    "next": None,
                }
            ).encode()
            return _APIResponse(body)

        with (
            patch.dict(os.environ, settings),
            patch.object(module, "sync_playwright", new=lambda: _PlaywrightContext(page)),
            patch.object(urllib.request, "urlopen", new=urlopen),
        ):
            _, failed = module.run_tests(base_url)

        self.assertEqual(failed, [])
        self.assertEqual(deleted_module_ids, [401, 402])

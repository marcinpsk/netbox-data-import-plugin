/* SPDX-License-Identifier: Apache-2.0 */
/* SPDX-FileCopyrightText: 2026 Marcin Zieba <marcinpsk@gmail.com> */

/* The collapsed topology group under the htmx release NetBox ships, so the swap after an acceptance is real. */
import {expect, test} from '@playwright/test';
import {readFileSync} from 'node:fs';
import {scriptSource} from './preview_page.js';

const WORKSPACE_URL = 'http://preview.test/plugins/data-import/trace-workspace/';
const ACCEPT_URL = 'http://preview.test/plugins/data-import/trace-workspace/proposals/accept/';
const CARD_URL = 'http://preview.test/plugins/data-import/trace-workspace/proposals/';
// NetBox bundles htmx inside netbox.js and never sets window.htmx, so the release loads in a closure here too.
const htmxSource = `(function () {\n${readFileSync('node_modules/htmx.org/dist/htmx.min.js', 'utf8')}\n}());`;
const html = body => ({contentType: 'text/html; charset=utf-8', body});

/* The workspace as the server renders it: the group closed, and the scripts inside the swapped content. */
function workspace(revision) {
  return `<div id="page-content">
    <output id="ndi-revision">${revision}</output>
    <details class="ndi-trace-topology" data-trace-topology>
      <summary><span class="h3">Topology</span> <span data-trace-topology-summary>3 segments: 3 create</span></summary>
      <p id="ndi-panels">Source evidence, current NetBox topology, proposed physical topology</p>
    </details>
    <li id="card" data-revision="${revision}" hx-get="${CARD_URL}" hx-trigger="ndi:read" hx-swap="outerHTML">card</li>
    <form method="post" action="${ACCEPT_URL}" hx-post="${ACCEPT_URL}"
          hx-target="#page-content" hx-select="#page-content" hx-swap="outerHTML" hx-push-url="true">
      <button type="submit" data-proposal-action="accept">Accept</button>
    </form>
    <script>${scriptSource('trace_topology.js')}</script>
  </div>`;
}

function document(revision) {
  return `<!doctype html><html><head><meta charset="utf-8"><script>${htmxSource}</script></head>
    <body>${workspace(revision)}</body></html>`;
}

async function open(page) {
  let loads = 0;
  await page.route(WORKSPACE_URL, route => {
    loads += 1;
    return route.fulfill(html(document(loads)));
  });
  await page.goto(WORKSPACE_URL);
  return () => loads;
}

const group = page => page.locator('[data-trace-topology]');
const isOpen = page => group(page).evaluate(element => element.open);
// The browser queues the toggle event, so a reload waits until the choice is stored.
const stored = page => page.evaluate(() => window.localStorage.getItem('ndi.traceWorkspace.topologyOpen'));

test('the topology group starts closed and a reload keeps the viewer\'s choice', async ({page}) => {
  const loads = await open(page);
  await expect(page.locator('#ndi-panels')).toBeHidden();

  await page.locator('[data-trace-topology] summary').click();
  await expect(page.locator('#ndi-panels')).toBeVisible();
  await expect.poll(() => stored(page)).toBe('true');
  await page.reload();

  expect(loads()).toBe(2);
  await expect(page.locator('#ndi-panels')).toBeVisible();

  await page.locator('[data-trace-topology] summary').click();
  await expect.poll(() => stored(page)).toBe('false');
  await page.reload();

  await expect(page.locator('#ndi-panels')).toBeHidden();
});

test('an accepted proposal swaps the workspace and the open group stays open', async ({page}) => {
  await page.route(ACCEPT_URL, route => route.fulfill(html(document(7))));
  const loads = await open(page);
  await page.locator('[data-trace-topology] summary').click();
  await expect.poll(() => stored(page)).toBe('true');

  await page.locator('[data-proposal-action="accept"]').click();

  // The swapped content is the next revision, which the server rendered with the group closed.
  await expect(page.locator('#ndi-revision')).toHaveText('7');
  await expect(page.locator('#ndi-panels')).toBeVisible();
  expect(await isOpen(page)).toBe(true);
  expect(loads()).toBe(1);
});

test('without browser storage the page renders the closed group and still opens it', async ({page}) => {
  await page.addInitScript(() => {
    Object.defineProperty(window, 'localStorage', {get() { throw new Error('storage is blocked'); }});
  });
  const errors = [];
  page.on('pageerror', error => errors.push(error.message));
  await open(page);

  await expect(page.locator('#ndi-panels')).toBeHidden();
  await page.locator('[data-trace-topology] summary').click();

  await expect(page.locator('#ndi-panels')).toBeVisible();
  expect(errors).toEqual([]);
});

test('a card refresh keeps the group the viewer opened when storage refuses writes', async ({page}) => {
  await page.addInitScript(() => {
    window.localStorage.setItem('ndi.traceWorkspace.topologyOpen', 'false');
    Storage.prototype.setItem = () => { throw new Error('quota exceeded'); };
  });
  await page.route(CARD_URL, route => route.fulfill(html(
    `<li id="card" data-revision="refreshed" hx-get="${CARD_URL}" hx-trigger="ndi:read" hx-swap="outerHTML">card</li>`)));
  await open(page);
  await page.locator('[data-trace-topology] summary').click();
  await expect(page.locator('#ndi-panels')).toBeVisible();

  // htmx fires htmx:load as the swapped card settles, a moment after the swap itself.
  const settled = page.evaluate(() => new Promise(done => document.addEventListener('htmx:load', done, {once: true})));
  await page.locator('#card').evaluate(card => card.dispatchEvent(new CustomEvent('ndi:read')));
  await settled;

  await expect(page.locator('#card')).toHaveAttribute('data-revision', 'refreshed');
  expect(await isOpen(page)).toBe(true);
});

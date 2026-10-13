/* SPDX-License-Identifier: Apache-2.0 */
/* SPDX-FileCopyrightText: 2026 Marcin Zieba <marcinpsk@gmail.com> */

/* The Source Trace list filter under real clicks, reloads, and the htmx swap that follows an action. */
import {expect, test} from '@playwright/test';
import {readFileSync} from 'node:fs';
import {scriptSource} from './preview_page.js';

const WORKSPACE_URL = 'http://preview.test/plugins/data-import/trace-workspace/';
const ACTION_URL = 'http://preview.test/plugins/data-import/trace-workspace/sync/';
// NetBox bundles htmx inside netbox.js and never sets window.htmx, so the release loads in a closure here too.
const htmxSource = `(function () {\n${readFileSync('node_modules/htmx.org/dist/htmx.min.js', 'utf8')}\n}());`;
const bootstrapCss = readFileSync('node_modules/bootstrap/dist/css/bootstrap.min.css', 'utf8');
const html = body => ({contentType: 'text/html; charset=utf-8', body});

const TRACES = [['T1', 'actionable'], ['T2', 'blocked'], ['T3', 'blocked'], ['T4', 'invalid'], ['T5', 'no-op']];
const COUNTS = [['actionable', 1], ['blocked', 2], ['invalid', 1], ['no-op', 1]];

/* The workspace as the server renders it: the list unfiltered, and the scripts inside the swapped content.
 * The list items carry no id, as on the real page, because htmx keeps the old attributes of an id until it settles. */
function workspace(revision, active, withScript = true) {
  return `<div id="page-content">
    <output id="ndi-revision">${revision}</output>
    <div class="card" data-trace-list-filter>
      <div class="card-header">Source Traces</div>
      <div class="card-body py-2 d-flex flex-wrap gap-1" role="group" aria-label="Filter traces by disposition">
        ${COUNTS.map(([disposition, count]) => `<button type="button" class="btn btn-sm btn-outline-secondary"
          data-trace-filter="${disposition}" aria-pressed="false">${disposition} ${count}</button>`).join('')}
      </div>
      <div class="list-group list-group-flush">
        ${TRACES.map(([name, disposition]) => `<a href="?trace=${name}"
          class="list-group-item list-group-item-action${name === active ? ' active' : ''}"
          data-disposition="${disposition}">${name}</a>`).join('')}
        <div class="list-group-item text-secondary" data-trace-filter-empty hidden>No trace matches the filter.</div>
      </div>
    </div>
    <form method="post" action="${ACTION_URL}" hx-post="${ACTION_URL}"
          hx-target="#page-content" hx-select="#page-content" hx-swap="outerHTML">
      <button type="submit" id="ndi-action">Sync</button>
    </form>
    ${withScript ? `<script>${scriptSource('trace_list_filter.js')}</script>` : ''}
  </div>`;
}

function document(revision, active = 'T1', withScript = true) {
  return `<!doctype html><html><head><meta charset="utf-8"><style>${bootstrapCss}</style>
    <script>${htmxSource}</script></head><body>${workspace(revision, active, withScript)}</body></html>`;
}

async function open(page, active = 'T1') {
  let loads = 0;
  await page.route(WORKSPACE_URL, route => {
    loads += 1;
    return route.fulfill(html(document(loads, active)));
  });
  await page.goto(WORKSPACE_URL);
  return () => loads;
}

const pill = (page, disposition) => page.locator(`[data-trace-filter="${disposition}"]`);
const visible = page => page.locator('[data-disposition]:visible').evaluateAll(items => items.map(item => item.textContent));

test('clicks filter the list to the union of the pressed pills and a reload keeps them', async ({page}) => {
  const loads = await open(page);
  expect(await visible(page)).toEqual(['T1', 'T2', 'T3', 'T4', 'T5']);

  await pill(page, 'blocked').click();
  await expect(pill(page, 'blocked')).toHaveAttribute('aria-pressed', 'true');
  expect(await visible(page)).toEqual(['T1', 'T2', 'T3']);
  await pill(page, 'no-op').click();
  expect(await visible(page)).toEqual(['T1', 'T2', 'T3', 'T5']);

  await page.reload();

  expect(loads()).toBe(2);
  await expect(page.locator('#ndi-revision')).toHaveText('2');
  await expect(pill(page, 'blocked')).toHaveAttribute('aria-pressed', 'true');
  await expect(pill(page, 'no-op')).toHaveAttribute('aria-pressed', 'true');
  await expect(pill(page, 'invalid')).toHaveAttribute('aria-pressed', 'false');
  expect(await visible(page)).toEqual(['T1', 'T2', 'T3', 'T5']);

  await pill(page, 'blocked').click();
  await pill(page, 'no-op').click();
  expect(await visible(page)).toEqual(['T1', 'T2', 'T3', 'T4', 'T5']);
});

test('the selected trace stays in the list when its disposition is filtered out', async ({page}) => {
  await open(page, 'T4');

  await pill(page, 'blocked').click();

  expect(await visible(page)).toEqual(['T2', 'T3', 'T4']);
  await expect(page.locator('[data-trace-filter-empty]')).toBeHidden();
});

test('a keyboard user toggles a pill with the space key', async ({page}) => {
  await open(page);

  await pill(page, 'invalid').focus();
  await page.keyboard.press('Space');

  await expect(pill(page, 'invalid')).toHaveAttribute('aria-pressed', 'true');
  expect(await visible(page)).toEqual(['T1', 'T4']);
});

// Without the script in the response, only the document listeners can filter the new list in time.
for (const withScript of [true, false]) {
  test(`the first frame after an action swap is filtered, ${withScript ? 'with' : 'without'} the script`, async ({page}) => {
    await page.route(ACTION_URL, route => route.fulfill(html(document(9, 'T3', withScript))));
    await open(page);
    await pill(page, 'invalid').click();
    // Record what the browser would paint first after htmx replaces the page content.
    await page.evaluate(() => {
      new MutationObserver((records, observer) => {
        if (!records.some(record => [...record.addedNodes].some(node => node.id === 'page-content'))) return;
        observer.disconnect();
        requestAnimationFrame(() => {
          window.ndiFirstFrame = [...document.querySelectorAll('[data-disposition]')]
            .filter(item => item.checkVisibility()).map(item => item.textContent);
        });
      }).observe(document.body, {childList: true});
    });

    await page.locator('#ndi-action').click();

    await expect(page.locator('#ndi-revision')).toHaveText('9');
    await expect.poll(() => page.evaluate(() => window.ndiFirstFrame)).toEqual(['T3', 'T4']);
    await expect(pill(page, 'invalid')).toHaveAttribute('aria-pressed', 'true');
    expect(await visible(page)).toEqual(['T3', 'T4']);
  });
}

test('without browser storage the list still filters and the page raises no error', async ({page}) => {
  await page.addInitScript(() => {
    Object.defineProperty(window, 'localStorage', {get() { throw new Error('storage is blocked'); }});
  });
  const errors = [];
  page.on('pageerror', error => errors.push(error.message));
  await open(page);

  expect(await visible(page)).toEqual(['T1', 'T2', 'T3', 'T4', 'T5']);
  await pill(page, 'blocked').click();

  expect(await visible(page)).toEqual(['T1', 'T2', 'T3']);
  expect(errors).toEqual([]);
});

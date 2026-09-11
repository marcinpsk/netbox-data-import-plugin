/* SPDX-License-Identifier: Apache-2.0 */
/* SPDX-FileCopyrightText: 2026 Marcin Zieba <marcinpsk@gmail.com> */

import {expect, test} from '@playwright/test';
import {readFileSync} from 'node:fs';
import {completed, fixture, payload} from '../js/trace_proposal_fixture.js';

const source = readFileSync('netbox_data_import/static/netbox_data_import/js/trace_proposals.js', 'utf8');
const slot = (page, name) => page.locator('[data-proposal-' + name + ']');
const action = (page, name) => page.locator('[data-proposal-action="' + name + '"]');

async function mount(page, initial = payload()) {
  await page.setContent(fixture(initial));
  await page.addScriptTag({content: `
    window.proposalReads = 0;
    var originalFetch = window.fetch;
    window.fetch = function (url, options) {
      if (!options.method) window.proposalReads += 1;
      return originalFetch(url, options);
    };
  `});
  await page.addScriptTag({content: source});
}

async function serve(page, current = completed()) {
  await page.route('**/proposal/**', route => route.fulfill({json: current}));
}

test('pending progress polls every three seconds and stops on completion', async ({page}) => {
  await page.clock.install({time: new Date("2026-09-11T08:00:00Z")});
  await page.clock.pauseAt(new Date("2026-09-11T08:00:01Z"));
  await serve(page);
  await mount(page);
  await expect(slot(page, 'progress')).toBeVisible();
  await page.clock.runFor(2999);
  expect(await page.evaluate(() => window.proposalReads)).toBe(0);
  await page.clock.runFor(1);
  await expect(slot(page, 'badge')).toHaveText('Proposal - not applied');
  await expect(slot(page, 'progress')).toBeHidden();
  await page.clock.runFor(12000);
  expect(await page.evaluate(() => window.proposalReads)).toBe(1);
});

test('a boost removes the old poll and binds the replacement card only once', async ({page}) => {
  await page.clock.install({time: new Date("2026-09-11T08:00:00Z")});
  await page.clock.pauseAt(new Date("2026-09-11T08:00:01Z"));
  await serve(page, payload());
  await mount(page);
  await page.clock.runFor(3000);
  await expect.poll(() => page.evaluate(() => window.proposalReads)).toBe(1);
  await page.evaluate(() => { document.body.innerHTML = '<p>Another page</p>'; });
  await page.clock.runFor(12000);
  expect(await page.evaluate(() => window.proposalReads)).toBe(1);
  let posts = 0;
  await page.route('**/cancel/', async route => {
    posts += 1;
    await route.fulfill({json: {ok: true}});
  });
  await page.evaluate(markup => { document.body.innerHTML = markup; }, fixture());
  await page.addScriptTag({content: source});
  await page.addScriptTag({content: source});
  await action(page, 'cancel').click();
  await expect.poll(() => posts).toBe(1);
  await expect(action(page, 'cancel')).toBeEnabled();
  expect(posts).toBe(1);
  await page.clock.runFor(3000);
  expect(await page.evaluate(() => window.proposalReads)).toBe(3);
});

test('a completed card shows the candidate, kind, explanation, backend, and full history', async ({page}) => {
  const initial = completed();
  initial.history_display = [
    {id: 7, created: 'today', status: 'Completed', outcome: 'Candidate'},
    {id: 6, created: 'yesterday', status: 'Completed', outcome: 'No match', decision: 'Rejected'},
    {id: 5, created: 'earlier', status: 'Failed', outcome: 'No outcome', failure: 'Timeout'},
  ];
  await mount(page, initial);
  await expect(slot(page, 'badge')).toHaveText('Proposal - not applied');
  await expect(slot(page, 'candidate')).toHaveText('eth0 (Interface)');
  await expect(slot(page, 'explanation')).toHaveText('The labels name the same port.');
  await expect(slot(page, 'attempts')).toHaveText('Backend attempts: 2');
  await expect(slot(page, 'metadata')).toHaveText('backend model: fixture-model');
  await expect(action(page, 'accept')).toBeEnabled();
  await expect(action(page, 'reject')).toBeEnabled();
  await expect(slot(page, 'history').locator('li')).toHaveText([
    '#7 · today · Completed · Candidate', '#6 · yesterday · Completed · No match · Rejected',
    '#5 · earlier · Failed · No outcome · Timeout',
  ]);
});

test('stale and no-match cards keep Accept disabled with the reason underneath', async ({page}) => {
  for (const [badge, reason] of [
    ['Proposal - stale, not applied', 'The eligible candidates changed.'],
    ['Proposal - not applied', 'The backend found no match.'],
  ]) {
    const initial = completed({badge});
    initial.presentation.actions[2].reason = reason;
    await mount(page, initial);
    await expect(slot(page, 'badge')).toHaveText(badge);
    await expect(action(page, 'accept')).toBeVisible();
    await expect(action(page, 'accept')).toBeDisabled();
    await expect(page.locator('[data-proposal-reason="accept"]')).toHaveText(reason);
  }
});

test('Ask AI again posts a new request after failure and shows pending progress', async ({page}) => {
  const initial = completed({badge: 'Failed', field_state: 'failed', failure: 'Backend refusal', failure_code: 'backend_refusal'});
  initial.presentation.actions[0].label = 'Ask AI again';
  let received;
  await page.route('**/request/', async route => {
    received = route.request().postData();
    await route.fulfill({json: {ok: true, proposal_id: 8}});
  });
  await serve(page, payload({badge: 'Running'}));
  await mount(page, initial);
  await expect(slot(page, 'failure')).toHaveText('Backend refusal (backend_refusal)');
  await action(page, 'request').click();
  await expect(slot(page, 'badge')).toHaveText('Running');
  await expect(slot(page, 'progress')).toBeVisible();
  expect(received).toContain('name="field_key"\r\n\r\nfield');
});

test('acceptance refreshes the accepted field and submits the existing replan form', async ({page}) => {
  let postBody;
  await page.route('**/accept/', async route => {
    postBody = route.request().postData();
    await route.fulfill({json: {ok: true, preview_state: 'recalculation_required'}});
  });
  await serve(page, completed({badge: 'Accepted', field_state: 'accepted'}));
  await mount(page, completed());
  await page.evaluate(() => {
    window.replans = 0;
    document.getElementById('traceWorkspaceReread').form.addEventListener('submit', event => {
      event.preventDefault();
      window.replans += 1;
    });
  });
  await action(page, 'accept').click();
  await expect(slot(page, 'state')).toHaveText('accepted');
  await expect.poll(() => page.evaluate(() => window.replans)).toBe(1);
  expect(postBody).toContain('name="proposal_id"\r\n\r\n7');
  expect(postBody).toContain('name="csrfmiddlewaretoken"\r\n\r\nfixture-token');
  expect(postBody).toContain('name="preview_revision"\r\n\r\nrevision-1');
});

test('rejection stays in the field and shows the refreshed decision', async ({page}) => {
  let posts = 0;
  await page.route('**/reject/', async route => { posts += 1; await route.fulfill({json: {ok: true}}); });
  await serve(page, completed({badge: 'Rejected'}));
  await mount(page, completed());
  await action(page, 'reject').click();
  await expect(slot(page, 'badge')).toHaveText('Rejected');
  expect(posts).toBe(1);
});

test('HTTP action refusals are visible in the field', async ({page}) => {
  await page.route('**/accept/', route => route.fulfill({status: 409, json: {ok: false, error: 'The proposal is stale.'}}));
  await serve(page);
  await mount(page, completed());
  await action(page, 'accept').click();
  await expect(slot(page, 'error')).toHaveText('The proposal is stale.');
  await expect(slot(page, 'error')).toBeVisible();
});

test('a saved acceptance explains why an active sync prevents the replan', async ({page}) => {
  await page.route('**/accept/', route => route.fulfill({json: {ok: true, preview_state: 'recalculation_required'}}));
  await serve(page, completed({badge: 'Accepted', field_state: 'accepted'}));
  await mount(page, completed());
  await page.evaluate(() => {
    window.replans = 0;
    const reread = document.getElementById('traceWorkspaceReread');
    reread.disabled = true;
    reread.form.addEventListener('submit', event => { event.preventDefault(); window.replans += 1; });
  });
  await action(page, 'accept').click();
  await expect(slot(page, 'error')).toHaveText('The resolution was saved. Re-read the workspace when the active sync finishes.');
  expect(await page.evaluate(() => window.replans)).toBe(0);
});

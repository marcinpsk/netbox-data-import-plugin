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

test('a completed card shows the candidate, kind, explanation, and recent history', async ({page}) => {
  const initial = completed();
  initial.history_display = [
    {id: 7, created: 'today', status: 'Completed', outcome: 'Candidate'},
    {id: 6, created: 'yesterday', status: 'Completed', outcome: 'No match', decision: 'Rejected'},
    {id: 5, created: 'earlier', status: 'Failed', outcome: 'No outcome', failure: 'Timeout'},
  ];
  initial.history_has_more = true;
  await mount(page, initial);
  await expect(slot(page, 'badge')).toHaveText('Proposal - not applied');
  await expect(slot(page, 'candidate')).toHaveText('eth0 (Interface)');
  await expect(slot(page, 'explanation')).toHaveText('The labels name the same port.');
  await expect(action(page, 'accept')).toBeEnabled();
  await expect(action(page, 'reject')).toBeEnabled();
  await expect(slot(page, 'history').locator('li')).toHaveText([
    '#7 · today · Completed · Candidate', '#6 · yesterday · Completed · No match · Rejected',
    '#5 · earlier · Failed · No outcome · Timeout',
  ]);
  await expect(slot(page, 'history-link')).toBeVisible();
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

test('a first request adds the card and history while field actions stay outside it', async ({page}) => {
  const initial = completed({field_state: 'unresolved', state_style: 'unresolved'});
  initial.proposal = null;
  initial.history_display = [];
  await serve(page, payload());
  await page.route('**/request/', route => route.fulfill({json: {ok: true, proposal_id: 7}}));
  await mount(page, initial);
  await expect(slot(page, 'display')).toHaveCount(0);
  await expect(action(page, 'accept')).toHaveCount(0);
  await expect(action(page, 'reject')).toHaveCount(0);
  await expect(slot(page, 'history')).toHaveCount(0);
  await expect(action(page, 'request')).toBeVisible();
  await expect(action(page, 'cancel')).toBeDisabled();
  await expect(page.locator('[data-proposal-reason="cancel"]')).toHaveText('There is no active proposal.');
  await action(page, 'request').click();
  await expect(slot(page, 'display')).toBeVisible();
  await expect(slot(page, 'display').locator('[data-proposal-action]')).toHaveText(['Accept', 'Reject']);
  await expect(slot(page, 'progress')).toBeVisible();
  await expect(action(page, 'cancel')).toBeEnabled();
  await expect(slot(page, 'history').locator('li')).toHaveCount(1);
});

test('the job line follows the background job and warns when it ended with no result', async ({page}) => {
  await page.clock.install({time: new Date("2026-09-11T08:00:00Z")});
  await page.clock.pauseAt(new Date("2026-09-11T08:00:01Z"));
  await mount(page);
  await expect(slot(page, 'job')).toHaveText('Background job: Pending, requested 0 minutes ago');
  // An empty slot reads as hidden to a visibility check, so the attribute is what has to be asserted.
  expect(await slot(page, 'job-note').evaluate(el => el.hidden)).toBe(true);

  // The worker was killed mid-run, so the attempt stays active and only the job says so.
  await serve(page, payload({
    job_status: 'Background job: Errored, requested 2 hours ago',
    job_note: 'The background job ended without recording a result. Cancel this proposal and ask again.',
  }));
  await page.clock.runFor(3000);

  await expect(slot(page, 'job')).toHaveText('Background job: Errored, requested 2 hours ago');
  await expect(slot(page, 'job-note')).toBeVisible();
});

test('a settled card carries no job line', async ({page}) => {
  await mount(page, completed());

  expect(await slot(page, 'job').evaluate(el => el.hidden)).toBe(true);
  expect(await slot(page, 'job-note').evaluate(el => el.hidden)).toBe(true);
});

test('a no_match that searched one page says so and offers the next', async ({page}) => {
  await mount(page, completed({
    badge: 'Proposal - not applied', candidate: '', explanation: 'No candidate in this page names the port.',
    page_status: 'Searched candidates 1-64 of 120.',
    actions: [
      {key: 'request', label: 'Ask AI: next 56', reason: '', url: '/request/'},
      {key: 'cancel', label: 'Cancel', reason: 'There is no active proposal.', url: '/cancel/'},
      {key: 'accept', label: 'Accept', reason: 'No match in candidates 1-64 of 120. Ask AI for the next 56.',
       url: '/accept/'},
      {key: 'reject', label: 'Reject', reason: '', url: '/reject/'},
    ],
  }));

  await expect(slot(page, 'page')).toHaveText('Searched candidates 1-64 of 120.');
  await expect(action(page, 'request')).toHaveText('Ask AI: next 56');
  await expect(action(page, 'accept')).toBeDisabled();
});

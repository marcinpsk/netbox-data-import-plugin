/* SPDX-License-Identifier: Apache-2.0 */
/* SPDX-FileCopyrightText: 2026 Marcin Zieba <marcinpsk@gmail.com> */

import {expect, test} from '@playwright/test';
import {readFileSync} from 'node:fs';
import {claimFields, completed, fixture, payload} from '../js/trace_proposal_fixture.js';
import {postedFields, scriptSource, servePage} from './preview_page.js';

const WORKSPACE_URL = 'http://preview.test/plugins/data-import/trace-workspace/';
const source = readFileSync('netbox_data_import/static/netbox_data_import/js/trace_proposals.js', 'utf8');
const claimSource = scriptSource('preview_claim.js');
const slot = (page, name) => page.locator('[data-proposal-' + name + ']');
const action = (page, name) => page.locator('[data-proposal-action="' + name + '"]');
const inCard = (page, key, selector) => page.locator('[data-proposal-field="' + key + '"] ' + selector);
const readCounter = `
  window.proposalReads = 0;
  var originalFetch = window.fetch;
  window.fetch = function (url, options) {
    if (!options.method) window.proposalReads += 1;
    return originalFetch(url, options);
  };
`;

async function mount(page, initial = payload()) {
  await page.setContent(fixture(initial));
  await page.addScriptTag({content: readCounter});
  await page.addScriptTag({content: claimSource});
  await page.addScriptTag({content: source});
}

/* A routed workspace, so a successful action can reload it; `cards(revision)` names what each load shows. */
async function mountRouted(page, cards) {
  return servePage(page, revision => {
    const [initial, others] = cards(revision);
    return fixture(initial, others, revision)
      + `<script>${readCounter}</script><script>${claimSource}</script><script>${source}</script>`;
  }, WORKSPACE_URL);
}

async function serve(page, current = completed()) {
  await page.route('**/proposal/**', route => route.fulfill({json: current}));
}

test('pending progress polls every three seconds and stops on completion', async ({page}) => {
  await page.clock.install({time: new Date("2026-09-11T08:00:00Z")});
  await page.clock.pauseAt(new Date("2026-09-11T08:00:01Z"));
  const asked = [];
  await page.route('**/proposal/**', route => {
    asked.push(new URL(route.request().url()).searchParams);
    return route.fulfill({json: completed()});
  });
  await mount(page);
  await expect(slot(page, 'progress')).toBeVisible();
  await page.clock.runFor(2999);
  expect(await page.evaluate(() => window.proposalReads)).toBe(0);
  await page.clock.runFor(1);
  await expect(slot(page, 'badge')).toHaveText('Proposal - not applied');
  expect([...asked[0]]).toEqual([['field_key', 'field'], ...claimFields()]);
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
  // A refusal keeps the page, so the test can count what one click posted.
  await page.route('**/cancel/', async route => {
    posts += 1;
    await route.fulfill({status: 409, json: {ok: false, error: 'The proposal moved on.'}});
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

test('Ask AI again posts the claim and the reloaded workspace shows pending progress', async ({page}) => {
  const failed = completed({badge: 'Failed', field_state: 'failed', failure: 'Backend refusal', failure_code: 'backend_refusal'});
  failed.presentation.actions[0].label = 'Ask AI again';
  const posted = [];
  await page.route('**/request/', async route => {
    posted.push(await postedFields(route.request()));
    await route.fulfill({json: {ok: true, proposal_id: 8, status: 'queued', job_id: 3}});
  });
  await serve(page, payload({badge: 'Running'}));
  const loads = await mountRouted(page, revision => [revision === 4 ? failed : payload({badge: 'Running'})]);
  await expect(slot(page, 'failure')).toHaveText('Backend refusal (backend_refusal)');

  await action(page, 'request').click();

  await expect(page.locator('#ndi-revision')).toHaveText('5');
  await expect(slot(page, 'badge')).toHaveText('Running');
  await expect(slot(page, 'progress')).toBeVisible();
  expect(loads()).toBe(2);
  expect(posted).toEqual([{
    csrfmiddlewaretoken: 'fixture-token', ...Object.fromEntries(claimFields()), field_key: 'field', proposal_id: '7',
  }]);
});

test('acceptance posts the claim and reloads the workspace', async ({page}) => {
  const posted = [];
  await page.route('**/accept/', async route => {
    posted.push(await postedFields(route.request()));
    await route.fulfill({json: {ok: true, proposal_id: 7, status: 'completed', decision: 'accepted'}});
  });
  const loads = await mountRouted(page, revision => [
    revision === 4 ? completed() : completed({badge: 'Accepted', field_state: 'accepted'}),
  ]);

  await action(page, 'accept').click();

  await expect(page.locator('#ndi-revision')).toHaveText('5');
  await expect(slot(page, 'state')).toHaveText('accepted');
  expect(loads()).toBe(2);
  expect(posted[0]).toMatchObject({proposal_id: '7', preview_revision: '4', preview_token: 'token-1'});
});

test('actions on two cards reload once, and the late answer changes nothing', async ({page}) => {
  const held = [];
  await page.route('**/reject/', route => { held.push(route); });
  await page.route('**/accept/', route =>
    route.fulfill({json: {ok: true, proposal_id: 7, status: 'completed', decision: 'accepted'}}));
  const loads = await mountRouted(page, () => [completed(), {other: completed()}]);

  await inCard(page, 'other', '[data-proposal-action="reject"]').click();
  await expect.poll(() => held.length).toBe(1);
  await inCard(page, 'field', '[data-proposal-action="accept"]').click();
  await expect(page.locator('#ndi-revision')).toHaveText('5');

  // The page that sent the rejection is gone, so its answer has nothing to update.
  await held[0].fulfill({status: 409, json: {ok: false, error: 'A newer preview replaced this one.'}}).catch(() => {});
  await page.waitForTimeout(200);
  expect(loads()).toBe(2);
  await expect(inCard(page, 'other', '[data-proposal-error]')).toBeHidden();
  await expect(inCard(page, 'other', '[data-proposal-action="reject"]')).toBeEnabled();
});

test('HTTP action refusals are visible in the field and do not reload', async ({page}) => {
  await page.route('**/accept/', route => route.fulfill({status: 409, json: {ok: false, error: 'The proposal is stale.'}}));
  await serve(page);
  const loads = await mountRouted(page, () => [completed()]);
  await action(page, 'accept').click();
  await expect(slot(page, 'error')).toHaveText('The proposal is stale.');
  await expect(slot(page, 'error')).toBeVisible();
  await expect(action(page, 'accept')).toBeEnabled();
  expect(loads()).toBe(1);
});

test('a poll the server refuses stops polling and says why', async ({page}) => {
  await page.clock.install({time: new Date("2026-09-11T08:00:00Z")});
  await page.clock.pauseAt(new Date("2026-09-11T08:00:01Z"));
  await page.route('**/proposal/**', route =>
    route.fulfill({status: 409, json: {ok: false, error: 'A newer preview replaced this one.', code: 'preview_stale'}}));
  await mount(page);
  await page.clock.runFor(3000);
  await expect(slot(page, 'error')).toHaveText('A newer preview replaced this one.');
  await page.clock.runFor(12000);
  expect(await page.evaluate(() => window.proposalReads)).toBe(1);
});

test('a netbox-branching refusal is visible in the field', async ({page}) => {
  const refusal = {
    ok: false,
    error: 'NetBox Data Import runs on main only, and the active branch is \u201cfeature\u201d.',
    code: 'branch_not_supported',
  };
  await page.route('**/accept/', route => route.fulfill({status: 409, json: refusal}));
  await page.route('**/proposal/**', route => route.fulfill({status: 409, json: refusal}));
  await mount(page, completed());
  await action(page, 'accept').click();
  await expect(slot(page, 'error')).toHaveText(refusal.error);
  await expect(slot(page, 'error')).toBeVisible();
});

test('a proposal another tab requested adds the card and history while field actions stay outside it', async ({page}) => {
  const initial = completed({field_state: 'unresolved', state_style: 'unresolved'});
  initial.proposal = null;
  initial.history_display = [];
  await serve(page, payload());
  // The refusal reads the field again, and the read finds the proposal the other tab asked for.
  await page.route('**/request/', route => route.fulfill({
    status: 409, json: {ok: false, error: 'This field already has an active Resolution Proposal.'},
  }));
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

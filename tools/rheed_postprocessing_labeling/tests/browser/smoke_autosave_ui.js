#!/usr/bin/env node
'use strict';

const fs = require('fs');
const http = require('http');
const path = require('path');

const playwright = require(process.env.PLAYWRIGHT_MODULE || 'playwright');
const report = path.resolve(process.argv[2] || '');
if (!fs.existsSync(report)) throw new Error(`Report not found: ${report}`);
const root = path.dirname(report);
const reportHtml = fs.readFileSync(report, 'utf8');
const reportConfigMatch = reportHtml.match(/\s+const config = (\{[^\r\n]+\});\r?\n/);
if (!reportConfigMatch) throw new Error('Could not locate embedded report config');
const reportConfig = JSON.parse(reportConfigMatch[1]);
const reviewImages = path.join(root, 'images');
const retainedFrameIndices = fs.readdirSync(reviewImages)
  .map(name => /^frame_(\d+)\.webp$/i.exec(name))
  .filter(Boolean)
  .map(match => Number(match[1]) - 1)
  .sort((left, right) => left - right);
if (!retainedFrameIndices.length) {
  throw new Error(`No retained review frames found in: ${reviewImages}`);
}
let desktopVerification = 'pending';
let desktopRevisionRequests = 0;
const desktopEvents = [];
const desktopRevisions = [];
let activeBrowser = null;

function sendJson(response, status, value) {
  response.writeHead(status, { 'Content-Type': 'application/json; charset=utf-8' });
  response.end(JSON.stringify(value));
}

function nearestRetainedFrame(index) {
  return retainedFrameIndices.reduce((best, candidate) =>
    Math.abs(candidate - index) < Math.abs(best - index) ? candidate : best,
  retainedFrameIndices[0]);
}

async function selectedFrameSnapshot(page) {
  return page.evaluate(() => ({
    index: Number(document.querySelector('#arp-frame-scrubber-point')?.value),
    asset: new URL(document.querySelector('#arp-selected-image')?.src || '',
      document.baseURI).pathname.split('/').pop(),
  }));
}

async function assertSelectedFrame(page, expectedIndex, action) {
  const selected = await selectedFrameSnapshot(page);
  const expectedAsset = `frame_${String(expectedIndex + 1).padStart(4, '0')}.webp`;
  if (selected.index !== expectedIndex || selected.asset !== expectedAsset) {
    throw new Error(JSON.stringify({ action, expectedIndex, expectedAsset, selected }, null, 2));
  }
  return selected;
}

async function dispatchNavigationKey(page, key) {
  await page.evaluate(value => {
    document.dispatchEvent(new KeyboardEvent('keydown', { key: value, bubbles: true }));
  }, key);
}

async function verifyRetainedFrameNavigation(page) {
  const first = retainedFrameIndices[0];
  const last = retainedFrameIndices[retainedFrameIndices.length - 1];
  await dispatchNavigationKey(page, 'End');
  await assertSelectedFrame(page, last, 'End');
  await dispatchNavigationKey(page, 'Home');
  await assertSelectedFrame(page, first, 'Home');

  if (retainedFrameIndices.length > 1) {
    const second = retainedFrameIndices[1];
    if (await page.locator('#arp-frame-next').isVisible()) {
      await page.locator('#arp-frame-next').click();
      await assertSelectedFrame(page, second, 'annotation next button');
      await page.locator('#arp-frame-previous').click();
      await assertSelectedFrame(page, first, 'annotation previous button');
    }
    await page.locator('#arp-frame-next-point').click();
    await assertSelectedFrame(page, second, 'point next button');
    await page.locator('#arp-frame-previous-point').click();
    await assertSelectedFrame(page, first, 'point previous button');
    await dispatchNavigationKey(page, 'ArrowRight');
    await assertSelectedFrame(page, second, 'ArrowRight');
    await dispatchNavigationKey(page, 'ArrowLeft');
    await assertSelectedFrame(page, first, 'ArrowLeft');
  }

  const retained = new Set(retainedFrameIndices);
  const missing = Array.from({ length: last - first + 1 }, (_, offset) => first + offset)
    .find(index => !retained.has(index));
  let snapped = null;
  if (missing !== undefined) {
    snapped = nearestRetainedFrame(missing);
    await page.locator('#arp-frame-scrubber-point').evaluate((input, value) => {
      input.value = String(value);
      input.dispatchEvent(new Event('input', { bubbles: true }));
    }, missing);
    await assertSelectedFrame(page, snapped, 'scrubber nearest-frame snap');
  }
  await dispatchNavigationKey(page, 'Home');
  return { first, last, retainedCount: retainedFrameIndices.length, missing, snapped };
}

const server = http.createServer((request, response) => {
  const requestUrl = new URL(request.url, 'http://127.0.0.1');
  const pathname = decodeURIComponent(requestUrl.pathname);
  if (pathname === '/favicon.ico') {
    response.writeHead(204).end();
    return;
  }
  if (pathname === '/api/status') {
    sendJson(response, 200, {
      schema_version: 1,
      desktop: true,
      equalizer_available: false,
      revision_persistence_available: true,
      event_state_available: true,
      event_import_available: false,
      session_verification: desktopVerification,
      session_verification_error: '',
    });
    return;
  }
  if (pathname === '/api/events') {
    sendJson(response, 200, {
      events: desktopEvents,
      revisions: desktopRevisions,
      annotation_set: { annotation_set_id: 'smoke-desktop-annotations', reviewer: '' },
    });
    return;
  }
  if (pathname === '/api/revisions' && request.method === 'POST') {
    let rawBody = '';
    request.setEncoding('utf8');
    request.on('data', chunk => { rawBody += chunk; });
    request.on('end', () => {
      const command = JSON.parse(rawBody || '{}');
      desktopRevisionRequests += 1;
      const anchor = command.changes?.anchor || {};
      const eventId = `00000000-0000-4000-8000-${String(desktopRevisionRequests).padStart(12, '0')}`;
      const revisionId = `10000000-0000-4000-8000-${String(desktopRevisionRequests).padStart(12, '0')}`;
      const event = {
        schema: 'rheed-point-events-v3',
        event_id: eventId,
        source: {
          kind: 'posthoc',
          created_at_utc: new Date().toISOString(),
          created_by: command.actor,
          original_at_utc: anchor.captured_at_utc || '',
          original_elapsed_s: anchor.elapsed_s ?? 0,
          original_anchor: anchor,
          original_note: '',
          source_file: '',
          source_sequence: desktopRevisionRequests,
          source_row_sha256: '',
          legacy_imports: [],
        },
        review: {
          anchor,
          representative_anchor: null,
          labels: [],
          candidate_decision: 'confirmed',
          comment: '',
          reviewer: command.actor,
          confidence: null,
          disposition: 'active',
          disposition_reason: '',
        },
        status: 'Draft',
        revision_id: revisionId,
      };
      const revision = {
        schema_version: 'rheed-point-events-v3',
        revision_id: revisionId,
        base_revision_id: command.base_revision_id || '',
        actor: command.actor,
        action: command.action,
        event_id: eventId,
        at_utc: new Date().toISOString(),
      };
      desktopEvents.push(event);
      desktopRevisions.push(revision);
      desktopVerification = 'ready';
      setTimeout(() => sendJson(response, 200, { event, revision }), 300);
    });
    return;
  }
  const relative = pathname === '/' ? 'interactive_report.html' : pathname.slice(1);
  const candidate = path.resolve(root, relative);
  if (candidate !== root && !candidate.startsWith(root + path.sep)) {
    response.writeHead(403).end();
    return;
  }
  fs.readFile(candidate, (error, data) => {
    if (error) {
      response.writeHead(404).end();
      return;
    }
    response.writeHead(200, {
      'Content-Type': candidate.endsWith('.html') ? 'text/html; charset=utf-8' :
        candidate.endsWith('.js') ? 'text/javascript; charset=utf-8' : 'application/octet-stream',
    });
    response.end(data);
  });
});

(async () => {
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  const address = server.address();
  const browser = await playwright.chromium.launch({
    headless: true,
    executablePath: process.env.PLAYWRIGHT_EXECUTABLE_PATH || undefined,
  });
  activeBrowser = browser;
  const page = await browser.newPage({ viewport: { width: 1280, height: 900 } });
  const errors = [];
  page.on('pageerror', error => errors.push(String(error)));
  page.on('console', message => {
    if (message.type() === 'error') errors.push(message.text());
  });
  await page.goto(`http://127.0.0.1:${address.port}/`, { waitUntil: 'domcontentloaded' });
  await page.waitForSelector('#arp-point-editor:not([hidden])');
  const facts = await page.evaluate(() => ({
    grower: Boolean(document.querySelector('#arp-point-grower')),
    oldSave: Boolean(document.querySelector('#arp-point-save')),
    oldReviewer: Boolean(document.querySelector('#arp-point-reviewer')),
    oldConfidence: Boolean(document.querySelector('#arp-point-confidence')),
    initialQualityButtons: document.querySelectorAll('[data-point-initial-quality]').length,
    qualityButtons: document.querySelectorAll('[data-point-quick-kind="surface_quality"]').length,
    intervalAnchor: Boolean(document.querySelector('#arp-point-interval-anchor')),
    visiblePhysicalEvents: document.querySelectorAll('.arp-point-marker[data-source-kind="initial_assumption"]').length,
    commandAfterTrack: document.querySelector('#arp-point-track-host')?.nextElementSibling
      ?.classList.contains('arp-point-command-bar') || false,
    commandBeforeWorkspace: Boolean(document.querySelector('.arp-point-command-bar')
      ?.compareDocumentPosition(document.querySelector('.arp-point-workspace')) &
      Node.DOCUMENT_POSITION_FOLLOWING),
    addNativeDisabled: document.querySelector('#arp-point-add')?.disabled,
    evidenceTag: document.querySelector('.arp-point-evidence')?.tagName || '',
    contextTag: document.querySelector('.arp-point-context')?.tagName || '',
    labelsBeforeNote: Boolean(document.querySelector('.arp-point-label-editor')
      ?.compareDocumentPosition(document.querySelector('#arp-point-comment')) &
      Node.DOCUMENT_POSITION_FOLLOWING),
  }));
  if (!facts.grower || facts.oldSave || facts.oldReviewer || facts.oldConfidence ||
      facts.initialQualityButtons !== 0 || facts.qualityButtons !== 0 ||
      !facts.intervalAnchor || facts.visiblePhysicalEvents !== 0 || !facts.commandAfterTrack ||
      !facts.commandBeforeWorkspace || facts.addNativeDisabled || facts.evidenceTag !== 'DETAILS' ||
      facts.contextTag !== 'DETAILS' || !facts.labelsBeforeNote || errors.length) {
    throw new Error(JSON.stringify({ facts, errors }, null, 2));
  }
  const retainedNavigation = await verifyRetainedFrameNavigation(page);
  await page.waitForTimeout(500);
  const baselineSummary = await page.locator('#arp-point-summary').textContent();
  const baselineEvents = Number.parseInt(String(baselineSummary || ''), 10);
  if (!Number.isInteger(baselineEvents) || baselineEvents < 0) {
    throw new Error(`Could not parse point-event summary: ${baselineSummary}`);
  }
  await page.locator('#arp-point-add').click();
  const missingGrower = await page.evaluate(() => ({
    activeId: document.activeElement?.id || '',
    ariaInvalid: document.querySelector('#arp-point-grower')?.getAttribute('aria-invalid'),
    message: document.querySelector('#arp-point-status')?.textContent || '',
    tone: document.querySelector('#arp-point-status')?.dataset.tone || '',
    summary: document.querySelector('#arp-point-summary')?.textContent || '',
  }));
  if (missingGrower.activeId !== 'arp-point-grower' ||
      missingGrower.ariaInvalid !== 'true' ||
      missingGrower.tone !== 'error' ||
      !/Enter the Grower name above before adding an event/i.test(missingGrower.message) ||
      missingGrower.summary !== baselineSummary) {
    throw new Error(JSON.stringify({ facts, baselineSummary, missingGrower, errors }, null, 2));
  }
  await page.locator('#arp-point-grower').fill('smoke-grower');
  await page.waitForTimeout(450);
  await page.locator('#arp-point-add').click();
  const expectedEventCount = baselineEvents + 1;
  await page.waitForFunction(expected =>
    Number.parseInt(document.querySelector('#arp-point-summary')?.textContent || '', 10) === expected,
  expectedEventCount);
  await page.waitForFunction(() =>
    document.activeElement?.id === 'arp-point-reconstruction-choice');
  const addEvent = await page.evaluate(() => ({
    summary: document.querySelector('#arp-point-summary')?.textContent || '',
    formVisible: !document.querySelector('#arp-point-form')?.hidden,
    message: document.querySelector('#arp-point-status')?.textContent || '',
    tone: document.querySelector('#arp-point-status')?.dataset.tone || '',
    activeId: document.activeElement?.id || '',
    pulse: document.querySelector('.arp-point-card')?.classList.contains('is-new-event') || false,
    selectedQueueRows: document.querySelectorAll(
      '.arp-point-queue-list .btn[aria-pressed="true"]').length,
    evidenceClosed: !document.querySelector('.arp-point-evidence')?.open,
    contextClosed: !document.querySelector('.arp-point-context')?.open,
  }));
  if (!addEvent.formVisible || Number.parseInt(addEvent.summary, 10) !== expectedEventCount ||
      !/Event added and autosaved/.test(addEvent.message) || addEvent.tone !== 'success' ||
      addEvent.activeId !== 'arp-point-reconstruction-choice' || !addEvent.pulse ||
      addEvent.selectedQueueRows < 1 || !addEvent.evidenceClosed ||
      !addEvent.contextClosed || errors.length) {
    throw new Error(JSON.stringify({ facts, addEvent, errors }, null, 2));
  }

  const desktopContext = await browser.newContext({ viewport: { width: 1280, height: 900 } });
  const desktopPage = await desktopContext.newPage();
  const desktopErrors = [];
  desktopPage.on('pageerror', error => desktopErrors.push(String(error)));
  desktopPage.on('console', message => {
    if (message.type() === 'error') desktopErrors.push(message.text());
  });
  await desktopPage.goto(
    `http://127.0.0.1:${address.port}/?bridge_token=smoke-desktop-token-1234`,
    { waitUntil: 'domcontentloaded' }
  );
  await desktopPage.waitForFunction(() =>
    /Verifying source session in background/.test(
      document.querySelector('#arp-point-status')?.textContent || ''));
  await dispatchNavigationKey(desktopPage, 'End');
  const desktopSelectedFrame = await assertSelectedFrame(
    desktopPage, retainedNavigation.last, 'desktop End');
  const pendingDesktop = await desktopPage.evaluate(() => ({
    message: document.querySelector('#arp-point-status')?.textContent || '',
    nativeDisabled: document.querySelector('#arp-point-add')?.disabled,
    ariaDisabled: document.querySelector('#arp-point-add')?.getAttribute('aria-disabled'),
  }));
  await desktopPage.locator('#arp-point-grower').fill('desktop-smoke-grower');
  await desktopPage.locator('#arp-point-add').click();
  await desktopPage.waitForFunction(() =>
    document.querySelector('#arp-point-add')?.textContent === 'Adding…');
  const addingDesktop = await desktopPage.evaluate(() => ({
    button: document.querySelector('#arp-point-add')?.textContent || '',
    busy: document.querySelector('#arp-point-add')?.getAttribute('aria-busy'),
    nativeDisabled: document.querySelector('#arp-point-add')?.disabled,
    message: document.querySelector('#arp-point-status')?.textContent || '',
  }));
  await desktopPage.waitForFunction(() =>
    /^1 event\b/.test(document.querySelector('#arp-point-summary')?.textContent || ''));
  await desktopPage.waitForFunction(() =>
    document.activeElement?.id === 'arp-point-reconstruction-choice');
  const desktopAddEvent = await desktopPage.evaluate(() => ({
    summary: document.querySelector('#arp-point-summary')?.textContent || '',
    message: document.querySelector('#arp-point-status')?.textContent || '',
    tone: document.querySelector('#arp-point-status')?.dataset.tone || '',
    activeId: document.activeElement?.id || '',
    pulse: document.querySelector('.arp-point-card')?.classList.contains('is-new-event') || false,
    selectedQueueRows: document.querySelectorAll(
      '.arp-point-queue-list .btn[aria-pressed="true"]').length,
    canonicalId: document.querySelector(
      '.arp-point-queue-list .btn[aria-pressed="true"]')?.dataset.pointEventListId || '',
  }));
  const desktopAnchor = desktopEvents[0]?.review?.anchor || {};
  const expectedDesktopAsset = String(
    reportConfig.frame_contexts?.[retainedNavigation.last]?.archive_member ||
    `images/frame_${String(retainedNavigation.last + 1).padStart(4, '0')}.webp`
  );
  const expectedDesktopSession = String(
    reportConfig.frame_contexts?.[retainedNavigation.last]?.session_id ||
    reportConfig.dataset.acquisition_run || reportConfig.dataset.dataset_id || '',
  );
  if (pendingDesktop.nativeDisabled || pendingDesktop.ariaDisabled !== 'false' ||
      addingDesktop.button !== 'Adding…' || addingDesktop.busy !== 'true' ||
      addingDesktop.nativeDisabled || !/this save may wait/.test(addingDesktop.message) ||
      !/^1 event\b/.test(desktopAddEvent.summary) ||
      !/Event added and autosaved/.test(desktopAddEvent.message) ||
      desktopAddEvent.tone !== 'success' ||
      desktopAddEvent.activeId !== 'arp-point-reconstruction-choice' ||
      !desktopAddEvent.pulse || desktopAddEvent.selectedQueueRows < 1 ||
      !/^00000000-0000-4000-8000-/.test(desktopAddEvent.canonicalId) ||
      desktopAnchor.session_identity !== expectedDesktopSession ||
      desktopAnchor.frame_index !== retainedNavigation.last + 1 ||
      desktopAnchor.archive_member !== expectedDesktopAsset ||
      desktopRevisionRequests !== 1 || desktopErrors.length) {
    throw new Error(JSON.stringify({
      pendingDesktop, addingDesktop, desktopAddEvent,
      desktopSelectedFrame, desktopAnchor, expectedDesktopAsset,
      expectedDesktopSession, desktopRevisionRequests, desktopErrors,
    }, null, 2));
  }
  await desktopContext.close();
  process.stdout.write(JSON.stringify({
    ok: true, facts, retainedNavigation, baselineSummary, missingGrower, addEvent,
    pendingDesktop, addingDesktop, desktopSelectedFrame, desktopAnchor, desktopAddEvent,
    errors, desktopErrors,
  }) + '\n');
  await browser.close();
  activeBrowser = null;
  server.close();
})().catch(async error => {
  process.stderr.write(String(error.stack || error) + '\n');
  if (activeBrowser) {
    await activeBrowser.close().catch(() => {});
    activeBrowser = null;
  }
  server.close();
  process.exitCode = 1;
});

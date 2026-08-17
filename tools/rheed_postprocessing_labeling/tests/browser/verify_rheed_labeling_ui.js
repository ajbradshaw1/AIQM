#!/usr/bin/env node
'use strict';

// Offline browser acceptance test for point-event review. The ordinary local
// server intentionally exposes no desktop Equalizer bridge.
const fs = require('fs');
const http = require('http');
const path = require('path');

function loadPlaywright() {
  const candidates = [process.env.PLAYWRIGHT_MODULE, 'playwright', '@playwright/test', 'playwright-core'].filter(Boolean);
  const failures = [];
  for (const candidate of candidates) {
    try {
      const loaded = require(candidate);
      const chromium = loaded.chromium || (loaded.default && loaded.default.chromium);
      if (chromium) return { chromium, moduleName: candidate };
      failures.push(`${candidate}: no chromium export`);
    } catch (error) { failures.push(`${candidate}: ${error.message}`); }
  }
  throw new Error(`Cannot load Playwright. Set PLAYWRIGHT_MODULE if needed.\n${failures.join('\n')}`);
}

function assert(condition, message, detail) {
  if (!condition) throw new Error(`${message}${detail === undefined ? '' : `: ${JSON.stringify(detail)}`}`);
}

function reportFixture(reportPath) {
  const html = fs.readFileSync(reportPath, 'utf8');
  const configMatch = html.match(/(\s+const config = )(\{[^\r\n]+\})(;\r?\n)/);
  assert(configMatch, 'Could not locate embedded report config');
  const config = JSON.parse(configMatch[2]);
  const packed = (name, width, reader) => {
    const match = html.match(new RegExp(`const ${name} = decode\\('([^']+)'`));
    assert(match, `Could not locate packed ${name}`);
    const bytes = Buffer.from(match[1], 'base64');
    return Array.from({ length: bytes.length / width }, (_, index) => bytes[reader](index * width));
  };
  const times = packed('times', 4, 'readFloatLE');
  const heartbeats = packed('heartbeats', 4, 'readUInt32LE');
  const sequences = packed('sequences', 4, 'readUInt32LE');
  const hashMatch = html.match(/const frameHashBytes = decode\('([^']+)'/);
  assert(hashMatch, 'Could not locate packed frame hashes');
  const hashBytes = Buffer.from(hashMatch[1], 'base64');
  const hashes = Array.from({ length: hashBytes.length / 32 }, (_, index) =>
    hashBytes.subarray(index * 32, (index + 1) * 32).toString('hex'));
  const anchorIndex = 1;
  const clickIndex = 4;
  const anchor = {
    frame_index: anchorIndex + 1,
    heartbeat_idx: heartbeats[anchorIndex],
    elapsed_s: times[anchorIndex],
    captured_at_utc: config.frame_contexts[anchorIndex].captured_at_utc,
    capture_sequence: sequences[anchorIndex],
    image_sha256: hashes[anchorIndex],
    image_sha256_algorithm: 'raw-file-bytes-v1',
    archive_member: `images/frame_${String(anchorIndex + 1).padStart(4, '0')}.png`,
  };
  const weights = {
    raw: { '1x1': null, 'Tw(2x1)': null, 'c(6x2)': null, RT13: null, HTR: null },
    final: { '1x1': 0.7, 'Tw(2x1)': 0.1, 'c(6x2)': 0.1, RT13: 0.1, HTR: null },
    normalized: { '1x1': 0.7, 'Tw(2x1)': 0.1, 'c(6x2)': 0.1, RT13: 0.1, HTR: null },
  };
  const event = {
    schema: 'rheed-point-events-v1',
    event_id: 'browser-source-event-click-time-test',
    source: {
      kind: 'manual', session_identity: 'browser-fixture', source_file: 'rheed_events.csv',
      source_file_sha256: '1'.repeat(64), source_row_sha256: '2'.repeat(64), source_row_index: 7,
      source_sequence: 7, original_at_utc: config.frame_contexts[clickIndex].captured_at_utc,
      original_elapsed_s: times[clickIndex], original_anchor: anchor,
      original_note: 'Live click deliberately differs from captured frame time', legacy_imports: [],
    },
    review: {
      anchor, comment: 'Ready for durability failure test', reviewer: 'Browser Test Reviewer', confidence: 0.9,
      human_reconstruction: 'none_weak', change_from: null, change_to: null,
      equalizer: {
        schema_version: 1, valid: true, calibration_id: 'cal-browser', basis_bundle_id: 'basis-browser',
        frame_sha256: hashes[anchorIndex], frame_sha256_algorithm: 'raw-file-bytes-v1',
        capture_sequence: sequences[anchorIndex], active_classes: ['1x1', 'Tw(2x1)', 'c(6x2)', 'RT13'],
        fit_residual: 1.25, valid_coverage: 0.82, weights, HTR: null,
      },
      disposition: 'active', disposition_reason: '',
    },
    status: 'Draft', revision_id: 'browser-base-revision',
  };
  const unresolvedEvent = {
    schema: 'rheed-point-events-v1',
    event_id: 'browser-auto-event-unresolved-provenance',
    source: {
      kind: 'auto_capture', session_identity: 'browser-fixture', source_file: 'events_labels.csv',
      source_file_sha256: '4'.repeat(64), source_row_sha256: '5'.repeat(64), source_row_index: 8,
      source_sequence: 8, original_at_utc: config.frame_contexts[3].captured_at_utc,
      original_elapsed_s: times[3],
      original_anchor: {
        frame_index: null, heartbeat_idx: null, capture_sequence: 999999,
        image_sha256: '', image_sha256_algorithm: 'raw-file-bytes-v1',
        mapping_status: 'ambiguous', mapping_candidates: 2,
      },
      original_note: 'Automatic event could not be bound uniquely to a saved heartbeat frame', legacy_imports: [],
    },
    review: {
      anchor, comment: '', reviewer: '', confidence: null, human_reconstruction: null,
      change_from: null, change_to: null, equalizer: null, disposition: 'active', disposition_reason: '',
    },
    status: 'Draft', revision_id: 'browser-unresolved-base-revision',
  };
  config.sensor_context = {
    source_file: 'sensor_log.csv', source_file_sha256: '3'.repeat(64),
    columns: ['elapsed_s', 'pyrometer_temp_C', 'mistral_v_actual_V', 'mistral_i_actual_A',
      'pyrometer_age_ms', 'mistral_age_ms', 'evap_age_ms', 'rheed_age_ms'],
    rows: [
      { elapsed_s: times[anchorIndex], pyrometer_temp_C: 111.1, mistral_v_actual_V: 1.111,
        mistral_i_actual_A: 0.111, pyrometer_age_ms: 101, mistral_age_ms: 102, evap_age_ms: 103, rheed_age_ms: 104 },
      { elapsed_s: times[clickIndex], pyrometer_temp_C: 777.7, mistral_v_actual_V: 4.321,
        mistral_i_actual_A: 0.876, pyrometer_age_ms: 11, mistral_age_ms: 22, evap_age_ms: 33, rheed_age_ms: 44 },
    ],
  };
  const transformed = html.replace(configMatch[0], `${configMatch[1]}${JSON.stringify(config)}${configMatch[3]}`);
  return { html: transformed, event, unresolvedEvent, clickElapsed: times[clickIndex], anchorElapsed: times[anchorIndex] };
}

async function startLocalServer(reportPath, options = {}) {
  const root = path.dirname(reportPath);
  const types = { '.html': 'text/html; charset=utf-8', '.js': 'text/javascript; charset=utf-8', '.json': 'application/json; charset=utf-8', '.webp': 'image/webp', '.png': 'image/png' };
  const server = http.createServer(async (request, response) => {
    const url = new URL(request.url, 'http://127.0.0.1');
    if (options.api && url.pathname.startsWith('/api/')) {
      try { await options.api(request, response, url); }
      catch (error) { response.writeHead(500, { 'Content-Type': 'application/json' }).end(JSON.stringify({ error: error.message })); }
      return;
    }
    const relative = decodeURIComponent(url.pathname === '/' ? `/${path.basename(reportPath)}` : url.pathname).replace(/^\/+/, '');
    const target = path.resolve(root, relative);
    if (target !== root && !target.startsWith(`${root}${path.sep}`)) return response.writeHead(403).end('forbidden');
    if (options.reportHtml !== undefined && target === reportPath) {
      response.writeHead(200, { 'Content-Type': 'text/html; charset=utf-8', 'Cache-Control': 'no-store' });
      return response.end(options.reportHtml);
    }
    fs.stat(target, (error, stat) => {
      if (error || !stat.isFile()) return response.writeHead(404).end('not found');
      response.writeHead(200, { 'Content-Type': types[path.extname(target).toLowerCase()] || 'application/octet-stream', 'Cache-Control': 'no-store' });
      fs.createReadStream(target).pipe(response);
    });
  });
  await new Promise((resolve, reject) => { server.once('error', reject); server.listen(0, '127.0.0.1', resolve); });
  return {
    url: `http://127.0.0.1:${server.address().port}/${encodeURIComponent(path.basename(reportPath))}`,
    close: () => new Promise((resolve, reject) => {
      server.close(error => error ? reject(error) : resolve());
      if (typeof server.closeAllConnections === 'function') server.closeAllConnections();
    }),
  };
}

async function waitForEditor(page) {
  await page.waitForSelector('#arp-point-editor:not([hidden])', { timeout: 20000 });
  await page.waitForSelector('#arp-point-track-host .arp-point-track', { timeout: 20000 });
}

async function setFrame(page, index) {
  const scrubber = page.locator('#arp-frame-scrubber-point');
  await scrubber.fill(String(index));
  await scrubber.dispatchEvent('input');
  await page.waitForTimeout(25);
}

async function downloadText(page, click) {
  const pending = page.waitForEvent('download');
  await click();
  return fs.readFileSync(await (await pending).path(), 'utf8');
}

async function verifyDesktopFailClosed(browser, reportPath) {
  const fixture = reportFixture(reportPath);
  const token = 'browser-test-bridge-token-0123456789abcdef';
  const sendJson = (response, status, payload) => {
    response.writeHead(status, { 'Content-Type': 'application/json', 'Cache-Control': 'no-store' });
    response.end(JSON.stringify(payload));
  };
  let server = await startLocalServer(reportPath, {
    reportHtml: fixture.html,
    api: async (request, response, url) => {
      if (url.searchParams.get('bridge_token') !== token) return sendJson(response, 403, { error: 'forbidden' });
      if (url.pathname === '/api/status') return sendJson(response, 200, {
        desktop: true, equalizer_available: true, revision_persistence_available: true, event_state_available: true,
      });
      if (url.pathname === '/api/events') return sendJson(response, 200, {
        ok: true, events: [fixture.event, fixture.unresolvedEvent], revisions: [], unfinished_count: 2,
        annotation_set: { annotation_set_id: 'browser-desktop-annotation-set', reviewer: 'Browser Test Reviewer' },
      });
      if (url.pathname === '/api/events/import') {
        for await (const _chunk of request) { /* drain the simulated upload */ }
        return sendJson(response, 400, { error: 'simulated atomic import rejection' });
      }
      return sendJson(response, 404, { error: 'unknown endpoint' });
    },
  });
  const context = await browser.newContext({ acceptDownloads: true, viewport: { width: 1024, height: 900 } });
  const page = await context.newPage();
  try {
    await page.goto(`${server.url}?bridge_token=${encodeURIComponent(token)}`, { waitUntil: 'load' });
    await waitForEditor(page);
    await page.waitForFunction(() => /Desktop Labeler connected/i.test(document.querySelector('#arp-point-status')?.textContent || ''), null, { timeout: 10000 });
    await page.locator('[data-point-event-list-id="browser-source-event-click-time-test"]').first().click();
    const contextHost = page.locator('#arp-point-context');
    const lookup = await contextHost.evaluate(element => ({ ...element.dataset }));
    const contextText = await contextHost.textContent();
    assert(Math.abs(Number(lookup.lookupElapsedS) - fixture.clickElapsed) < 0.001,
      'Sensor lookup did not preserve the live click elapsed time', { lookup, fixture });
    assert(Math.abs(Number(lookup.originalAnchorElapsedS) - fixture.anchorElapsed) < 0.001,
      'Fixture did not keep the distinct capture elapsed time', { lookup, fixture });
    assert(Math.abs(Number(lookup.lookupElapsedS) - Number(lookup.originalAnchorElapsedS)) > 1,
      'Click and capture time were not distinct', lookup);
    assert(Math.abs(Number(lookup.matchedElapsedS) - fixture.clickElapsed) < 0.001,
      'Read-only sensor context matched the capture instead of click time', lookup);
    for (const expected of ['777.7 °C', '4.321 V', '0.876 A', '11.0 ms', '22.0 ms', '33.0 ms', '44.0 ms', 'Maximum data age (oldest source)']) {
      assert(contextText.includes(expected), `Production sensor field was not shown: ${expected}`, contextText);
    }
    assert(!contextText.includes('111.1 °C'), 'Sensor context used the captured-frame row instead of click-time row', contextText);
    assert(!await page.locator('#arp-point-complete').isDisabled(), 'Desktop fixture should be eligible for explicit Complete');

    await page.locator('[data-point-event-list-id="browser-auto-event-unresolved-provenance"]').first().click();
    const unresolvedEvidence = await page.locator('#arp-point-evidence').evaluate(host => {
      const terms = [...host.querySelectorAll('dt')];
      return Object.fromEntries(terms.map(term => [term.textContent.trim(), term.nextElementSibling?.textContent.trim()]));
    });
    assert(/separate source image/i.test(unresolvedEvidence['Original frame']),
      'Unresolved auto event was presented as a saved heartbeat frame', unresolvedEvidence);
    assert(unresolvedEvidence['Original frame SHA-256'] === 'N/A',
      'Unresolved auto event was assigned a fabricated frame hash in the UI', unresolvedEvidence);
    const unresolvedJson = JSON.parse(await downloadText(page, () => page.locator('#arp-point-export-json').click()));
    const unresolvedExport = unresolvedJson.events.find(item => item.event_id === 'browser-auto-event-unresolved-provenance');
    assert(unresolvedExport && !unresolvedExport.source.original_anchor.image_sha256,
      'Unresolved auto event was assigned a fabricated frame hash in JSON', unresolvedExport);
    assert(unresolvedExport.source.original_anchor.mapping_status === 'ambiguous',
      'Unresolved auto-event mapping status was not preserved', unresolvedExport?.source?.original_anchor);
    const unresolvedCsv = await downloadText(page, () => page.locator('#arp-point-export-csv').click());
    const csvLines = unresolvedCsv.trimEnd().split(/\r?\n/);
    const csvHeader = csvLines[0].replace(/^\ufeff/, '').split(',');
    const csvRow = csvLines.find(line => line.includes('browser-auto-event-unresolved-provenance'))?.split(',');
    assert(csvRow && csvRow[csvHeader.indexOf('original_frame_sha256')] === '',
      'Unresolved auto event was assigned a fabricated frame hash in CSV', { csvHeader, csvRow });
    await page.locator('[data-point-event-list-id="browser-source-event-click-time-test"]').first().click();

    const beforeImport = await downloadText(page, () => page.locator('#arp-point-export-json').click());
    page.once('dialog', dialog => dialog.accept());
    await page.locator('#arp-point-import').setInputFiles({
      name: 'desktop-import-rejected.json', mimeType: 'application/json', buffer: Buffer.from(beforeImport),
    });
    await page.waitForFunction(() => /simulated atomic import rejection/i.test(document.querySelector('#arp-point-status')?.textContent || ''), null, { timeout: 10000 });
    assert(await page.locator('[data-point-event-list-id="browser-source-event-click-time-test"]').count() >= 1,
      'Rejected desktop import replaced the canonical event state');
    assert(/· Draft ·/.test(await page.locator('#arp-point-editor-heading').textContent()),
      'Rejected desktop import changed event status');
    const afterRejectedImport = JSON.parse(await downloadText(page, () => page.locator('#arp-point-export-json').click()));
    const beforeRejectedEvent = JSON.parse(beforeImport).events.find(item => item.event_id === 'browser-source-event-click-time-test');
    const afterRejectedEvent = afterRejectedImport.events.find(item => item.event_id === 'browser-source-event-click-time-test');
    assert(JSON.stringify(afterRejectedEvent) === JSON.stringify(beforeRejectedEvent),
      'Rejected desktop import changed canonical event content', { beforeRejectedEvent, afterRejectedEvent });

    await server.close();
    server = null;
    await page.locator('#arp-point-complete').click();
    await page.waitForFunction(() => /not applied|persistence failed/i.test(document.querySelector('#arp-point-status')?.textContent || ''), null, { timeout: 10000 });
    assert(/· Draft ·/.test(await page.locator('#arp-point-editor-heading').textContent()),
      'Failed durable Complete changed the visible canonical Draft state');
    const jsonText = await downloadText(page, () => page.locator('#arp-point-export-json').click());
    const exported = JSON.parse(jsonText);
    const event = exported.events.find(item => item.event_id === 'browser-source-event-click-time-test');
    assert(event?.status === 'Draft', 'Failed durable Complete leaked into export', event);
    assert(event?.source?.original_elapsed_s === fixture.clickElapsed,
      'Normalization/export lost source.original_elapsed_s', event?.source);
    return { lookup, contextText, failedCompleteStatus: event.status, unresolvedOriginalHash: null };
  } finally {
    await context.close();
    if (server) await server.close();
  }
}

async function main() {
  const [reportArgument, outputArgument] = process.argv.slice(2);
  if (!reportArgument || !outputArgument) throw new Error('usage: node verify_rheed_labeling_ui.js <interactive_report.html> <output-dir>');
  const unquote = value => String(value).trim().replace(/^["']+|["']+$/g, '');
  const reportPath = path.resolve(unquote(reportArgument));
  const outputDir = path.resolve(unquote(outputArgument));
  assert(fs.existsSync(reportPath), 'Report does not exist', reportPath);
  fs.mkdirSync(outputDir, { recursive: true });
  const { chromium, moduleName } = loadPlaywright();
  const options = { headless: true, args: ['--allow-file-access-from-files'] };
  if (process.env.PLAYWRIGHT_EXECUTABLE_PATH) options.executablePath = process.env.PLAYWRIGHT_EXECUTABLE_PATH;
  const browser = await chromium.launch(options);
  const localServer = await startLocalServer(reportPath);
  const context = await browser.newContext({ acceptDownloads: true, viewport: { width: 1024, height: 900 } });
  const externalRequests = [];
  await context.route(/^https?:/i, route => {
    const url = new URL(route.request().url());
    if (url.hostname === '127.0.0.1' || url.hostname === 'localhost') route.continue();
    else { externalRequests.push(route.request().url()); route.abort('internetdisconnected'); }
  });
  const page = await context.newPage();
  const browserErrors = [];
  page.on('pageerror', error => browserErrors.push(error.stack || error.message));
  page.on('console', message => { if (message.type() === 'error') browserErrors.push(`console: ${message.text()}`); });
  try {
    await page.goto(localServer.url, { waitUntil: 'load' });
    await waitForEditor(page);
    await page.evaluate(() => localStorage.clear());
    await page.reload({ waitUntil: 'load' });
    await waitForEditor(page);
    const bounds = await page.locator('#arp-frame-scrubber-point').evaluate(input => ({ min: Number(input.min), max: Number(input.max) }));
    assert(Number.isInteger(bounds.min) && Number.isInteger(bounds.max), 'Scrubber bounds are invalid', bounds);
    assert(bounds.max - bounds.min >= 5, 'Smoke test needs six saved frames', bounds);
    assert(!await page.locator('#arp-legacy-segment-editor').isVisible(), 'Legacy segment editor is visible');

    await setFrame(page, bounds.min + 1);
    await page.locator('#arp-point-add').click();
    assert(await page.locator('[data-point-event-id]').count() >= 2, 'Posthoc event markers were not rendered');
    const firstId = (await page.locator('#arp-point-editor-heading').textContent()).split(' · ').pop();
    await page.locator('#arp-point-comment').fill('first, "quoted" point event');
    await page.locator('#arp-point-reviewer').fill('Offline Browser Test');
    await page.locator('#arp-point-confidence').fill('0.9');
    await page.locator('#arp-point-reconstruction').selectOption('rt13');
    await page.locator('#arp-point-save').click();
    assert(await page.locator('#arp-point-equalizer-run').isDisabled(), 'Static report enabled Run Equalizer');
    assert(await page.locator('#arp-point-complete').isDisabled(), 'Static report enabled Complete');
    assert(/desktop Labeler/i.test(await page.locator('#arp-point-bridge-note').textContent()), 'Missing bridge explanation');

    await setFrame(page, bounds.min + 3);
    await page.locator('#arp-point-move').click();
    const movedEvidence = await page.locator('#arp-point-evidence').textContent();
    assert(movedEvidence.includes(`#${bounds.min + 4}`), 'Review point did not move', movedEvidence);
    assert(movedEvidence.includes(`#${bounds.min + 2}`), 'Original point was not preserved', movedEvidence);

    await page.locator('#arp-point-add').click();
    await page.locator('#arp-point-comment').fill('second event at same saved frame');
    await page.locator('#arp-point-reviewer').fill('Offline Browser Test');
    await page.locator('#arp-point-save').click();
    assert(await page.locator('[data-point-role="original"]').count() >= 2, 'Same-time point event was rejected');

    const jsonText = await downloadText(page, () => page.locator('#arp-point-export-json').click());
    const exported = JSON.parse(jsonText);
    assert(exported.schema_version === 'rheed-point-events-v1', 'Unexpected schema');
    assert(exported.document_type === 'ai4mbe_rheed_point_event_annotations', 'Unexpected document type');
    assert(exported.dataset.dataset_id, 'Missing dataset identity');
    assert(/^[0-9a-f]{64}$/i.test(exported.dataset.source_archive_sha256), 'Missing source-archive SHA-256');
    assert(/^sha256:[0-9a-f]{64}$/i.test(exported.dataset.ordered_frame_fingerprint), 'Missing ordered-frame fingerprint');
    assert(/^sha256:[0-9a-f]{64}$/i.test(exported.dataset.model_context_fingerprint), 'Missing model-context fingerprint');
    assert(exported.annotation_set.model_outputs_visible === true, 'Model visibility was not disclosed');
    assert(exported.annotation_set.eligible_for_gold === false, 'Model-assisted events claim gold eligibility');
    assert(exported.review_mode.equalizer_is_model_probability === false, 'Equalizer was presented as probability');
    assert(exported.events.length >= 2, 'Point events did not export');
    for (const event of exported.events.filter(item => item.review?.disposition === 'active')) {
      const anchor = event.review?.anchor || {};
      assert(Number.isInteger(anchor.frame_index) && anchor.frame_index >= 1, 'Review anchor has no saved-frame ordinal', event);
      assert(Number.isInteger(anchor.heartbeat_idx) && anchor.heartbeat_idx >= 0, 'Review anchor has no heartbeat index', event);
      assert(Number.isInteger(anchor.capture_sequence) && anchor.capture_sequence >= 0, 'Review anchor has no capture sequence', event);
      assert(/^[0-9a-f]{64}$/i.test(anchor.image_sha256), 'Review anchor has no frame SHA-256', event);
    }
    const first = exported.events.find(event => event.event_id === firstId);
    assert(first && first.review.comment === 'first, "quoted" point event', 'Edited point event did not round-trip');
    assert(first.status === 'Draft' && first.review.equalizer === null, 'Incomplete/moved event is not a clean Draft');
    assert(first.review.anchor.frame_index === bounds.min + 4, 'Moved review anchor did not export');
    assert(first.source.original_anchor.frame_index === bounds.min + 2, 'Original anchor changed');
    assert(exported.revisions.some(item => item.action === 'move_anchor'), 'Move was not audited');

    const tampered = JSON.parse(jsonText);
    tampered.events[0].review.anchor.image_sha256 = 'f'.repeat(64);
    await page.locator('#arp-point-import').setInputFiles({ name: 'tampered.json', mimeType: 'application/json', buffer: Buffer.from(JSON.stringify(tampered)) });
    assert(/hash|provenance|immutable/i.test(await page.locator('#arp-point-status').textContent()), 'Tampered frame was not rejected');
    const afterTamperedText = await downloadText(page, () => page.locator('#arp-point-export-json').click());
    const afterTampered = JSON.parse(afterTamperedText);
    assert(JSON.stringify(afterTampered.events) === JSON.stringify(exported.events), 'Rejected static import changed point-event state');
    assert(JSON.stringify(afterTampered.revisions) === JSON.stringify(exported.revisions), 'Rejected static import changed revision history');

    const csvText = await downloadText(page, () => page.locator('#arp-point-export-csv').click());
    const csvHeader = csvText.split(/\r?\n/, 1)[0].replace(/^\ufeff/, '').split(',');
    for (const column of [
      'source_archive_sha256', 'ordered_frame_fingerprint', 'model_context_fingerprint',
      'model_outputs_visible', 'eligible_for_gold', 'event_id', 'original_frame_sha256',
      'review_frame_sha256', 'calibration_id', 'equalizer_htr',
    ]) assert(csvHeader.includes(column), `CSV omits ${column}`);

    await page.reload({ waitUntil: 'load' });
    await waitForEditor(page);
    const restoredCount = await page.locator(`[data-point-event-list-id="${firstId}"]`).count();
    assert(restoredCount >= 1, 'Stable ID was not restored', {
      firstId, restoredCount,
      status: await page.locator('#arp-point-status').textContent(),
      ids: await page.locator('[data-point-event-list-id]').evaluateAll(items => items.map(item => item.dataset.pointEventListId)),
    });
    await page.locator(`[data-point-event-list-id="${firstId}"]`).first().click();
    assert(await page.locator('#arp-point-comment').inputValue() === 'first, "quoted" point event', 'Draft was not restored');

    const sync = await page.evaluate(() => {
      const lines = [...document.querySelectorAll('.arp-panel .selected-guide'), document.querySelector('.arp-point-track .annotation-playhead')].filter(Boolean);
      const positions = lines.map(line => line.getBoundingClientRect().left);
      return {
        modelGuideCount: document.querySelectorAll('.arp-panel .selected-guide').length,
        frameValue: document.getElementById('arp-frame-scrubber-point').value,
        zoomValue: document.getElementById('arp-zoom-timeline')?.value ?? null,
        metadata: document.getElementById('arp-selected-meta')?.textContent ?? '',
        guideSpread: positions.length ? Math.max(...positions) - Math.min(...positions) : null,
      };
    });
    const expectedSelectedIndex = Number(first.review.anchor.frame_index) - 1;
    assert(sync.modelGuideCount > 0, 'No model playhead guide was rendered', sync);
    assert(sync.frameValue === String(expectedSelectedIndex), 'Selected frame did not follow the point event', sync);
    if (sync.zoomValue !== null) assert(sync.zoomValue === String(expectedSelectedIndex), 'Zoom timeline is out of sync', sync);
    assert(sync.metadata.trim().length > 0, 'Selected-frame provenance is empty', sync);
    assert(sync.guideSpread !== null && sync.guideSpread <= 3, 'Timeline playheads are not aligned', sync);

    const desktopFailClosed = await verifyDesktopFailClosed(browser, reportPath);
    const responsive = [];
    for (const width of [1024, 736, 360]) {
      await page.setViewportSize({ width, height: 900 });
      await page.waitForTimeout(60);
      const layout = await page.evaluate(() => ({ overflow: document.documentElement.scrollWidth - document.documentElement.clientWidth, trackWidth: document.getElementById('arp-point-track-host').getBoundingClientRect().width, editorWidth: document.querySelector('.arp-point-editor').getBoundingClientRect().width }));
      assert(layout.overflow <= 2 && layout.trackWidth > 0 && layout.editorWidth > 0, `Responsive layout failed at ${width}px`, layout);
      const screenshot = path.join(outputDir, `rheed-point-labeling-${width}.png`);
      await page.screenshot({ path: screenshot, fullPage: false });
      responsive.push({ width, ...layout, screenshot });
    }
    assert(externalRequests.length === 0, 'Report attempted external network access', externalRequests);
    assert(browserErrors.length === 0, 'Browser errors occurred', browserErrors);
    const result = { playwrightModule: moduleName, report: reportPath, exportedEvents: exported.events.length, sync, desktopFailClosed, responsive, externalRequests, browserErrors };
    fs.writeFileSync(path.join(outputDir, 'rheed-point-labeling-browser-verification.json'), `${JSON.stringify(result, null, 2)}\n`);
    process.stdout.write(`${JSON.stringify(result, null, 2)}\n`);
  } finally { await context.close(); await browser.close(); await localServer.close(); }
}

main().catch(error => { console.error(error.stack || error); process.exitCode = 1; });

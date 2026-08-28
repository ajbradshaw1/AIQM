#!/usr/bin/env node
'use strict';

// Offline browser acceptance for rheed-point-events-v2. Synthetic source
// events are injected into a generated standalone report; no experimental
// archive is copied into the repository or changed by this verifier.
const fs = require('fs');
const http = require('http');
const path = require('path');
const crypto = require('crypto');

function loadPlaywright() {
  const candidates = [
    process.env.PLAYWRIGHT_MODULE,
    'playwright',
    '@playwright/test',
    'playwright-core',
  ].filter(Boolean);
  const failures = [];
  for (const candidate of candidates) {
    try {
      const loaded = require(candidate);
      const chromium = loaded.chromium || (loaded.default && loaded.default.chromium);
      if (chromium) return { chromium, moduleName: candidate };
      failures.push(`${candidate}: no chromium export`);
    } catch (error) {
      failures.push(`${candidate}: ${error.message}`);
    }
  }
  throw new Error(
    `Cannot load Playwright. Set PLAYWRIGHT_MODULE if needed.\n${failures.join('\n')}`,
  );
}

function assert(condition, message, detail) {
  if (!condition) {
    throw new Error(
      `${message}${detail === undefined ? '' : `: ${JSON.stringify(detail)}`}`,
    );
  }
}

const clone = value => JSON.parse(JSON.stringify(value));
const checkpoint = message => process.stdout.write(`[browser-v2] ${message}\n`);

function expectedSegmentId(datasetId, startFrame, boundaryEventIds) {
  const namespace = Buffer.from('877062c884f35bc59bd0e032edcbaeb5', 'hex');
  const identity = JSON.stringify({
    boundary_event_ids: [...boundaryEventIds].sort(),
    session_identity: String(datasetId || ''),
    start_frame: startFrame,
  });
  const digest = crypto.createHash('sha1')
    .update(Buffer.concat([namespace, Buffer.from(identity, 'utf8')])).digest().subarray(0, 16);
  digest[6] = (digest[6] & 0x0f) | 0x50;
  digest[8] = (digest[8] & 0x3f) | 0x80;
  const hex = digest.toString('hex');
  return [hex.slice(0, 8), hex.slice(8, 12), hex.slice(12, 16),
    hex.slice(16, 20), hex.slice(20)].join('-');
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
    return Array.from(
      { length: bytes.length / width },
      (_, index) => bytes[reader](index * width),
    );
  };
  const times = packed('times', 4, 'readFloatLE');
  const heartbeats = packed('heartbeats', 4, 'readUInt32LE');
  const sequences = packed('sequences', 4, 'readUInt32LE');
  const hashMatch = html.match(/const frameHashBytes = decode\('([^']+)'/);
  assert(hashMatch, 'Could not locate packed frame hashes');
  const hashBytes = Buffer.from(hashMatch[1], 'base64');
  const hashes = Array.from(
    { length: hashBytes.length / 32 },
    (_, index) => hashBytes.subarray(index * 32, (index + 1) * 32).toString('hex'),
  );
  assert(times.length >= 6, 'Browser fixture requires at least six saved frames');

  const archiveMember = index => String(config.image_pattern || '')
    .replace('{index}', String(index + 1).padStart(4, '0'));
  const anchor = index => ({
    frame_index: index + 1,
    heartbeat_idx: heartbeats[index],
    elapsed_s: Number(times[index].toFixed(3)),
    captured_at_utc: config.frame_contexts[index].captured_at_utc,
    capture_sequence: sequences[index],
    image_sha256: hashes[index],
    image_sha256_algorithm: 'raw-file-bytes-v1',
    archive_member: archiveMember(index),
  });
  const sourceEvent = ({
    id,
    kind,
    eventIndex,
    frameIndex = eventIndex,
    labels = [],
    decision = 'pending',
  }) => ({
    schema: 'rheed-point-events-v2',
    event_id: id,
    source: {
      kind,
      session_identity: 'browser-v2-fixture',
      source_file: kind === 'auto_capture'
        ? 'auto_capture_events.csv'
        : 'session_metadata.json',
      source_file_sha256: kind === 'auto_capture' ? 'a'.repeat(64) : 'b'.repeat(64),
      source_row_sha256: kind === 'auto_capture' ? 'c'.repeat(64) : 'd'.repeat(64),
      source_row_index: eventIndex,
      source_sequence: sequences[eventIndex],
      original_at_utc: config.frame_contexts[eventIndex].captured_at_utc,
      original_elapsed_s: Number(times[eventIndex].toFixed(3)),
      original_anchor: anchor(frameIndex),
      original_note: kind === 'initial_assumption'
        ? 'Initial 1x1 assumption; grower confirmation required'
        : 'Translation-insensitive image-change candidate',
      legacy_imports: [],
    },
    review: {
      anchor: anchor(frameIndex),
      representative_anchor: null,
      labels,
      candidate_decision: decision,
      comment: '',
      reviewer: '',
      confidence: null,
      disposition: 'active',
      disposition_reason: '',
    },
    status: 'Draft',
    revision_id: `seed-${id}`,
  });

  const initialEvent = sourceEvent({
    id: `initial-assumption-${config.dataset.dataset_id}`,
    kind: 'initial_assumption',
    eventIndex: 0,
    labels: [],
    decision: 'confirmed',
  });
  // The first automatic event deliberately records a click at frame 5 while
  // its saved evidence image is frame 2. Sensor context must follow the event
  // timestamp, not silently substitute the captured-image timestamp.
  const autoConfirmEvent = sourceEvent({
    id: 'auto-candidate-confirm-browser-v2',
    kind: 'auto_capture',
    eventIndex: 4,
    frameIndex: 1,
  });
  const autoRejectEvent = sourceEvent({
    id: 'auto-candidate-reject-browser-v2',
    kind: 'auto_capture',
    eventIndex: 5,
    frameIndex: 5,
    labels: [{
      label_id: 'ignored-pending-htr-label',
      kind: 'reconstruction',
      change: 'appeared',
      value: 'htr',
    }],
  });
  const deletedEvent = sourceEvent({
    id: 'deleted-posthoc-browser-v2',
    kind: 'posthoc',
    eventIndex: 2,
    labels: [{
      label_id: 'ignored-deleted-twinned-label',
      kind: 'reconstruction',
      change: 'appeared',
      value: 'twinned_two_by_one',
    }],
    decision: 'confirmed',
  });
  deletedEvent.review.disposition = 'deleted';
  deletedEvent.review.disposition_reason = 'Synthetic tombstone for inactive-state replay';

  config.annotation_schema = 'rheed-point-events-v2';
  config.point_events = [initialEvent, autoConfirmEvent, autoRejectEvent, deletedEvent];
  config.source_event_revisions = [];
  config.source_event_journal = null;
  config.sensor_context = {
    source_file: 'sensor_log.csv',
    source_file_sha256: 'e'.repeat(64),
    columns: [
      'elapsed_s',
      'pyrometer_temp_C',
      'mistral_v_actual_V',
      'mistral_i_actual_A',
      'pyrometer_age_ms',
      'mistral_age_ms',
      'evap_age_ms',
      'rheed_age_ms',
    ],
    rows: [
      {
        elapsed_s: times[1],
        pyrometer_temp_C: 111.1,
        mistral_v_actual_V: 1.111,
        mistral_i_actual_A: 0.111,
        pyrometer_age_ms: 101,
        mistral_age_ms: 102,
        evap_age_ms: 103,
        rheed_age_ms: 104,
      },
      {
        elapsed_s: times[4],
        pyrometer_temp_C: 777.7,
        mistral_v_actual_V: 4.321,
        mistral_i_actual_A: 0.876,
        pyrometer_age_ms: 11,
        mistral_age_ms: 22,
        evap_age_ms: 33,
        rheed_age_ms: 44,
      },
    ],
  };
  const transformed = html.replace(
    configMatch[0],
    `${configMatch[1]}${JSON.stringify(config)}${configMatch[3]}`,
  );
  return {
    html: transformed,
    config,
    times,
    hashes,
    sequences,
    anchor,
    initialEvent,
    autoConfirmEvent,
    autoRejectEvent,
    deletedEvent,
  };
}

async function startLocalServer(reportPath, options = {}) {
  const root = path.dirname(reportPath);
  const types = {
    '.html': 'text/html; charset=utf-8',
    '.js': 'text/javascript; charset=utf-8',
    '.json': 'application/json; charset=utf-8',
    '.webp': 'image/webp',
    '.png': 'image/png',
  };
  const server = http.createServer(async (request, response) => {
    const url = new URL(request.url, 'http://127.0.0.1');
    if (options.api && url.pathname.startsWith('/api/')) {
      try {
        await options.api(request, response, url);
      } catch (error) {
        response.writeHead(500, { 'Content-Type': 'application/json' });
        response.end(JSON.stringify({ error: error.message }));
      }
      return;
    }
    const relative = decodeURIComponent(
      url.pathname === '/' ? `/${path.basename(reportPath)}` : url.pathname,
    ).replace(/^\/+/, '');
    const target = path.resolve(root, relative);
    if (target !== root && !target.startsWith(`${root}${path.sep}`)) {
      response.writeHead(403).end('forbidden');
      return;
    }
    if (options.reportHtml !== undefined && target === reportPath) {
      response.writeHead(200, {
        'Content-Type': 'text/html; charset=utf-8',
        'Cache-Control': 'no-store',
      });
      response.end(options.reportHtml);
      return;
    }
    fs.stat(target, (error, stat) => {
      if (error || !stat.isFile()) {
        response.writeHead(404).end('not found');
        return;
      }
      response.writeHead(200, {
        'Content-Type': types[path.extname(target).toLowerCase()]
          || 'application/octet-stream',
        'Cache-Control': 'no-store',
      });
      fs.createReadStream(target).pipe(response);
    });
  });
  await new Promise((resolve, reject) => {
    server.once('error', reject);
    server.listen(0, '127.0.0.1', resolve);
  });
  return {
    url: `http://127.0.0.1:${server.address().port}/${encodeURIComponent(path.basename(reportPath))}`,
    close: () => new Promise((resolve, reject) => {
      server.close(error => (error ? reject(error) : resolve()));
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
  await page.waitForFunction(
    expected => Number(document.querySelector('#arp-frame-scrubber-point')?.value) === expected,
    index,
  );
}

async function selectEvent(page, eventId) {
  const button = page.locator(`[data-point-event-list-id="${eventId}"]`).first();
  await button.click();
  await page.waitForFunction(
    expected => (document.querySelector('#arp-point-editor-heading')?.textContent || '')
      .includes(expected),
    eventId,
  );
}

async function downloadText(page, click) {
  const pending = page.waitForEvent('download');
  await click();
  return fs.readFileSync(await (await pending).path(), 'utf8');
}

function parseCsv(text) {
  const rows = [];
  let row = [];
  let field = '';
  let quoted = false;
  const source = text.replace(/^\ufeff/, '');
  for (let index = 0; index < source.length; index += 1) {
    const char = source[index];
    if (quoted) {
      if (char === '"' && source[index + 1] === '"') {
        field += '"';
        index += 1;
      } else if (char === '"') {
        quoted = false;
      } else {
        field += char;
      }
    } else if (char === '"') {
      quoted = true;
    } else if (char === ',') {
      row.push(field);
      field = '';
    } else if (char === '\n') {
      row.push(field.replace(/\r$/, ''));
      rows.push(row);
      row = [];
      field = '';
    } else {
      field += char;
    }
  }
  if (field || row.length) {
    row.push(field.replace(/\r$/, ''));
    rows.push(row);
  }
  const header = rows.shift() || [];
  return rows
    .filter(values => values.some(Boolean))
    .map(values => Object.fromEntries(header.map((name, index) => [name, values[index] || ''])));
}

async function addLabel(page, kind, change, value) {
  const before = await page.locator('[data-point-label-id]').count();
  await page.locator('#arp-point-label-kind').selectOption(kind);
  await page.locator('#arp-point-label-change').selectOption(change);
  await page.locator('#arp-point-label-value').selectOption(value);
  await page.locator('#arp-point-label-add').click();
  await page.waitForFunction(
    expected => document.querySelectorAll('[data-point-label-id]').length === expected,
    before + 1,
  );
}

async function labelRowIdForValue(page, value) {
  return page.locator('[data-point-label-id]').evaluateAll((rows, expected) => {
    const row = rows.find(item => item.querySelector('[data-point-label-field="value"]')?.value === expected);
    return row?.dataset.pointLabelId || '';
  }, value);
}

async function dragMarkerToFrame(page, fixture, eventId, role, targetIndex) {
  const selector = `[data-point-event-id="${eventId}"][data-point-role="${role}"]`;
  let marker;
  let markerBox;
  for (let attempt = 0; attempt < 4; attempt += 1) {
    marker = page.locator(selector).last();
    try {
      await marker.waitFor({ state: 'visible' });
      await marker.scrollIntoViewIfNeeded();
      markerBox = await marker.boundingBox();
      if (markerBox) break;
    } catch (error) {
      if (attempt === 3) throw error;
      await page.waitForTimeout(50);
    }
  }
  const frameBox = await page.locator('#arp-point-track-host [data-chart-frame]').boundingBox();
  assert(markerBox && frameBox, 'Could not resolve draggable marker geometry', {
    eventId,
    role,
    markerBox,
    frameBox,
  });
  const low = Math.min(...fixture.times);
  const high = Math.max(...fixture.times);
  const padding = Math.max(1, (high - low) * 0.005);
  const fraction = (fixture.times[targetIndex] - (low - padding))
    / ((high + padding) - (low - padding));
  const targetX = frameBox.x + fraction * frameBox.width;
  const targetY = markerBox.y + markerBox.height / 2;
  await page.mouse.move(markerBox.x + markerBox.width / 2, targetY);
  await page.mouse.down();
  await page.mouse.move(targetX, targetY, { steps: 12 });
  await page.mouse.up();
  const evidenceTerm = role === 'review' ? 'Review event point' : 'Interval Anchor';
  try {
    await page.waitForFunction(
      ({ term, ordinal }) => {
        const host = document.querySelector('#arp-point-evidence');
        const dt = [...(host?.querySelectorAll('dt') || [])]
          .find(item => item.textContent.trim() === term);
        return (dt?.nextElementSibling?.textContent || '').includes(`#${ordinal}`);
      },
      { term: evidenceTerm, ordinal: targetIndex + 1 },
      { timeout: 3000 },
    );
  } catch (error) {
    const diagnostic = await page.evaluate(({ markerSelector, term }) => {
      const host = document.querySelector('#arp-point-evidence');
      const evidence = Object.fromEntries([...host.querySelectorAll('dt')].map(item => [
        item.textContent.trim(),
        item.nextElementSibling?.textContent || '',
      ]));
      const markerNode = document.querySelector(markerSelector);
      return {
        evidence,
        markerTransform: markerNode?.getAttribute('transform') || null,
        status: document.querySelector('#arp-point-status')?.textContent || '',
        selectedFrame: document.querySelector('#arp-frame-scrubber-point')?.value || '',
        requestedTerm: term,
      };
    }, { markerSelector: selector, term: evidenceTerm });
    throw new Error(`Drag did not snap to frame #${targetIndex + 1}: ${JSON.stringify(diagnostic)}`, {
      cause: error,
    });
  }
}

function installNoExternalRoute(context, externalRequests) {
  return context.route(/^https?:/i, route => {
    const url = new URL(route.request().url());
    if (url.hostname === '127.0.0.1' || url.hostname === 'localhost') {
      route.continue();
    } else {
      externalRequests.push(route.request().url());
      route.abort('internetdisconnected');
    }
  });
}

function desktopApi(fixture, token) {
  const events = clone([
    fixture.initialEvent,
    fixture.autoConfirmEvent,
    fixture.autoRejectEvent,
  ]);
  const revisions = [];
  let revisionCounter = 0;
  let labelCounter = 0;
  const send = (response, status, payload) => {
    response.writeHead(status, {
      'Content-Type': 'application/json',
      'Cache-Control': 'no-store',
    });
    response.end(JSON.stringify(payload));
  };
  const readBody = async request => {
    const chunks = [];
    for await (const chunk of request) chunks.push(chunk);
    return JSON.parse(Buffer.concat(chunks).toString('utf8') || '{}');
  };
  const reopenOnEdit = event => {
    if (event.status === 'Complete') event.status = 'Draft';
  };
  const apply = command => {
    const event = events.find(item => item.event_id === command.event_id);
    if (!event) throw new Error(`missing event ${command.event_id}`);
    const review = event.review;
    const changes = command.changes || {};
    const before = clone(event);
    switch (command.action) {
      case 'edit':
        review.comment = String(changes.comment || '');
        review.reviewer = String(changes.reviewer || '');
        review.confidence = changes.confidence ?? null;
        reopenOnEdit(event);
        break;
      case 'add_label':
        labelCounter += 1;
        review.labels.push({
          label_id: `desktop-label-${labelCounter}`,
          ...clone(changes.label || {}),
        });
        reopenOnEdit(event);
        break;
      case 'edit_label':
        review.labels = review.labels.map(label => label.label_id === changes.label_id
          ? { label_id: label.label_id, ...clone(changes.label || {}) }
          : label);
        reopenOnEdit(event);
        break;
      case 'remove_label':
        review.labels = review.labels.filter(label => label.label_id !== changes.label_id);
        reopenOnEdit(event);
        break;
      case 'set_candidate_decision':
        review.candidate_decision = changes.decision;
        if (changes.decision === 'rejected') {
          review.labels = [];
          review.representative_anchor = null;
        }
        reopenOnEdit(event);
        break;
      case 'move_anchor':
        review.anchor = clone(changes.anchor);
        review.representative_anchor = null;
        reopenOnEdit(event);
        break;
      case 'move_representative_anchor':
        review.representative_anchor = clone(changes.representative_anchor);
        reopenOnEdit(event);
        break;
      case 'complete':
        event.status = 'Complete';
        break;
      case 'reopen':
        event.status = 'Draft';
        break;
      default:
        throw new Error(`unsupported desktop test action ${command.action}`);
    }
    revisionCounter += 1;
    const revisionId = `desktop-revision-${revisionCounter}`;
    event.revision_id = revisionId;
    const revision = {
      schema_version: 'rheed-point-events-v2',
      revision_id: revisionId,
      base_revision_id: command.base_revision_id || '',
      actor: command.actor,
      at_utc: new Date().toISOString(),
      action: command.action,
      event_id: event.event_id,
      before,
      after: clone(event),
    };
    revisions.push(revision);
    return { event: clone(event), revision };
  };
  return async (request, response, url) => {
    if (url.searchParams.get('bridge_token') !== token) {
      send(response, 403, { error: 'forbidden' });
      return;
    }
    if (url.pathname === '/api/status') {
      send(response, 200, {
        desktop: true,
        equalizer_available: false,
        revision_persistence_available: true,
        event_state_available: true,
      });
      return;
    }
    if (url.pathname === '/api/events' && request.method === 'GET') {
      send(response, 200, {
        ok: true,
        events: clone(events),
        revisions: clone(revisions),
        unfinished_count: events.filter(item => item.status !== 'Complete').length,
        annotation_set: {
          annotation_set_id: 'desktop-browser-v2-annotation-set',
          reviewer: '',
        },
      });
      return;
    }
    if (url.pathname === '/api/revisions' && request.method === 'POST') {
      const command = await readBody(request);
      send(response, 200, apply(command));
      return;
    }
    send(response, 404, { error: 'unknown endpoint' });
  };
}

async function changeTextField(page, selector, value) {
  const input = page.locator(selector);
  await input.fill(value);
  await input.dispatchEvent('change');
  await page.waitForFunction(
    target => !document.querySelector(target)?.disabled,
    selector,
  );
}

async function verifyDesktopCompletion(browser, reportPath, fixture, externalRequests) {
  checkpoint('checking desktop Complete/Reopen gates');
  const token = 'browser-test-v2-bridge-token-0123456789abcdef';
  const server = await startLocalServer(reportPath, {
    reportHtml: fixture.html,
    api: desktopApi(fixture, token),
  });
  const context = await browser.newContext({
    acceptDownloads: true,
    viewport: { width: 1024, height: 900 },
  });
  await installNoExternalRoute(context, externalRequests);
  const page = await context.newPage();
  const pageErrors = [];
  page.on('pageerror', error => pageErrors.push(error.stack || error.message));
  page.on('console', message => {
    if (message.type() === 'error') pageErrors.push(`console: ${message.text()}`);
  });
  try {
    await page.goto(`${server.url}?bridge_token=${encodeURIComponent(token)}`, {
      waitUntil: 'load',
    });
    await waitForEditor(page);
    await page.waitForFunction(
      () => /Desktop Labeler connected/i.test(
        document.querySelector('#arp-point-status')?.textContent || '',
      ),
      null,
      { timeout: 10000 },
    );
    assert(!/equalizer/i.test(await page.locator('body').innerText()),
      'Desktop labeling UI exposed Equalizer');
    assert(!await page.locator('#arp-mark-in').isVisible()
      && !await page.locator('#arp-mark-out').isVisible(),
    'Desktop labeling UI exposed legacy segment controls');

    await selectEvent(page, fixture.initialEvent.event_id);
    assert(!await page.locator('#arp-point-candidate-decision').isVisible()
      && !await page.locator('.arp-point-label-editor').isVisible()
      && !await page.locator('#arp-point-dismiss').isVisible(),
    'Initial-state audit exposed candidate, delta-label, or dismissal controls');
    assert(await page.locator('#arp-point-complete').isDisabled(),
      'Initial-state audit without reviewer or Anchor enabled Complete');
    await changeTextField(page, '#arp-point-reviewer', 'Desktop Reviewer');
    assert(await page.locator('#arp-point-complete').isDisabled(),
      'Initial-state reviewer without first-segment Anchor enabled Complete');
    await setFrame(page, 0);
    await page.locator('#arp-point-anchor').click();
    await page.waitForSelector(
      `[data-point-event-id="${fixture.initialEvent.event_id}"]`
        + '[data-point-role="representative-anchor"]',
    );
    assert(!await page.locator('#arp-point-complete').isDisabled(),
      'Initial-state reviewer plus first-segment Anchor did not enable Complete');
    await page.locator('#arp-point-complete').click();
    await page.waitForFunction(
      id => (document.querySelector('#arp-point-editor-heading')?.textContent || '')
        .includes(`· Complete · ${id}`),
      fixture.initialEvent.event_id,
    );

    await selectEvent(page, fixture.autoConfirmEvent.event_id);
    assert(await page.locator('#arp-point-complete').isDisabled(),
      'Pending candidate without reviewer, labels, and Anchor enabled Complete');
    await changeTextField(page, '#arp-point-reviewer', 'Desktop Reviewer');
    assert(await page.locator('#arp-point-complete').isDisabled(),
      'Reviewer alone enabled Complete');
    await page.locator('#arp-point-candidate-decision').selectOption('confirmed');
    await page.waitForFunction(
      () => /confirmed as a real change/i.test(
        document.querySelector('#arp-point-status')?.textContent || '',
      ),
    );
    assert(await page.locator('#arp-point-complete').isDisabled(),
      'Confirmed candidate without labels and Anchor enabled Complete');
    await addLabel(page, 'reconstruction', 'appeared', 'rt13');
    assert(await page.locator('#arp-point-complete').isDisabled(),
      'Candidate without representative Anchor enabled Complete');
    await setFrame(page, 3);
    await page.locator('#arp-point-anchor').click();
    await page.waitForSelector(
      `[data-point-event-id="${fixture.autoConfirmEvent.event_id}"]`
        + '[data-point-role="representative-anchor"]',
    );
    assert(!await page.locator('#arp-point-complete').isDisabled(),
      'Reviewer + label + Anchor + confirmed candidate did not enable Complete');
    assert(await page.locator('#arp-point-comment').inputValue() === '',
      'Desktop completion fixture unexpectedly required a comment');
    await page.locator('#arp-point-complete').click();
    await page.waitForFunction(
      id => (document.querySelector('#arp-point-editor-heading')?.textContent || '')
        .includes(`· Complete · ${id}`),
      fixture.autoConfirmEvent.event_id,
    );

    const labelRow = page.locator('[data-point-label-id]').first();
    await labelRow.locator('[data-point-label-field="value"]').selectOption('htr');
    await page.waitForFunction(
      id => (document.querySelector('#arp-point-editor-heading')?.textContent || '')
        .includes(`· Draft · ${id}`),
      fixture.autoConfirmEvent.event_id,
    );
    assert(!await page.locator('#arp-point-complete').isDisabled(),
      'Editing a completed event did not return an otherwise valid event to Draft');
    await page.locator('#arp-point-complete').click();
    await page.waitForFunction(
      id => (document.querySelector('#arp-point-editor-heading')?.textContent || '')
        .includes(`· Complete · ${id}`),
      fixture.autoConfirmEvent.event_id,
    );
    await page.locator('#arp-point-reopen').click();
    await page.waitForFunction(
      id => (document.querySelector('#arp-point-editor-heading')?.textContent || '')
        .includes(`· Draft · ${id}`),
      fixture.autoConfirmEvent.event_id,
    );

    await selectEvent(page, fixture.autoRejectEvent.event_id);
    await changeTextField(page, '#arp-point-reviewer', 'Desktop Reviewer');
    await page.locator('#arp-point-candidate-decision').selectOption('rejected');
    await page.waitForFunction(
      () => /source evidence was preserved/i.test(
        document.querySelector('#arp-point-status')?.textContent || '',
      ),
    );
    assert(await page.locator('[data-point-label-id]').count() === 0,
      'Rejected candidate unexpectedly acquired semantic labels');
    assert(await page.locator(
      `[data-point-event-id="${fixture.autoRejectEvent.event_id}"]`
        + '[data-point-role="representative-anchor"]',
    ).count() === 0, 'Rejected candidate unexpectedly required an Anchor');
    assert(!await page.locator('#arp-point-complete').isDisabled(),
      'Reviewer + rejected candidate did not enable Complete');
    await page.locator('#arp-point-complete').click();
    await page.waitForFunction(
      id => (document.querySelector('#arp-point-editor-heading')?.textContent || '')
        .includes(`· Complete · ${id}`),
      fixture.autoRejectEvent.event_id,
    );

    const exported = JSON.parse(await downloadText(
      page,
      () => page.locator('#arp-point-export-json').click(),
    ));
    const confirmed = exported.events.find(
      item => item.event_id === fixture.autoConfirmEvent.event_id,
    );
    const initial = exported.events.find(
      item => item.event_id === fixture.initialEvent.event_id,
    );
    const rejected = exported.events.find(
      item => item.event_id === fixture.autoRejectEvent.event_id,
    );
    assert(confirmed.status === 'Draft' && confirmed.review.comment === '',
      'Reopen or optional-comment behavior did not round-trip', confirmed);
    assert(rejected.status === 'Complete'
      && rejected.review.candidate_decision === 'rejected',
    'Rejected completion did not round-trip', rejected);
    assert(initial.status === 'Complete'
      && initial.review.labels.length === 0
      && initial.review.candidate_decision === 'confirmed'
      && initial.review.representative_anchor?.frame_index === 1,
    'Initial-state audit did not Complete without change labels', initial);
    assert(exported.segments[0]?.status === 'Complete'
      && exported.segments[0].state.reconstructions.join('|') === 'one_by_one'
      && exported.segments[0].state.clarity === 'unknown'
      && exported.segments[0].anchor?.frame_index === 1,
    'Completed first derived interval did not own the initial-state Anchor',
    exported.segments[0]);
    assert(pageErrors.length === 0, 'Desktop browser errors occurred', pageErrors);
    checkpoint('desktop Complete/Reopen gates passed');
    return {
      confirmedStatusAfterReopen: confirmed.status,
      rejectedStatus: rejected.status,
      rejectedLabels: rejected.review.labels.length,
    };
  } finally {
    await context.close();
    await server.close();
  }
}

async function verifyStaticEditing(page, fixture, outputDir) {
  checkpoint('checking static/offline event editing');
  const bounds = await page.locator('#arp-frame-scrubber-point').evaluate(input => ({
    min: Number(input.min),
    max: Number(input.max),
  }));
  assert(bounds.min === 0 && bounds.max >= 5, 'Unexpected saved-frame scrubber bounds', bounds);
  assert(!await page.locator('#arp-legacy-segment-editor').isVisible(),
    'Legacy segment editor is visible');
  assert(!await page.locator('#arp-mark-in').isVisible()
    && !await page.locator('#arp-mark-out').isVisible(),
  'Mark In/Out segment controls are visible');
  const visibleText = await page.locator('body').innerText();
  assert(!/equalizer/i.test(visibleText), 'Equalizer is visible in the v2 labeling UI');
  const laneLabels = await page.locator('.arp-point-track .point-lane-label').allTextContents();
  assert(JSON.stringify(laneLabels) === JSON.stringify([
    'Event points', 'Derived state intervals', 'Interval anchors',
  ]), 'Point-event timeline does not expose the three required lanes', laneLabels);
  const baselineInterval = page.locator(
    '[data-point-role="derived-state-interval"][data-baseline="true"]',
  );
  assert(await baselineInterval.count() === 1,
    'Initial derived-state interval is missing');
  const baselineState = await baselineInterval.evaluate(element => ({ ...element.dataset }));
  assert(baselineState.stateReconstructions === 'one_by_one'
    && baselineState.stateQuality === 'unknown'
    && baselineState.boundaryEventIds === '',
  'Pending, rejected, or deleted fixture events changed the initial state', baselineState);
  assert(!/htr|twinned_2x1/i.test(baselineState.stateText || ''),
    'Inactive fixture labels leaked into cumulative state', baselineState);
  assert(await page.locator(
    `[data-point-event-id="${fixture.autoRejectEvent.event_id}"][data-point-role="review"]`,
  ).count() === 1, 'Pending automatic event point is not visible as evidence');

  await selectEvent(page, fixture.initialEvent.event_id);
  const initialHeading = await page.locator('#arp-point-editor-heading').textContent();
  assert(/Initial state: 1×1 present, clarity unknown.*Draft/.test(initialHeading),
    'Initial-state audit item was not shown explicitly', initialHeading);
  assert(!await page.locator('#arp-point-candidate-decision').isVisible(),
    'Initial-state audit item exposed a candidate decision');
  assert(!await page.locator('.arp-point-label-editor').isVisible(),
    'Initial-state audit item exposed change-label editing');
  assert(await page.locator('[data-point-label-id]').count() === 0,
    'Initial-state audit item contains a change delta');
  assert(!await page.locator('#arp-point-dismiss').isVisible(),
    'Initial-state audit item can be dismissed');
  const initialEvidenceText = await page.locator('#arp-point-evidence').textContent();
  assert(/Initial state.*1×1 present.*clarity unknown/i.test(initialEvidenceText),
    'Fixed initial state is not explained in the evidence panel', initialEvidenceText);

  await selectEvent(page, fixture.autoConfirmEvent.event_id);
  const contextHost = page.locator('#arp-point-context');
  const lookup = await contextHost.evaluate(element => ({ ...element.dataset }));
  const contextText = await contextHost.textContent();
  assert(Math.abs(Number(lookup.lookupElapsedS) - fixture.times[4]) < 0.01,
    'Sensor lookup did not preserve the automatic event time', lookup);
  for (const expected of [
    '777.7 °C',
    '4.321 V',
    '0.876 A',
    '11.0 ms',
    '22.0 ms',
    '33.0 ms',
    '44.0 ms',
  ]) {
    assert(contextText.includes(expected),
      `Sensor context omitted ${expected}`, contextText);
  }
  assert(!contextText.includes('111.1 °C'),
    'Sensor context silently used the saved evidence-image timestamp');
  checkpoint('initial assumption and sensor context passed');

  await page.locator('#arp-point-candidate-decision').selectOption('confirmed');
  await changeTextField(page, '#arp-point-reviewer', 'Offline Browser Reviewer');
  assert(await page.locator('#arp-point-comment').inputValue() === '',
    'Comment is not optional for the synthetic automatic event');
  await addLabel(page, 'reconstruction', 'appeared', 'rt13');
  await addLabel(page, 'reconstruction', 'disappeared', 'one_by_one');
  await addLabel(page, 'pattern_clarity', 'became', 'good');
  await addLabel(page, 'reconstruction', 'appeared', 'c_six_by_two');

  const rt13Id = await labelRowIdForValue(page, 'rt13');
  assert(rt13Id, 'Could not find newly added RT13 appearance label');
  const rt13Row = page.locator(`[data-point-label-id="${rt13Id}"]`);
  await rt13Row.locator('[data-point-label-field="value"]').selectOption('htr');
  await page.waitForFunction(
    id => document.querySelector(`[data-point-label-id="${id}"]`)
      ?.querySelector('[data-point-label-field="value"]')?.value === 'htr',
    rt13Id,
  );
  await page.locator(`[data-point-label-id="${rt13Id}"]`)
    .locator('[data-point-label-field="value"]').selectOption('rt13');
  await page.waitForFunction(
    id => document.querySelector(`[data-point-label-id="${id}"]`)
      ?.querySelector('[data-point-label-field="value"]')?.value === 'rt13',
    rt13Id,
  );

  const clarityId = await labelRowIdForValue(page, 'good');
  assert(clarityId, 'Could not find Good clarity label');
  await page.locator(`[data-point-label-id="${clarityId}"]`)
    .locator('[data-point-label-field="value"]').selectOption('bad');
  await page.waitForFunction(
    id => document.querySelector(`[data-point-label-id="${id}"]`)
      ?.querySelector('[data-point-label-field="value"]')?.value === 'bad',
    clarityId,
  );

  const removedId = await labelRowIdForValue(page, 'c_six_by_two');
  assert(removedId, 'Could not find temporary c(6×2) label');
  await page.locator(`[data-point-label-id="${removedId}"]`)
    .locator('[data-point-label-remove]').click();
  await page.waitForFunction(
    id => !document.querySelector(`[data-point-label-id="${id}"]`),
    removedId,
  );

  await addLabel(page, 'reconstruction', 'disappeared', 'htr');
  const absentHtrId = await labelRowIdForValue(page, 'htr');
  assert(absentHtrId, 'Could not find temporary absent-HTR transition');
  await page.waitForFunction(
    () => document.querySelector('#arp-point-state-diagnostic')
      ?.getAttribute('data-state-valid') === 'false',
  );
  const contradictionText = await page.locator('#arp-point-state-diagnostic').textContent();
  assert(/HTR cannot disappear because it is not present/i.test(contradictionText),
    'Cross-event state contradiction was not explained', contradictionText);
  assert(await page.locator('#arp-point-complete').isDisabled(),
    'State contradiction did not disable Complete');
  const invalidInterval = page.locator(
    `[data-point-role="derived-state-interval"]`
      + `[data-boundary-event-ids*="${fixture.autoConfirmEvent.event_id}"]`,
  );
  const invalidState = await invalidInterval.evaluate(element => ({ ...element.dataset }));
  assert(invalidState.stateValid === 'false'
    && invalidState.stateReconstructions === 'one_by_one'
    && invalidState.stateQuality === 'unknown',
  'Contradictory same-frame changes were not rejected atomically', invalidState);
  await page.locator(`[data-point-label-id="${absentHtrId}"]`)
    .locator('[data-point-label-remove]').click();
  await page.waitForFunction(
    () => document.querySelector('#arp-point-state-diagnostic')
      ?.getAttribute('data-state-valid') === 'true',
  );
  const recoveredState = await page.locator(
    `[data-point-role="derived-state-interval"]`
      + `[data-boundary-event-ids*="${fixture.autoConfirmEvent.event_id}"]`,
  ).evaluate(element => ({ ...element.dataset }));
  assert(recoveredState.stateReconstructions === 'rt13'
    && recoveredState.stateQuality === 'bad',
  'Resolving the contradiction did not restore cumulative state', recoveredState);

  const beforeRejectedSemantics = JSON.parse(await downloadText(
    page,
    () => page.locator('#arp-point-export-json').click(),
  ));
  const beforeRejectedConfirmed = beforeRejectedSemantics.events.find(
    item => item.event_id === fixture.autoConfirmEvent.event_id,
  );
  const beforeRejectedRevisions = clone(beforeRejectedSemantics.revisions);

  await page.locator('#arp-point-label-kind').selectOption('reconstruction');
  await page.locator('#arp-point-label-change').selectOption('disappeared');
  await page.locator('#arp-point-label-value').selectOption('rt13');
  await page.locator('#arp-point-label-add').click();
  await page.waitForFunction(
    () => /cannot both appear and disappear/i.test(
      document.querySelector('#arp-point-status')?.textContent || '',
    ),
  );
  assert(await page.locator('[data-point-label-id]').count() === 3,
    'Rejected −RT13 addition changed the label rows');

  await page.locator('#arp-point-label-kind').selectOption('pattern_clarity');
  await page.locator('#arp-point-label-change').selectOption('became');
  await page.locator('#arp-point-label-value').selectOption('good');
  await page.locator('#arp-point-label-add').click();
  await page.waitForFunction(
    () => /only one Good\/Bad clarity change/i.test(
      document.querySelector('#arp-point-status')?.textContent || '',
    ),
  );
  assert(await page.locator('[data-point-label-id]').count() === 3,
    'Rejected second clarity addition changed the label rows');

  const disappearedId = await labelRowIdForValue(page, 'one_by_one');
  assert(disappearedId, 'Could not find −1×1 label for conflict-edit test');
  await page.locator(`[data-point-label-id="${disappearedId}"]`)
    .locator('[data-point-label-field="value"]').selectOption('rt13');
  await page.waitForFunction(
    () => /cannot both appear and disappear/i.test(
      document.querySelector('#arp-point-status')?.textContent || '',
    ),
  );
  assert(await page.locator(`[data-point-label-id="${disappearedId}"]`)
    .locator('[data-point-label-field="value"]').inputValue() === 'one_by_one',
  'Rejected −1×1→−RT13 edit was not rolled back in the UI');

  const afterRejectedSemantics = JSON.parse(await downloadText(
    page,
    () => page.locator('#arp-point-export-json').click(),
  ));
  const afterRejectedConfirmed = afterRejectedSemantics.events.find(
    item => item.event_id === fixture.autoConfirmEvent.event_id,
  );
  assert(JSON.stringify(afterRejectedConfirmed) === JSON.stringify(beforeRejectedConfirmed),
    'Rejected semantic-label attempts mutated the event', {
      before: beforeRejectedConfirmed,
      after: afterRejectedConfirmed,
    });
  assert(JSON.stringify(afterRejectedSemantics.revisions)
    === JSON.stringify(beforeRejectedRevisions),
  'Rejected semantic-label attempts appended audit revisions');
  checkpoint('contradictory reconstruction and duplicate clarity attempts were rejected atomically');

  await dragMarkerToFrame(
    page,
    fixture,
    fixture.autoConfirmEvent.event_id,
    'review',
    3,
  );
  await setFrame(page, 0);
  await page.locator('#arp-point-anchor').click();
  await page.waitForFunction(
    () => /Anchor blocked: choose a saved frame inside/i.test(
      document.querySelector('#arp-point-status')?.textContent || '',
    ),
  );
  assert(await page.locator(
    `[data-point-event-id="${fixture.autoConfirmEvent.event_id}"]`
      + '[data-point-role="representative-anchor"]',
  ).count() === 0, 'Out-of-interval Anchor was stored or rendered');
  await setFrame(page, 5);
  await page.locator('#arp-point-anchor').click();
  await page.waitForSelector(
    `[data-point-event-id="${fixture.autoConfirmEvent.event_id}"]`
      + '[data-point-role="representative-anchor"]',
  );
  await dragMarkerToFrame(
    page,
    fixture,
    fixture.autoConfirmEvent.event_id,
    'representative-anchor',
    4,
  );
  checkpoint('semantic labels and draggable event/Anchor passed');

  await selectEvent(page, fixture.initialEvent.event_id);
  await changeTextField(page, '#arp-point-reviewer', 'Offline Browser Reviewer');
  await setFrame(page, 2);
  assert(!await page.locator('#arp-point-anchor').isDisabled(),
    'Initial-state audit did not allow a first-interval Anchor');
  await page.locator('#arp-point-anchor').click();
  await page.waitForSelector(
    `[data-point-event-id="${fixture.initialEvent.event_id}"]`
      + '[data-point-role="representative-anchor"]',
  );
  assert(await page.locator('[data-point-label-id]').count() === 0,
    'Initial-state audit acquired semantic label rows');
  const initialEvidence = await page.locator('#arp-point-evidence').evaluate(host => {
    const terms = [...host.querySelectorAll('dt')];
    return Object.fromEntries(terms.map(term => [
      term.textContent.trim(),
      term.nextElementSibling?.textContent.trim() || '',
    ]));
  });
  assert(/^#3 /.test(initialEvidence['Interval Anchor']),
    'Initial-state audit did not own the first-interval Anchor', initialEvidence);
  checkpoint('initial-state audit owns the first segment without candidate or delta controls');

  await selectEvent(page, fixture.autoRejectEvent.event_id);
  await changeTextField(page, '#arp-point-reviewer', 'Offline Browser Reviewer');
  await page.locator('#arp-point-candidate-decision').selectOption('rejected');
  await page.waitForFunction(
    () => /source evidence was preserved/i.test(
      document.querySelector('#arp-point-status')?.textContent || '',
    ),
  );

  await setFrame(page, 5);
  await page.locator('#arp-point-add').click();
  const firstPosthocId = (await page.locator('#arp-point-editor-heading').textContent())
    .split(' · ').pop();
  await addLabel(page, 'reconstruction', 'appeared', 'c_six_by_two');
  await page.locator('#arp-point-add').click();
  const secondPosthocId = (await page.locator('#arp-point-editor-heading').textContent())
    .split(' · ').pop();
  await addLabel(page, 'reconstruction', 'disappeared', 'rt13');
  assert(firstPosthocId && secondPosthocId && firstPosthocId !== secondPosthocId,
    'Two posthoc events at one saved frame did not receive distinct IDs', {
      firstPosthocId,
      secondPosthocId,
    });
  const sameFrameInterval = page.locator(
    '[data-point-role="derived-state-interval"][data-start-index="5"]',
  );
  const sameFrameState = await sameFrameInterval.evaluate(element => ({ ...element.dataset }));
  assert(sameFrameState.stateValid === 'true'
    && sameFrameState.stateReconstructions === 'c_six_by_two'
    && sameFrameState.stateQuality === 'bad'
    && sameFrameState.boundaryEventIds.split('|').sort().join('|')
      === [firstPosthocId, secondPosthocId].sort().join('|'),
  'Same-frame events were not applied atomically into one complete state', sameFrameState);
  await setFrame(page, 5);
  await page.locator('#arp-point-anchor').click();
  const sameFrameAnchor = page.locator(
    '[data-point-role="representative-anchor"][data-interval-start-index="5"]',
  );
  await sameFrameAnchor.waitFor({ state: 'visible' });
  assert(await sameFrameAnchor.count() === 1,
    'One atomic same-frame interval rendered more than one Anchor');

  const jsonText = await downloadText(
    page,
    () => page.locator('#arp-point-export-json').click(),
  );
  const exported = JSON.parse(jsonText);
  assert(exported.schema_version === 'rheed-point-events-v2',
    'Unexpected point-event schema', exported.schema_version);
  assert(exported.document_type === 'ai4mbe_rheed_point_event_annotations',
    'Unexpected point-event document type');
  assert(!/equalizer/i.test(JSON.stringify(exported)),
    'v2 point-event JSON still exposes Equalizer data');
  assert(exported.dataset.dataset_id, 'Missing dataset identity');
  assert(/^[0-9a-f]{64}$/i.test(exported.dataset.source_archive_sha256),
    'Missing source-archive SHA-256');
  assert(/^sha256:[0-9a-f]{64}$/i.test(exported.dataset.ordered_frame_fingerprint),
    'Missing ordered-frame fingerprint');
  assert(/^sha256:[0-9a-f]{64}$/i.test(exported.dataset.model_context_fingerprint),
    'Missing model-context fingerprint');
  assert(JSON.stringify(exported.initial_state) === JSON.stringify({
    reconstructions: ['one_by_one'], clarity: 'unknown',
  }), 'Export omitted or changed the fixed initial state', exported.initial_state);
  assert(Array.isArray(exported.segments) && exported.segments.length === 3,
    'Point events were not materialized into three derived intervals', exported.segments);

  const initial = exported.events.find(item => item.event_id === fixture.initialEvent.event_id);
  assert(initial?.status === 'Draft'
    && initial.review.candidate_decision === 'confirmed'
    && initial.review.labels.length === 0
    && initial.review.representative_anchor?.frame_index === 3,
  'Initial-state audit did not retain only its first-interval Anchor', initial);
  assert(JSON.stringify(initial.source) === JSON.stringify(fixture.initialEvent.source),
    'Reviewing the initial state changed immutable source evidence', {
      expected: fixture.initialEvent.source,
      actual: initial.source,
    });
  assert(!exported.revisions.some(item => item.event_id === fixture.initialEvent.event_id
    && ['set_candidate_decision', 'add_label', 'edit_label', 'remove_label', 'dismiss']
      .includes(item.action)),
  'Initial-state audit accepted a candidate, delta-label, or dismissal revision');
  const confirmed = exported.events.find(
    item => item.event_id === fixture.autoConfirmEvent.event_id,
  );
  assert(confirmed?.review.candidate_decision === 'confirmed',
    'Confirmed automatic candidate did not round-trip', confirmed);
  assert(confirmed.review.comment === '', 'Optional empty comment did not round-trip');
  assert(confirmed.review.reviewer === 'Offline Browser Reviewer',
    'Reviewer did not round-trip');
  assert(confirmed.review.anchor.frame_index === 4,
    'Dragged event diamond did not snap to frame 4', confirmed.review.anchor);
  assert(confirmed.review.representative_anchor?.frame_index === 5,
    'Dragged Anchor star did not snap to frame 5', confirmed.review.representative_anchor);
  const semanticTriples = confirmed.review.labels.map(
    label => `${label.kind}:${label.change}:${label.value}`,
  );
  for (const expected of [
    'reconstruction:appeared:rt13',
    'reconstruction:disappeared:one_by_one',
    'pattern_clarity:became:bad',
  ]) {
    assert(semanticTriples.includes(expected),
      `Edited semantic label missing: ${expected}`, semanticTriples);
  }
  assert(!semanticTriples.some(value => value.includes('c_six_by_two')),
    'Removed reconstruction label remained in JSON', semanticTriples);
  const rejected = exported.events.find(
    item => item.event_id === fixture.autoRejectEvent.event_id,
  );
  assert(rejected?.review.candidate_decision === 'rejected',
    'Rejected automatic candidate did not round-trip', rejected);

  const sameTime = exported.events.filter(item => [firstPosthocId, secondPosthocId]
    .includes(item.event_id));
  assert(sameTime.length === 2
    && sameTime.every(item => item.review.anchor.frame_index === 6),
  'Same-time posthoc events were not preserved', sameTime);
  const [initialSegment, confirmedSegment, sameFrameSegment] = exported.segments;
  assert(initialSegment.start_frame_index === 1
    && initialSegment.end_frame_index_exclusive === 4
    && initialSegment.frame_count === 3
    && initialSegment.boundary_event_ids.length === 0
    && initialSegment.state.reconstructions.join('|') === 'one_by_one'
    && initialSegment.state.clarity === 'unknown'
    && initialSegment.anchor?.frame_index === 3,
  'Initial derived interval is not complete-state training data', initialSegment);
  assert(confirmedSegment.start_frame_index === 4
    && confirmedSegment.end_frame_index_exclusive === 6
    && confirmedSegment.frame_count === 2
    && confirmedSegment.boundary_event_ids.join('|') === fixture.autoConfirmEvent.event_id
    && confirmedSegment.state.reconstructions.join('|') === 'rt13'
    && confirmedSegment.state.clarity === 'bad'
    && confirmedSegment.anchor?.frame_index === 5,
  'Confirmed event did not create the expected complete-state interval', confirmedSegment);
  assert(sameFrameSegment.start_frame_index === 6
    && sameFrameSegment.end_frame_index_exclusive === 7
    && sameFrameSegment.frame_count === 1
    && sameFrameSegment.boundary_event_ids.slice().sort().join('|')
      === [firstPosthocId, secondPosthocId].sort().join('|')
    && sameFrameSegment.state.reconstructions.join('|') === 'c_six_by_two'
    && sameFrameSegment.state.clarity === 'bad'
    && sameFrameSegment.anchor?.frame_index === 6,
  'Atomic same-frame boundary did not materialize the expected final interval', sameFrameSegment);
  assert(exported.segments.every(item => item.segment_id === expectedSegmentId(
    exported.dataset.dataset_id,
    item.start_frame_index,
    item.boundary_event_ids,
  ) && item.status === 'Draft'),
  'Derived segments lack deterministic IDs or Draft status', exported.segments);
  assert(await page.locator(
    `#arp-point-unfinished [data-point-event-list-id="${fixture.initialEvent.event_id}"]`,
  ).count() === 1, 'Initial-state Draft is missing from Unfinished queue');
  assert(exported.revisions.some(item => item.action === 'add_label')
    && exported.revisions.some(item => item.action === 'edit_label')
    && exported.revisions.some(item => item.action === 'remove_label')
    && exported.revisions.some(item => item.action === 'move_anchor')
    && exported.revisions.some(item => item.action === 'move_representative_anchor')
    && exported.revisions.some(item => item.action === 'set_candidate_decision'),
  'Expected v2 edits are missing from revision history',
  exported.revisions.map(item => item.action));

  const csvText = await downloadText(
    page,
    () => page.locator('#arp-point-export-csv').click(),
  );
  const csvRows = parseCsv(csvText);
  assert(csvRows.length === exported.segments.length
    && csvRows.every(row => row.schema_version === 'rheed-point-events-v2'),
  'CSV did not export one v2 row per derived interval', csvRows);
  const csvInitial = csvRows.find(row => row.start_frame_index === '1');
  assert(csvInitial?.end_frame_index_exclusive === '4'
    && csvInitial.frame_count === '3'
    && csvInitial.reconstruction_presence === 'one_by_one'
    && csvInitial.clarity === 'unknown'
    && csvInitial.anchor_frame_index === '3'
    && csvInitial.anchor_frame_sha256 === fixture.hashes[2]
    && csvInitial.boundary_event_ids === '',
  'Initial complete-state interval is not directly trainable in CSV', csvInitial);
  const csvConfirmed = csvRows.find(row => row.start_frame_index === '4');
  assert(csvConfirmed?.end_frame_index_exclusive === '6'
    && csvConfirmed.reconstruction_presence === 'rt13'
    && csvConfirmed.clarity === 'bad'
    && csvConfirmed.anchor_frame_index === '5'
    && csvConfirmed.anchor_frame_sha256 === fixture.hashes[4]
    && csvConfirmed.boundary_event_ids === fixture.autoConfirmEvent.event_id,
  'Confirmed complete-state interval failed CSV round-trip', csvConfirmed);
  const csvFinal = csvRows.find(row => row.start_frame_index === '6');
  assert(csvFinal?.end_frame_index_exclusive === '7'
    && csvFinal.reconstruction_presence === 'c_six_by_two'
    && csvFinal.clarity === 'bad'
    && csvFinal.anchor_frame_index === '6'
    && csvFinal.anchor_frame_sha256 === fixture.hashes[5]
    && csvFinal.boundary_event_ids.split('|').sort().join('|')
      === [firstPosthocId, secondPosthocId].sort().join('|'),
  'Same-frame complete-state interval failed CSV round-trip', csvFinal);
  const csvHeader = csvText.split(/\r?\n/, 1)[0].replace(/^\ufeff/, '').split(',');
  assert(!csvHeader.some(name => /equalizer|appeared|disappeared|event_id$/i.test(name))
    && ['segment_id', 'start_frame_index', 'end_frame_index_exclusive',
      'reconstruction_presence', 'clarity', 'anchor_frame_index',
      'anchor_frame_sha256', 'status', 'boundary_event_ids']
      .every(name => csvHeader.includes(name)),
  'CSV is still an event-delta export instead of a derived-state training table', csvHeader);

  page.once('dialog', dialog => dialog.accept());
  await page.locator('#arp-point-import').setInputFiles({
    name: 'v2-roundtrip.json',
    mimeType: 'application/json',
    buffer: Buffer.from(jsonText),
  });
  await page.waitForFunction(
    () => /Imported .* point events/i.test(
      document.querySelector('#arp-point-status')?.textContent || '',
    ),
  );
  const roundTrip = JSON.parse(await downloadText(
    page,
    () => page.locator('#arp-point-export-json').click(),
  ));
  assert(JSON.stringify(roundTrip.events) === JSON.stringify(exported.events),
    'JSON import/export changed v2 events');
  assert(JSON.stringify(roundTrip.initial_state) === JSON.stringify(exported.initial_state)
    && JSON.stringify(roundTrip.segments) === JSON.stringify(exported.segments),
  'JSON import/export changed materialized state intervals');
  assert(JSON.stringify(roundTrip.revisions) === JSON.stringify(exported.revisions),
    'JSON import/export changed v2 revisions');

  const tamperedSegments = clone(exported);
  tamperedSegments.segments[0].state.clarity = 'good';
  await page.locator('#arp-point-import').setInputFiles({
    name: 'tampered-derived-segments.json',
    mimeType: 'application/json',
    buffer: Buffer.from(JSON.stringify(tamperedSegments)),
  });
  await page.waitForFunction(
    () => /Import failed:.*materialized state segments/i.test(
      document.querySelector('#arp-point-status')?.textContent || '',
    ),
  );

  const tampered = clone(exported);
  const tamperedSource = tampered.events.find(item => item.source?.kind !== 'posthoc');
  tamperedSource.source.original_note = 'tampered immutable source evidence';
  await page.locator('#arp-point-import').setInputFiles({
    name: 'tampered-source-provenance.json',
    mimeType: 'application/json',
    buffer: Buffer.from(JSON.stringify(tampered)),
  });
  await page.waitForFunction(
    () => /Import failed:.*immutable source evidence/i.test(
      document.querySelector('#arp-point-status')?.textContent || '',
    ),
  );
  const afterTamper = JSON.parse(await downloadText(
    page,
    () => page.locator('#arp-point-export-json').click(),
  ));
  assert(JSON.stringify(afterTamper.events) === JSON.stringify(exported.events),
    'Rejected source-provenance import changed event state');
  assert(JSON.stringify(afterTamper.revisions) === JSON.stringify(exported.revisions),
    'Rejected source-provenance import changed revision history');
  checkpoint('JSON/CSV roundtrip and provenance rejection passed');

  await page.reload({ waitUntil: 'load' });
  await waitForEditor(page);
  assert(await page.locator(
    `[data-point-event-list-id="${fixture.autoConfirmEvent.event_id}"]`,
  ).count() >= 1, 'Stable automatic-event ID was not restored from localStorage');
  await selectEvent(page, fixture.autoConfirmEvent.event_id);
  assert(await page.locator('#arp-point-reviewer').inputValue()
    === 'Offline Browser Reviewer', 'Reviewer was not restored from localStorage');
  assert(await page.locator('[data-point-label-id]').count() === 3,
    'Edited semantic labels were not restored from localStorage');
  assert(await page.locator(
    `#arp-point-unfinished [data-point-event-list-id="${fixture.initialEvent.event_id}"]`,
  ).count() === 1, 'Unfinished queue was not restored from localStorage');

  const sync = await page.evaluate(() => {
    const guides = [...document.querySelectorAll('.arp-panel .selected-guide')];
    const pointGuide = document.querySelector('.arp-point-track .annotation-playhead');
    const lines = [...guides, pointGuide].filter(Boolean);
    const positions = lines.map(line => line.getBoundingClientRect().left);
    return {
      modelGuideCount: guides.length,
      frameValue: document.getElementById('arp-frame-scrubber-point').value,
      zoomValue: document.getElementById('arp-zoom-timeline')?.value ?? null,
      metadata: document.getElementById('arp-selected-meta')?.textContent ?? '',
      guideSpread: positions.length ? Math.max(...positions) - Math.min(...positions) : null,
    };
  });
  assert(sync.modelGuideCount > 0, 'No model playhead guide was rendered', sync);
  assert(sync.frameValue === '3', 'Selected frame did not follow dragged event', sync);
  if (sync.zoomValue !== null) {
    assert(sync.zoomValue === '3', 'Zoom timeline is out of sync', sync);
  }
  assert(sync.metadata.trim().length > 0, 'Selected-frame provenance is empty', sync);
  assert(sync.guideSpread !== null && sync.guideSpread <= 3,
    'Timeline playheads are not aligned', sync);

  const responsive = [];
  for (const width of [1024, 736, 360]) {
    await page.setViewportSize({ width, height: 900 });
    await page.waitForTimeout(60);
    const layout = await page.evaluate(() => ({
      overflow: document.documentElement.scrollWidth
        - document.documentElement.clientWidth,
      trackWidth: document.getElementById('arp-point-track-host')
        .getBoundingClientRect().width,
      editorWidth: document.querySelector('.arp-point-editor')
        .getBoundingClientRect().width,
    }));
    assert(layout.overflow <= 2 && layout.trackWidth > 0 && layout.editorWidth > 0,
      `Responsive layout failed at ${width}px`, layout);
    const screenshot = path.join(outputDir, `rheed-point-labeling-v2-${width}.png`);
    await page.screenshot({ path: screenshot, fullPage: false });
    responsive.push({ width, ...layout, screenshot });
  }
  checkpoint('localStorage, synchronized playheads, and responsive layouts passed');
  return {
    bounds,
    lookup,
    exportedEvents: exported.events.length,
    confirmedLabels: semanticTriples,
    sameTimeEventIds: [firstPosthocId, secondPosthocId],
    sync,
    responsive,
  };
}

async function main() {
  const [reportArgument, outputArgument] = process.argv.slice(2);
  if (!reportArgument || !outputArgument) {
    throw new Error(
      'usage: node verify_rheed_labeling_ui.js <interactive_report.html> <output-dir>',
    );
  }
  const unquote = value => String(value).trim().replace(/^["']+|["']+$/g, '');
  const reportPath = path.resolve(unquote(reportArgument));
  const outputDir = path.resolve(unquote(outputArgument));
  assert(fs.existsSync(reportPath), 'Report does not exist', reportPath);
  fs.mkdirSync(outputDir, { recursive: true });
  const fixture = reportFixture(reportPath);
  const { chromium, moduleName } = loadPlaywright();
  const options = { headless: true, args: ['--allow-file-access-from-files'] };
  if (process.env.PLAYWRIGHT_EXECUTABLE_PATH) {
    options.executablePath = process.env.PLAYWRIGHT_EXECUTABLE_PATH;
  }
  const browser = await chromium.launch(options);
  const localServer = await startLocalServer(reportPath, { reportHtml: fixture.html });
  const context = await browser.newContext({
    acceptDownloads: true,
    viewport: { width: 1024, height: 900 },
  });
  const externalRequests = [];
  await installNoExternalRoute(context, externalRequests);
  const page = await context.newPage();
  const browserErrors = [];
  page.on('pageerror', error => browserErrors.push(error.stack || error.message));
  page.on('console', message => {
    if (message.type() === 'error') browserErrors.push(`console: ${message.text()}`);
  });
  try {
    checkpoint('opening fresh synthetic report');
    await page.goto(localServer.url, { waitUntil: 'load' });
    try {
      await waitForEditor(page);
    } catch (error) {
      checkpoint(`initialization browser errors: ${JSON.stringify(browserErrors)}`);
      throw error;
    }
    await page.evaluate(() => localStorage.clear());
    await page.reload({ waitUntil: 'load' });
    await waitForEditor(page);
    const staticResult = await verifyStaticEditing(page, fixture, outputDir);
    const desktopResult = await verifyDesktopCompletion(
      browser,
      reportPath,
      fixture,
      externalRequests,
    );
    assert(externalRequests.length === 0,
      'Report attempted external network access', externalRequests);
    assert(browserErrors.length === 0, 'Browser errors occurred', browserErrors);
    const result = {
      playwrightModule: moduleName,
      report: reportPath,
      schema: 'rheed-point-events-v2',
      staticResult,
      desktopResult,
      externalRequests,
      browserErrors,
    };
    fs.writeFileSync(
      path.join(outputDir, 'rheed-point-labeling-v2-browser-verification.json'),
      `${JSON.stringify(result, null, 2)}\n`,
    );
    process.stdout.write(`${JSON.stringify(result, null, 2)}\n`);
  } finally {
    await context.close();
    await browser.close();
    await localServer.close();
  }
}

main().catch(error => {
  console.error(error.stack || error);
  process.exitCode = 1;
});

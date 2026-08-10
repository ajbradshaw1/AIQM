#!/usr/bin/env node
'use strict';

// Portable, offline browser smoke test for a generated RHEED labeling report.
// Resolution order: PLAYWRIGHT_MODULE, playwright, @playwright/test,
// playwright-core.  PLAYWRIGHT_EXECUTABLE_PATH may name a system browser.

const fs = require('fs');
const http = require('http');
const path = require('path');

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
      failures.push(`${candidate}: module has no chromium export`);
    } catch (error) {
      failures.push(`${candidate}: ${error.message}`);
    }
  }
  throw new Error(`Cannot load Playwright. Set PLAYWRIGHT_MODULE if needed.\n${failures.join('\n')}`);
}

function assert(condition, message, detail) {
  if (!condition) {
    throw new Error(`${message}${detail === undefined ? '' : `: ${JSON.stringify(detail)}`}`);
  }
}

async function waitForEditor(page) {
  await page.waitForSelector('#arp-annotation-track-host .arp-annotation-track', { timeout: 20000 });
  await page.waitForSelector('#arp-frame-scrubber', { timeout: 20000 });
}

async function setFrame(page, index) {
  const scrubber = page.locator('#arp-frame-scrubber');
  await scrubber.fill(String(index));
  await scrubber.dispatchEvent('input');
  await page.waitForTimeout(30);
}

async function markRange(page, start, endInclusive) {
  await setFrame(page, start);
  await page.locator('#arp-mark-in').click();
  await setFrame(page, endInclusive);
  await page.locator('#arp-mark-out').click();
}

async function downloadText(page, click) {
  const pending = page.waitForEvent('download');
  await click();
  const download = await pending;
  const downloadedPath = await download.path();
  return fs.readFileSync(downloadedPath, 'utf8');
}

async function startLocalServer(reportPath) {
  const root = path.dirname(reportPath);
  const types = {
    '.html': 'text/html; charset=utf-8',
    '.js': 'text/javascript; charset=utf-8',
    '.json': 'application/json; charset=utf-8',
    '.webp': 'image/webp',
    '.png': 'image/png',
  };
  const server = http.createServer((request, response) => {
    const requestUrl = new URL(request.url, 'http://127.0.0.1');
    const relative = decodeURIComponent(requestUrl.pathname === '/' ? `/${path.basename(reportPath)}` : requestUrl.pathname)
      .replace(/^\/+/, '');
    const target = path.resolve(root, relative);
    if (target !== root && !target.startsWith(`${root}${path.sep}`)) {
      response.writeHead(403).end('forbidden');
      return;
    }
    fs.stat(target, (statError, stat) => {
      if (statError || !stat.isFile()) {
        response.writeHead(404).end('not found');
        return;
      }
      response.writeHead(200, {
        'Content-Type': types[path.extname(target).toLowerCase()] || 'application/octet-stream',
        'Cache-Control': 'no-store',
      });
      fs.createReadStream(target).pipe(response);
    });
  });
  await new Promise((resolve, reject) => {
    server.once('error', reject);
    server.listen(0, '127.0.0.1', resolve);
  });
  const address = server.address();
  return {
    url: `http://127.0.0.1:${address.port}/${encodeURIComponent(path.basename(reportPath))}`,
    close: () => new Promise((resolve, reject) => server.close(error => error ? reject(error) : resolve())),
  };
}

async function main() {
  const [reportArgument, outputArgument] = process.argv.slice(2);
  if (!reportArgument || !outputArgument) {
    throw new Error('usage: node verify_rheed_labeling_ui.js <interactive_report.html> <output-dir>');
  }
  const reportPath = path.resolve(reportArgument);
  const outputDir = path.resolve(outputArgument);
  assert(fs.existsSync(reportPath), 'Report does not exist', reportPath);
  fs.mkdirSync(outputDir, { recursive: true });
  const localServer = await startLocalServer(reportPath);

  const { chromium, moduleName } = loadPlaywright();
  const launchOptions = { headless: true, args: ['--allow-file-access-from-files'] };
  if (process.env.PLAYWRIGHT_EXECUTABLE_PATH) {
    launchOptions.executablePath = process.env.PLAYWRIGHT_EXECUTABLE_PATH;
  }
  const browser = await chromium.launch(launchOptions);
  const context = await browser.newContext({
    acceptDownloads: true,
    viewport: { width: 1024, height: 900 },
  });
  // Serve local assets on loopback for Chrome installations that block file://
  // subresources. Abort every non-loopback request so a hidden CDN dependency
  // still fails immediately.
  await context.route(/^https?:/i, route => {
    const url = new URL(route.request().url());
    if (url.hostname === '127.0.0.1' || url.hostname === 'localhost') route.continue();
    else route.abort('internetdisconnected');
  });
  const page = await context.newPage();
  const browserErrors = [];
  const networkRequests = [];
  page.on('pageerror', error => browserErrors.push(error.stack || error.message));
  page.on('console', message => {
    if (message.type() === 'error') browserErrors.push(`console: ${message.text()}`);
  });
  page.on('request', request => {
    if (/^https?:/i.test(request.url())) {
      const url = new URL(request.url());
      if (url.hostname !== '127.0.0.1' && url.hostname !== 'localhost') networkRequests.push(request.url());
    }
  });

  try {
    await page.goto(localServer.url, { waitUntil: 'load' });
    await waitForEditor(page);
    await page.evaluate(() => localStorage.clear());
    await page.reload({ waitUntil: 'load' });
    await waitForEditor(page);

    const bounds = await page.locator('#arp-frame-scrubber').evaluate(input => ({
      min: Number(input.min), max: Number(input.max), value: Number(input.value),
    }));
    assert(Number.isInteger(bounds.min) && Number.isInteger(bounds.max), 'Scrubber bounds are invalid', bounds);
    assert(bounds.max - bounds.min >= 5, 'Browser smoke test needs at least six saved frames', bounds);
    const start = bounds.min + 1;

    const labelValues = await page.locator('#arp-annotation-label option').evaluateAll(options =>
      options.map(option => option.value).filter(Boolean));
    assert(labelValues.length >= 2, 'At least two annotation labels are required', labelValues);
    await page.locator('#arp-annotation-labeler').fill('Offline Browser Test');

    await markRange(page, start, start + 1);
    await page.locator('#arp-annotation-label').selectOption(labelValues[0]);
    await page.locator('#arp-annotation-notes').fill('first, "quoted" segment');
    await page.locator('#arp-annotation-apply').click();
    assert(await page.locator('[data-segment-id]').count() === 1, 'First segment was not added');
    const firstId = await page.locator('[data-segment-id]').first().getAttribute('data-segment-id');

    await page.locator(`[data-annotation-list-id="${firstId}"]`).click();
    await page.locator('#arp-annotation-label').selectOption(labelValues[1]);
    await page.locator('#arp-annotation-notes').fill('edited segment');
    await page.locator('#arp-annotation-apply').click();
    assert(await page.locator(`[data-segment-id="${firstId}"]`).count() === 1,
      'Editing changed the stable annotation ID');

    await page.locator('#arp-annotation-new').click();
    await markRange(page, start + 2, start + 3);
    await page.locator('#arp-annotation-label').selectOption(labelValues[0]);
    await page.locator('#arp-annotation-apply').click();
    assert(await page.locator('[data-segment-id]').count() === 2, 'Adjacent segment was rejected');

    await page.locator('#arp-annotation-new').click();
    await markRange(page, start + 1, start + 3);
    await page.locator('#arp-annotation-label').selectOption(labelValues[1]);
    await page.locator('#arp-annotation-apply').click();
    assert(await page.locator('[data-segment-id]').count() === 2,
      'Overlapping segment mutated the annotation set');
    assert(/overlap/i.test(await page.locator('#arp-annotation-status').textContent()),
      'Overlap rejection was not visible');

    await setFrame(page, start);
    const sync = await page.evaluate(() => {
      const lines = [
        ...document.querySelectorAll('.arp-panel .selected-guide'),
        document.querySelector('.annotation-playhead'),
      ].filter(Boolean);
      const positions = lines.map(line => line.getBoundingClientRect().left);
      return {
        modelGuideCount: document.querySelectorAll('.arp-panel .selected-guide').length,
        frameValue: document.getElementById('arp-frame-scrubber').value,
        zoomValue: document.getElementById('arp-zoom-timeline')?.value ?? null,
        metadata: document.getElementById('arp-selected-meta')?.textContent ?? '',
        guideSpread: positions.length ? Math.max(...positions) - Math.min(...positions) : null,
      };
    });
    assert(sync.modelGuideCount > 0, 'No model playhead guide was rendered', sync);
    assert(sync.frameValue === String(start), 'Selected frame did not follow the scrubber', sync);
    if (sync.zoomValue !== null) assert(sync.zoomValue === String(start), 'Zoom timeline is out of sync', sync);
    assert(sync.metadata.length > 0, 'Selected-frame provenance is empty', sync);
    assert(sync.guideSpread !== null && sync.guideSpread <= 3, 'Timeline playheads are misaligned', sync);

    const jsonText = await downloadText(page, () => page.locator('#arp-annotation-export-json').click());
    const exported = JSON.parse(jsonText);
    assert(exported.schema_version === 'rheed-temporal-segments-v1', 'Unexpected export schema');
    assert(exported.dataset && exported.dataset.dataset_id, 'Export has no dataset identity');
    assert(exported.dataset.ordered_frame_fingerprint, 'Export has no ordered-frame fingerprint');
    assert(exported.dataset.model_context_fingerprint, 'Export has no model-review context fingerprint');
    assert(exported.annotation_set.model_outputs_visible === true, 'Model visibility was not disclosed');
    assert(exported.annotation_set.eligible_for_gold === false, 'Model-assisted labels claim gold eligibility');
    assert(exported.segments.length === 2, 'Unexpected exported segment count');
    assert(exported.segments.some(segment => segment.annotation_id === firstId && segment.notes === 'edited segment'),
      'Edited segment did not round-trip through JSON');
    for (const segment of exported.segments) {
      for (const endpoint of [segment.start, segment.end]) {
        assert(Number.isInteger(endpoint.heartbeat_idx), 'Endpoint has no heartbeat index', endpoint);
        assert(/^[0-9a-f]{64}$/i.test(endpoint.frame_sha256), 'Endpoint has no frame SHA-256', endpoint);
      }
    }

    const tampered = JSON.parse(jsonText);
    tampered.segments[0].start.frame_sha256 = 'f'.repeat(64);
    await page.locator('#arp-annotation-import').setInputFiles({
      name: 'tampered-endpoint.json',
      mimeType: 'application/json',
      buffer: Buffer.from(JSON.stringify(tampered)),
    });
    assert(await page.locator('[data-segment-id]').count() === 2,
      'Tampered endpoint import was not atomic');
    assert(/provenance|frame|hash|heartbeat/i.test(await page.locator('#arp-annotation-status').textContent()),
      'Tampered endpoint rejection was not visible');

    const csvText = await downloadText(page, () => page.locator('#arp-annotation-export-csv').click());
    assert(csvText.includes('ordered_frame_fingerprint'), 'CSV omits ordered-frame provenance');
    assert(csvText.includes('model_context_fingerprint'), 'CSV omits model-review context provenance');
    assert(csvText.includes('eligible_for_gold'), 'CSV omits gold-eligibility disclosure');
    for (const column of [
      'start_heartbeat_idx', 'end_heartbeat_idx', 'start_frame_sha256', 'end_frame_sha256',
    ]) {
      assert(csvText.includes(column), `CSV omits ${column}`);
    }

    const responsive = [];
    for (const width of [1024, 736, 360]) {
      await page.setViewportSize({ width, height: 900 });
      await page.waitForTimeout(80);
      const layout = await page.evaluate(() => ({
        overflow: document.documentElement.scrollWidth - document.documentElement.clientWidth,
        trackWidth: document.getElementById('arp-annotation-track-host').getBoundingClientRect().width,
        editorWidth: document.querySelector('.arp-annotation-editor').getBoundingClientRect().width,
      }));
      assert(layout.overflow <= 2 && layout.trackWidth > 0 && layout.editorWidth > 0,
        `Responsive layout failed at ${width}px`, layout);
      const screenshot = path.join(outputDir, `rheed-labeling-${width}.png`);
      await page.screenshot({ path: screenshot, fullPage: false });
      responsive.push({ width, ...layout, screenshot });
    }

    assert(networkRequests.length === 0, 'Report attempted network access while offline', networkRequests);
    assert(browserErrors.length === 0, 'Browser errors occurred', browserErrors);
    const result = {
      playwrightModule: moduleName,
      report: reportPath,
      exportedSegments: exported.segments.length,
      sync,
      responsive,
      networkRequests,
      browserErrors,
    };
    fs.writeFileSync(
      path.join(outputDir, 'rheed-labeling-browser-verification.json'),
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

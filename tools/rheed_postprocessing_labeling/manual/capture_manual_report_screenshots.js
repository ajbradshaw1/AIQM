#!/usr/bin/env node
'use strict';

// Capture deterministic, high-resolution documentation images from the real
// offline report UI. The report is served on loopback because Chromium applies
// stricter subresource rules to file:// pages. No non-loopback request is
// allowed to leave the browser context.

const childProcess = require('child_process');
const fs = require('fs');
const http = require('http');
const path = require('path');

function assert(condition, message, detail) {
  if (!condition) {
    const suffix = detail === undefined ? '' : `: ${JSON.stringify(detail)}`;
    throw new Error(`${message}${suffix}`);
  }
}

function addPlaywrightCandidates(candidates, moduleRoot) {
  if (!moduleRoot) return;
  candidates.push(
    path.join(moduleRoot, 'playwright'),
    path.join(moduleRoot, '@playwright', 'test'),
    path.join(moduleRoot, 'playwright-core'),
    path.join(moduleRoot, '@playwright', 'cli', 'node_modules', 'playwright'),
    path.join(moduleRoot, '@playwright', 'cli', 'node_modules', 'playwright-core'),
  );
}

function globalNodeModuleRoots() {
  const roots = [];
  const add = value => {
    if (value && !roots.includes(value)) roots.push(value);
  };

  for (const value of String(process.env.NODE_PATH || '').split(path.delimiter)) add(value.trim());
  if (process.env.APPDATA) add(path.join(process.env.APPDATA, 'npm', 'node_modules'));

  const executableDir = path.dirname(process.execPath);
  add(path.join(executableDir, 'node_modules'));
  add(path.join(executableDir, 'node_global', 'node_modules'));

  const npmCommands = process.platform === 'win32' ? ['npm.cmd', 'npm'] : ['npm'];
  for (const command of npmCommands) {
    try {
      add(childProcess.execFileSync(command, ['root', '-g'], {
        encoding: 'utf8',
        stdio: ['ignore', 'pipe', 'ignore'],
        timeout: 4000,
        windowsHide: true,
      }).trim());
      break;
    } catch (_error) {
      // Other deterministic roots above and normal Node resolution remain.
    }
  }
  return roots;
}

function loadPlaywright() {
  const candidates = [
    process.env.PLAYWRIGHT_MODULE,
    'playwright',
    '@playwright/test',
    'playwright-core',
  ].filter(Boolean);
  for (const root of globalNodeModuleRoots()) addPlaywrightCandidates(candidates, root);

  const failures = [];
  for (const candidate of [...new Set(candidates)]) {
    try {
      const loaded = require(candidate);
      const chromium = loaded.chromium || (loaded.default && loaded.default.chromium);
      if (chromium) return { chromium, moduleName: candidate };
      failures.push(`${candidate}: module has no chromium export`);
    } catch (error) {
      failures.push(`${candidate}: ${error.message}`);
    }
  }
  throw new Error(
    'Cannot load Playwright. Set PLAYWRIGHT_MODULE to an installed package.\n' +
    failures.join('\n'),
  );
}

function findBrowserExecutable() {
  const candidates = [
    process.env.PLAYWRIGHT_EXECUTABLE_PATH,
    process.env.CHROME_PATH,
    process.env.ProgramFiles && path.join(process.env.ProgramFiles, 'Google', 'Chrome', 'Application', 'chrome.exe'),
    process.env['ProgramFiles(x86)'] && path.join(process.env['ProgramFiles(x86)'], 'Google', 'Chrome', 'Application', 'chrome.exe'),
    process.env.LOCALAPPDATA && path.join(process.env.LOCALAPPDATA, 'Google', 'Chrome', 'Application', 'chrome.exe'),
    process.env.ProgramFiles && path.join(process.env.ProgramFiles, 'Microsoft', 'Edge', 'Application', 'msedge.exe'),
    process.env['ProgramFiles(x86)'] && path.join(process.env['ProgramFiles(x86)'], 'Microsoft', 'Edge', 'Application', 'msedge.exe'),
  ].filter(Boolean);
  return candidates.find(candidate => fs.existsSync(candidate)) || null;
}

function isLoopback(urlString) {
  try {
    const parsed = new URL(urlString);
    return (parsed.protocol === 'http:' || parsed.protocol === 'https:') &&
      (parsed.hostname === '127.0.0.1' || parsed.hostname === 'localhost' || parsed.hostname === '::1');
  } catch (_error) {
    return false;
  }
}

async function startLocalServer(reportPath) {
  const root = path.dirname(reportPath);
  const contentTypes = {
    '.css': 'text/css; charset=utf-8',
    '.gif': 'image/gif',
    '.html': 'text/html; charset=utf-8',
    '.jpeg': 'image/jpeg',
    '.jpg': 'image/jpeg',
    '.js': 'text/javascript; charset=utf-8',
    '.json': 'application/json; charset=utf-8',
    '.png': 'image/png',
    '.svg': 'image/svg+xml',
    '.webp': 'image/webp',
  };
  const server = http.createServer((request, response) => {
    if (request.method !== 'GET' && request.method !== 'HEAD') {
      response.writeHead(405, { Allow: 'GET, HEAD' }).end();
      return;
    }

    let pathname;
    try {
      const requestUrl = new URL(request.url, 'http://127.0.0.1');
      pathname = decodeURIComponent(requestUrl.pathname);
    } catch (_error) {
      response.writeHead(400).end('bad request');
      return;
    }
    const relative = (pathname === '/' ? path.basename(reportPath) : pathname).replace(/^[/\\]+/, '');
    const target = path.resolve(root, relative);
    const relativeTarget = path.relative(root, target);
    if (!relativeTarget || relativeTarget.startsWith('..') || path.isAbsolute(relativeTarget)) {
      response.writeHead(403).end('forbidden');
      return;
    }

    fs.stat(target, (statError, stat) => {
      if (statError || !stat.isFile()) {
        response.writeHead(404).end('not found');
        return;
      }
      response.writeHead(200, {
        'Cache-Control': 'no-store',
        'Content-Type': contentTypes[path.extname(target).toLowerCase()] || 'application/octet-stream',
      });
      if (request.method === 'HEAD') response.end();
      else fs.createReadStream(target).pipe(response);
    });
  });
  await new Promise((resolve, reject) => {
    server.once('error', reject);
    server.listen(0, '127.0.0.1', resolve);
  });
  const address = server.address();
  return {
    url: `http://127.0.0.1:${address.port}/${encodeURIComponent(path.basename(reportPath))}`,
    close: () => new Promise((resolve, reject) => {
      server.close(error => error ? reject(error) : resolve());
    }),
  };
}

async function waitForEditor(page) {
  await page.waitForSelector('#arp-annotation-track-host .arp-annotation-track', { timeout: 20000 });
  await page.waitForSelector('#arp-frame-scrubber', { timeout: 20000 });
  await page.waitForFunction(() => {
    const scrubber = document.getElementById('arp-frame-scrubber');
    return scrubber && Number(scrubber.max) >= 0;
  });
}

async function setRangeValue(page, selector, value) {
  const input = page.locator(selector);
  await input.fill(String(value));
  await input.dispatchEvent('input');
  await page.waitForTimeout(35);
}

async function addSegment(page, segment, expectedCount) {
  if (expectedCount > 1) await page.locator('#arp-annotation-new').click();
  await setRangeValue(page, '#arp-frame-scrubber', segment.start);
  await page.locator('#arp-mark-in').click();
  await setRangeValue(page, '#arp-frame-scrubber', segment.end);
  await page.locator('#arp-mark-out').click();
  await page.locator('#arp-annotation-label').selectOption(segment.label);
  await page.locator('#arp-annotation-notes').fill(segment.notes);
  await page.locator('#arp-annotation-apply').click();
  await page.waitForFunction(count => (
    document.querySelectorAll('#arp-annotation-track-host [data-segment-id]').length === count
  ), expectedCount);
}

async function editorClip(page) {
  return page.evaluate(() => {
    const section = document.getElementById('anneal-model-timeline');
    const editor = section && section.querySelector('.arp-annotation-editor');
    const detail = section && section.querySelector('.arp-detail');
    if (!section || !editor || !detail) throw new Error('Cannot locate the report editor regions');

    const sectionRect = section.getBoundingClientRect();
    const editorRect = editor.getBoundingClientRect();
    const detailRect = detail.getBoundingClientRect();
    const scrollX = window.scrollX;
    const scrollY = window.scrollY;
    const x = Math.max(0, Math.floor(sectionRect.left + scrollX));
    const y = Math.max(0, Math.floor(sectionRect.top + scrollY));
    const right = Math.ceil(Math.max(editorRect.right, detailRect.right) + scrollX);
    const bottom = Math.ceil(Math.max(editorRect.bottom, detailRect.bottom) + scrollY);
    return { x, y, width: right - x, height: bottom - y };
  });
}

async function main() {
  const [reportArgument, outputArgument] = process.argv.slice(2);
  if (!reportArgument || !outputArgument) {
    throw new Error(
      'usage: node capture_manual_report_screenshots.js <interactive_report.html> <output-dir>',
    );
  }

  const reportPath = path.resolve(reportArgument);
  const outputDir = path.resolve(outputArgument);
  assert(fs.existsSync(reportPath) && fs.statSync(reportPath).isFile(), 'Report does not exist', reportPath);
  fs.mkdirSync(outputDir, { recursive: true });

  const { chromium, moduleName } = loadPlaywright();
  const localServer = await startLocalServer(reportPath);
  let browser;
  let context;
  const externalRequests = [];
  const browserErrors = [];

  try {
    const executablePath = findBrowserExecutable();
    const launchOptions = {
      headless: true,
      args: [
        '--disable-background-networking',
        '--disable-component-update',
        '--disable-features=MediaRouter,Translate',
        '--no-first-run',
      ],
    };
    if (executablePath) launchOptions.executablePath = executablePath;
    browser = await chromium.launch(launchOptions);
    context = await browser.newContext({
      colorScheme: 'light',
      deviceScaleFactor: 2,
      locale: 'en-US',
      reducedMotion: 'reduce',
      timezoneId: 'UTC',
      viewport: { width: 1440, height: 1100 },
    });
    await context.route('**/*', route => {
      const requestUrl = route.request().url();
      const protocol = (() => {
        try { return new URL(requestUrl).protocol; } catch (_error) { return ''; }
      })();
      if (isLoopback(requestUrl) || ['about:', 'blob:', 'data:'].includes(protocol)) {
        route.continue();
      } else {
        externalRequests.push(requestUrl);
        route.abort('internetdisconnected');
      }
    });

    const page = await context.newPage();
    page.on('pageerror', error => browserErrors.push(error.stack || error.message));
    page.on('console', message => {
      if (message.type() === 'error') browserErrors.push(`console: ${message.text()}`);
    });

    await page.goto(localServer.url, { waitUntil: 'load' });
    await waitForEditor(page);
    await page.evaluate(() => localStorage.clear());
    await page.reload({ waitUntil: 'load' });
    await waitForEditor(page);

    const bounds = await page.locator('#arp-frame-scrubber').evaluate(input => ({
      min: Number(input.min),
      max: Number(input.max),
    }));
    assert(bounds.min === 0 && bounds.max === 59, 'Documentation fixture must contain exactly 60 frames', bounds);
    await page.locator('#arp-annotation-labeler').fill('Manual Demo');

    const segments = [
      { start: 0, end: 14, label: 'none_weak', notes: 'Initial 1x1-like interval' },
      { start: 15, end: 29, label: 'twinned_2x1', notes: 'First reconstructed interval' },
      { start: 30, end: 44, label: 'c_6x2', notes: 'Middle reconstructed interval' },
      { start: 45, end: 59, label: 'rt13', notes: 'Final reconstructed interval' },
    ];
    for (let index = 0; index < segments.length; index += 1) {
      await addSegment(page, segments[index], index + 1);
    }
    assert(
      await page.locator('#arp-annotation-track-host [data-segment-id]').count() === 4,
      'Expected four saved annotation segments',
    );

    await setRangeValue(page, '#arp-frame-scrubber', 49);
    await page.locator('#arp-brightness').fill('110');
    await page.locator('#arp-brightness').dispatchEvent('input');
    await page.locator('#arp-contrast').fill('125');
    await page.locator('#arp-contrast').dispatchEvent('input');
    await page.waitForTimeout(100);

    const editorPath = path.join(outputDir, 'rheed_timeline_editor.png');
    const clip = await editorClip(page);
    assert(clip.width > 800 && clip.height > 400, 'Editor screenshot bounds are implausible', clip);
    await page.screenshot({
      path: editorPath,
      clip,
      animations: 'disabled',
      type: 'png',
    });

    await page.locator('#arp-zoom-open').click();
    await page.waitForSelector('#arp-zoom-dialog[open]');
    await page.locator('#arp-zoom-in').click();
    await page.locator('#arp-zoom-in').click();
    await setRangeValue(page, '#arp-zoom-timeline', 52);
    await page.waitForFunction(() => {
      const image = document.getElementById('arp-zoom-image');
      return image && image.dataset.frameIndex === '52' && image.complete;
    });
    await page.waitForTimeout(100);

    const zoomPath = path.join(outputDir, 'rheed_timeline_zoom.png');
    await page.locator('#arp-zoom-dialog').screenshot({
      path: zoomPath,
      animations: 'disabled',
      type: 'png',
    });

    assert(externalRequests.length === 0, 'Report attempted non-loopback network access', externalRequests);
    assert(browserErrors.length === 0, 'Browser errors occurred while capturing the report', browserErrors);
    process.stdout.write(`${JSON.stringify({
      browserExecutable: executablePath || 'playwright-bundled',
      editorScreenshot: editorPath,
      frameCount: 60,
      playwrightModule: moduleName,
      segmentCount: 4,
      zoomScreenshot: zoomPath,
    }, null, 2)}\n`);
  } finally {
    if (context) await context.close();
    if (browser) await browser.close();
    await localServer.close();
  }
}

main().catch(error => {
  console.error(error.stack || error);
  process.exitCode = 1;
});

import { test, expect } from '@playwright/test';
import { spawn, spawnSync } from 'node:child_process';
import { mkdtemp, rm, writeFile } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { basename, join, resolve, sep } from 'node:path';

test('bundled assets drive a real synthetic capture and preserve annotations on disk', async ({
  page,
}, testInfo) => {
  test.setTimeout(60000);
  const directory = await mkdtemp(join(tmpdir(), 'sifi-web-browser-'));
  const capture = join(directory, 'browser.capture.jsonl.zst');
  const kinds = join(directory, 'kinds.json');
  await writeFile(
    kinds,
    JSON.stringify([
      { target: 'marker', kind: 'note', label: 'Note' },
      { target: 'segment', kind: 'rest', label: 'Rest' },
    ]),
  );
  const root = resolve(import.meta.dirname, '../..');
  const uv = process.env.UV_EXE || 'uv';
  const processHandle = spawn(
    uv,
    [
      'run',
      'python',
      '-m',
      'sifi_streamer.web.cli',
      capture,
      '--synthetic',
      '--no-open',
      '--capture-id',
      'browser-test',
      '--kinds-file',
      kinds,
    ],
    { cwd: root, windowsHide: true, stdio: ['ignore', 'pipe', 'pipe'] },
  );
  let logs = '';
  processHandle.stderr.on('data', (chunk) => {
    logs += chunk.toString();
  });
  let launchURL = '';
  try {
    launchURL = await new Promise<string>((resolveURL, reject) => {
      const timeout = setTimeout(() => reject(new Error(`Server did not start: ${logs}`)), 20000);
      processHandle.once('error', (error) => {
        clearTimeout(timeout);
        reject(error);
      });
      processHandle.once('exit', (code) => {
        clearTimeout(timeout);
        reject(new Error(`Server exited ${code}: ${logs}`));
      });
      processHandle.stdout.on('data', (chunk) => {
        const match = chunk.toString().match(/http:\/\/127\.0\.0\.1:\d+\/#\S+/);
        if (match) {
          clearTimeout(timeout);
          resolveURL(match[0]);
        }
      });
    });
    const errors: string[] = [];
    page.on('pageerror', (error) => errors.push(error.message));
    page.on('console', (message) => {
      if (message.type() === 'error') errors.push(message.text());
    });
    await page.goto(launchURL);
    await expect(page.getByLabel('Capture ID')).toHaveValue('browser-test');
    await page.getByRole('button', { name: 'Start capture', exact: true }).click();
    await expect(
      page.getByRole('img', { name: /emg_armband: 8 signal channels, [1-9]/ }),
    ).toBeVisible();
    await page.screenshot({ path: testInfo.outputPath('real-signals.png'), fullPage: true });
    await page.getByRole('tab', { name: 'Annotations' }).click();
    await page.getByRole('button', { name: 'Add marker', exact: true }).click();
    await page.getByRole('button', { name: 'Start segment', exact: true }).click();
    await expect(page.getByRole('button', { name: 'Close segment', exact: true })).toBeVisible();
    await page.getByRole('button', { name: 'Stop & save' }).click();
    await page.getByRole('button', { name: 'Stop and save', exact: true }).click();
    await expect(page.getByText('Recording flushed to disk.')).toBeVisible();
    const check = spawnSync(
      uv,
      [
        'run',
        'python',
        '-c',
        'import sys; from pathlib import Path; from sifi_streamer.capture import CaptureLogReader, RawPacket, Marker, SegmentStarted, SegmentStopped, CaptureStopped; records=list(CaptureLogReader(Path(sys.argv[1]))); assert any(isinstance(r, RawPacket) for r in records); assert any(isinstance(r, Marker) and r.marker_kind == "note" for r in records); assert any(isinstance(r, SegmentStarted) for r in records); assert any(isinstance(r, SegmentStopped) for r in records); assert isinstance(records[-1], CaptureStopped)',
        capture,
      ],
      { cwd: root, windowsHide: true, encoding: 'utf8', timeout: 10000 },
    );
    expect(check.status, check.stdout + check.stderr).toBe(0);
    await page.getByRole('button', { name: 'Exit dashboard' }).click();
    await expect(page.getByRole('heading', { name: 'All done.' })).toBeVisible();
    expect(errors).toEqual([]);
  } finally {
    if (launchURL && processHandle.exitCode === null) {
      await fetch(new URL('/api/server/stop', launchURL), {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          'X-SiFi-Session-Token': new URL(launchURL).hash.slice(1),
        },
        body: '{}',
      }).catch(() => {});
    }
    if (processHandle.exitCode === null) {
      await new Promise<void>((resolveExit) => {
        const timeout = setTimeout(() => {
          processHandle.kill();
          resolveExit();
        }, 5000);
        processHandle.once('exit', () => {
          clearTimeout(timeout);
          resolveExit();
        });
      });
    }
    if (
      !resolve(directory).startsWith(resolve(tmpdir()) + sep) ||
      !basename(directory).startsWith('sifi-web-browser-')
    )
      throw new Error('Unexpected test artifact directory');
    await rm(directory, { recursive: true, force: true });
  }
});

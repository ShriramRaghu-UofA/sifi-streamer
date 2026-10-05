import { expect, test } from '@playwright/test';
import { health, mockSession } from './fixtures';

test('capture, annotations, health rules, stop and acknowledged shutdown', async ({
  page,
}, testInfo) => {
  const { calls } = await mockSession(page);
  const errors: string[] = [];
  page.on('pageerror', (error) => errors.push(error.message));
  await page.goto('/#test-token');
  await expect(page.getByLabel('Capture ID')).toHaveValue('pilot-session-01');
  await page.screenshot({ path: testInfo.outputPath('session.png'), fullPage: true });
  await page.getByRole('button', { name: 'Start capture', exact: true }).click();
  await expect(page.getByRole('tab', { name: 'Signals', exact: true })).toHaveAttribute(
    'aria-selected',
    'true',
  );
  await expect(page.getByRole('img', { name: /EMG: 8 signal channels, [1-9]/ })).toBeVisible();
  await expect(page.locator('.uplot canvas')).toBeVisible();
  await page.screenshot({ path: testInfo.outputPath('signals.png'), fullPage: true });
  await page.getByRole('tab', { name: 'Annotations', exact: true }).click();
  await page.getByRole('button', { name: 'Add marker', exact: true }).click();
  await expect(page.getByText('ID: note_01')).toBeVisible();
  await page.getByRole('button', { name: 'Start segment', exact: true }).click();
  await page.getByRole('button', { name: 'Start segment', exact: true }).click();
  const segments = page.getByRole('heading', { name: 'Open segments' }).locator('..').locator('..');
  await expect(segments.getByRole('button', { name: 'Nested below' })).toBeDisabled();
  await segments.getByRole('button', { name: 'Close segment', exact: true }).click();
  await expect(segments.getByText('rest_02', { exact: true })).toHaveCount(0);
  await page.getByRole('tab', { name: 'Health', exact: true }).click();
  await page.getByLabel('Stale after (s)').fill('');
  await page.getByRole('button', { name: 'Save health rules' }).click();
  await expect
    .poll(
      () =>
        calls.filter((call) => call.path === '/api/thresholds').at(-1)?.body.stale_after_seconds,
    )
    .toBeNull();
  await expect(page.getByText('EMG stream recovered')).toBeVisible();
  await page.getByRole('button', { name: 'Stop & save' }).click();
  await page.getByRole('button', { name: 'Keep recording' }).click();
  await expect(page.getByRole('button', { name: 'Stop & save' })).toBeVisible();
  await page.getByRole('button', { name: 'Stop & save' }).click();
  await page.getByRole('button', { name: 'Stop and save', exact: true }).click();
  await expect(page.getByText('Recording flushed to disk.')).toBeVisible();
  await page.getByRole('button', { name: 'Exit dashboard' }).click();
  await expect(page.getByRole('heading', { name: 'All done.' })).toBeVisible();
  expect(errors).toEqual([]);
});

test('slow polling is serialized and a stale response cannot revive a stopped capture', async ({
  page,
}) => {
  const { session } = await mockSession(page);
  let release: (() => void) | undefined;
  let inFlight = 0;
  let maximumInFlight = 0;
  let received = false;
  const gate = new Promise<void>((resolve) => {
    release = resolve;
  });
  await page.route('**/api/live', async (route) => {
    received = true;
    maximumInFlight = Math.max(maximumInFlight, ++inFlight);
    await gate;
    await route.fulfill({
      json: {
        state: 'recording',
        error: null,
        health: health(1),
        events: [],
        kinds: session.kinds,
        thresholds: session.thresholds,
        active_segments: [],
        batches: {},
      },
    });
    --inFlight;
  });
  await page.goto('/#test-token');
  await page.getByRole('button', { name: 'Start capture', exact: true }).click();
  await expect.poll(() => received).toBe(true);
  // Several normal polling intervals elapse while the first request is pending.
  await page.getByRole('button', { name: 'Stop & save' }).click();
  await page.getByRole('button', { name: 'Stop and save', exact: true }).click();
  await expect(page.getByText('Recording flushed to disk.')).toBeVisible();
  const response = page.waitForResponse('**/api/live');
  release?.();
  await response;
  await page.evaluate(
    () =>
      new Promise<void>((resolve) =>
        requestAnimationFrame(() => requestAnimationFrame(() => resolve())),
      ),
  );
  await expect(page.getByText('Recording flushed to disk.')).toBeVisible();
  await expect(page.getByRole('button', { name: 'Stop & save' })).toHaveCount(0);
  expect(maximumInFlight).toBe(1);
});

test('scalar metadata validation and creating, editing, removing kinds', async ({ page }) => {
  const { calls } = await mockSession(page);
  await page.goto('/#test-token');
  await page.getByRole('button', { name: 'Add field', exact: true }).first().click();
  await expect(page.getByRole('button', { name: 'Start capture', exact: true })).toBeDisabled();
  await page.getByLabel('Field name', { exact: true }).last().fill('session');
  await page
    .getByRole('combobox', { name: 'Value type', exact: true })
    .last()
    .selectOption('number');
  await page.getByLabel('Field value', { exact: true }).last().fill('3');
  await expect(page.getByRole('button', { name: 'Start capture', exact: true })).toBeEnabled();
  await page.getByRole('tab', { name: 'Annotations' }).click();
  await page.getByRole('button', { name: 'New kind', exact: true }).click();
  const dialog = page.getByRole('dialog', { name: 'New annotation kind' });
  await dialog.getByLabel('Kind name', { exact: true }).fill('stimulus');
  await dialog.getByLabel('Display label', { exact: true }).fill('Stimulus');
  await dialog.getByRole('button', { name: 'Save kind', exact: true }).click();
  await expect(page.getByRole('heading', { name: 'Stimulus', exact: true })).toBeVisible();
  await page.getByRole('button', { name: 'Edit Stimulus' }).click();
  await page.getByRole('dialog').getByLabel('Display label').fill('Prompt');
  await page.getByRole('dialog').getByRole('button', { name: 'Save kind' }).click();
  await page.getByRole('button', { name: 'Remove Prompt' }).click();
  await expect(page.getByRole('heading', { name: 'Prompt' })).toHaveCount(0);
  await page.getByRole('button', { name: 'Start capture', exact: true }).click();
  expect(calls.find((call) => call.path === '/api/capture/start')?.body.attributes).toEqual({
    operator: 'Shriram',
    session: 3,
  });
});

test('appearance persists and narrow screens have no page overflow', async ({ page }, testInfo) => {
  await mockSession(page);
  await page.setViewportSize({ width: 390, height: 844 });
  await page.goto('/#test-token');
  await page.getByRole('button', { name: 'Appearance' }).click();
  await page.getByRole('combobox', { name: 'Theme', exact: true }).selectOption('dracula');
  await page.getByRole('radio', { name: 'Light', exact: true }).press('Space');
  await expect(page.locator('html')).toHaveAttribute('data-theme', 'dracula');
  await expect(page.locator('html')).not.toHaveClass(/dark/);
  await page.keyboard.press('Escape');
  await page.reload();
  await expect(page.locator('html')).toHaveAttribute('data-theme', 'dracula');
  await expect(page.locator('html')).not.toHaveClass(/dark/);
  await expect(page.getByRole('button', { name: 'Start capture', exact: true })).toBeEnabled();
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
  await page.screenshot({ path: testInfo.outputPath('mobile-session.png'), fullPage: true });
  await page.getByRole('button', { name: 'Start capture', exact: true }).click();
  await expect(page.getByRole('img', { name: /EMG: 8 signal channels/ })).toBeVisible();
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
});

test('failed commands remain actionable and never claim a saved capture', async ({ page }) => {
  await mockSession(page);
  await page.route('**/api/capture/start', (route) =>
    route.fulfill({ status: 400, json: { error: 'Device not found' } }),
  );
  await page.goto('/#test-token');
  await page.getByRole('button', { name: 'Start capture', exact: true }).click();
  await expect(page.getByRole('alert').getByText('Device not found')).toBeVisible();
  await expect(page.getByRole('button', { name: 'Start capture', exact: true })).toBeEnabled();
  await page.route('**/api/server/stop', (route) =>
    route.fulfill({ status: 400, json: { error: 'Could not flush capture' } }),
  );
  await page.getByRole('button', { name: 'Exit dashboard' }).click();
  await expect(page.getByRole('heading', { name: 'All done.' })).toHaveCount(0);
  await expect(page.getByRole('alert').getByText('Could not flush capture')).toBeVisible();
});

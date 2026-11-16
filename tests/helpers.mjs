// helpers.mjs — shared test utilities.

import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { mkdtempSync, rmSync, existsSync } from 'node:fs';

export const KNOWN_PLAINTEXT = 'super-secret-canyon-hideout-4271';
export const KNOWN_TITLE = 'The Alpha Deep Vault';
export const WRONG_PASSWORD = 'definitely-not-the-password';

// Create a temp dir unique to this test run.
export function tempDbPath() {
  return join(mkdtempSync(join(tmpdir(), 'localkb-')), 'vault.db');
}

export function rmrf(p) {
  if (existsSync(p)) rmSync(p, { recursive: true, force: true });
}

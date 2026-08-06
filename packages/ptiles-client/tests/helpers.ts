// tests/helpers.ts — shared fixture loading.
//
// Fixtures come from the conformance corpus in the ptile-client repo: real
// published bytes sliced down to a few index entries and blocks each, keeping
// the real header, entries and payloads. That makes them right for testing
// how a file is *laid out*, and wrong for testing how *large* it is — slices
// carry a fraction of the features and have their dictionaries stripped.
//
// Override with PTILES_DATA_DIR to point at full files.

import { existsSync, readFileSync } from 'fs';
import { resolve } from 'path';
import { describe } from 'vitest';
import { parseHeader } from '../src/header.js';
import type { Header } from '../src/types.js';

const CORPUS = resolve(
  import.meta.dirname, '..', '..', '..', '..', 'ptile-client', 'conformance', 'corpus',
);

export const DATA_DIR = process.env.PTILES_DATA_DIR
  || (existsSync(CORPUS) ? CORPUS : resolve(import.meta.dirname, '..', '..', '..', 'data', 'states'));

export function fixturePath(name: string): string {
  return `${DATA_DIR}/${name}`;
}

export function hasFixture(name: string): boolean {
  return existsSync(fixturePath(name));
}

export function loadLayer(name: string): { buf: Uint8Array; header: Header } {
  const data = readFileSync(fixturePath(name));
  const buf = new Uint8Array(data.buffer, data.byteOffset, data.byteLength);
  return { buf, header: parseHeader(buf, name) };
}

/**
 * Run a suite only when its fixture exists, skipping with a message naming
 * what is missing and where it was looked for. A suite that silently fails on
 * a missing file tells you nothing; one that silently passes is worse.
 */
export function describeIfPresent(title: string, fixture: string, fn: () => void): void {
  if (hasFixture(fixture)) {
    describe(title, fn);
  } else {
    describe.skip(`${title} — missing ${fixture} in ${DATA_DIR}`, fn);
  }
}

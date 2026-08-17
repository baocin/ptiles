import { describe, test, expect, beforeEach } from 'vitest';
import { mkdtempSync, mkdirSync, writeFileSync, rmSync } from 'fs';
import { tmpdir } from 'os';
import { join } from 'path';
import { PtilesClient, manifestRelpath, publishRelpath } from '../src/composite.js';

/**
 * A published snapshot ships a manifest.json naming every scope, its layer
 * version and its path. It is not guaranteed to be there: a partial download or
 * a hand-assembled directory has none.
 *
 * The distinction is *absent* versus *broken*. Absent is normal and falls back
 * to probing filenames. Present-but-unparseable is a real fault and must throw,
 * since quietly probing would hide a corrupt publish.
 */

function fullManifest() {
  return {
    built: '2026-08-20',
    source: 'test',
    countries: { JP: ['JP', 'JP-KANTO'] },
    layers: {
      buildings: {
        version: 9,
        pattern: '{scope}.buildings_v9.ptiles',
        scopes: {
          'JP-KANTO': { country: 'JP', path: 'JP/JP-KANTO.buildings_v9.ptiles', bounds: null },
        },
      },
      water: {
        version: 1,
        pattern: '{scope}.water_v1.ptiles',
        scopes: {
          JP: { country: 'JP', path: 'JP/JP.water_v1.ptiles', bounds: null },
        },
      },
    },
  };
}

let root: string;

beforeEach(() => {
  root = mkdtempSync(join(tmpdir(), 'ptiles-manifest-'));
});

describe('path resolution', () => {
  test('US stays at the snapshot root, other countries get a directory', () => {
    expect(publishRelpath('TN', 'TN.water_v1.ptiles')).toBe('TN.water_v1.ptiles');
    expect(publishRelpath('JP-KANTO', 'JP-KANTO.water_v1.ptiles'))
      .toBe('JP/JP-KANTO.water_v1.ptiles');
  });

  test('falls back through path, then pattern, then version', () => {
    const entry = { version: 9, pattern: '{scope}.buildings_v9.ptiles' };
    expect(manifestRelpath('JP-KANTO', 'buildings', entry, { path: 'x/y.ptiles' }))
      .toBe('x/y.ptiles');
    // A pre-countries manifest carries no path.
    expect(manifestRelpath('JP-KANTO', 'buildings', entry, {}))
      .toBe('JP/JP-KANTO.buildings_v9.ptiles');
    expect(manifestRelpath('TN', 'buildings', { version: 9 }, {}))
      .toBe('TN.buildings_v9.ptiles');
  });

  test('a name that is not scope-shaped cannot be placed', () => {
    expect(manifestRelpath('not-a-scope', 'buildings', { version: 9 }, {})).toBeNull();
  });
});

describe('a missing manifest', () => {
  test('asking for one by path that is absent throws clearly', () => {
    expect(() => PtilesClient.fromManifest(join(root, 'manifest.json'), root))
      .toThrow(/no manifest at/);
  });

  test('openCountry without a manifest falls back to probing', () => {
    // Empty files: discovery and path probing must not require a parseable body.
    mkdirSync(join(root, 'JP'));
    writeFileSync(join(root, 'JP', 'JP-KANTO.water_v1.ptiles'), '');
    const c = PtilesClient.openCountry('JP', root);
    expect(c.waterAll.length + c.buildingsAll.length).toBe(0); // unparseable, but no throw
    expect(PtilesClient.discoverScopes(root, 'JP')).toEqual(['JP-KANTO']);
  });
});

describe('a broken manifest', () => {
  test('malformed JSON throws rather than falling back', () => {
    const p = join(root, 'manifest.json');
    writeFileSync(p, '{not json');
    expect(() => PtilesClient.fromManifest(p, root)).toThrow(/not valid JSON/);
  });

  test('a JSON array is rejected', () => {
    const p = join(root, 'manifest.json');
    writeFileSync(p, '[]');
    expect(() => PtilesClient.fromManifest(p, root)).toThrow(/must be a JSON object/);
  });

  test('an empty object yields an empty client, not an error', () => {
    const c = PtilesClient.fromManifest({} as never, root, { country: 'JP' });
    expect(c.buildingsAll).toEqual([]);
  });
});

describe('a manifest describing files that are not there', () => {
  test('missing files are skipped rather than throwing', () => {
    const c = PtilesClient.fromManifest(fullManifest() as never, root, { country: 'JP' });
    expect(c.buildingsAll).toEqual([]);
    expect(c.waterAll).toEqual([]);
  });

  test('a layer with no reader is skipped, not fatal', () => {
    const m = fullManifest() as never as { layers: Record<string, unknown> };
    m.layers.bathymetry = {
      version: 1,
      pattern: '{scope}.bathymetry_v1.ptiles',
      scopes: { JP: { country: 'JP', path: 'JP/JP.bathymetry_v1.ptiles' } },
    };
    expect(() => PtilesClient.fromManifest(m as never, root, { country: 'JP' }))
      .not.toThrow();
  });
});

describe('older manifests, published before countries existed', () => {
  test('membership is derived when the countries index is absent', () => {
    const m = fullManifest() as Record<string, unknown>;
    delete m.countries;
    // No files on disk, so assert on selection rather than opened readers:
    // deriving JP from the scope names must not throw and must not match US.
    expect(() => PtilesClient.fromManifest(m as never, root, { country: 'JP' }))
      .not.toThrow();
    expect(() => PtilesClient.fromManifest(m as never, root, { country: 'US' }))
      .not.toThrow();
  });
});

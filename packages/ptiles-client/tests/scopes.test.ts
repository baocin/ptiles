import { describe, test, expect } from 'vitest';
import { mkdtempSync, mkdirSync, writeFileSync } from 'fs';
import { tmpdir } from 'os';
import { join } from 'path';
import { PtilesClient, countryOf } from '../src/composite.js';

describe('scope naming', () => {
  test('a bare two-letter scope is a US state, a hyphenated one names its country', () => {
    expect(countryOf('TN')).toBe('US');
    expect(countryOf('US')).toBe('US');
    expect(countryOf('JP')).toBe('JP');
    expect(countryOf('JP-KANTO')).toBe('JP');
  });

  test('the US set owns the unprefixed namespace, so DE is Delaware', () => {
    expect(countryOf('DE')).toBe('US');
    expect(countryOf('DE-BY')).toBe('DE');
  });

  test('malformed scopes are rejected rather than guessed', () => {
    for (const bad of ['', 'jp', 'JPN', 'JP_KANTO']) {
      expect(() => countryOf(bad)).toThrow();
    }
  });
});

describe('scope discovery', () => {
  // Published snapshots keep the US at the root and give other countries a
  // directory, so discovery has to look in both.
  function snapshot(): string {
    const root = mkdtempSync(join(tmpdir(), 'ptiles-snap-'));
    mkdirSync(join(root, 'JP'));
    writeFileSync(join(root, 'TN.buildings_v9.ptiles'), '');
    writeFileSync(join(root, 'CA.buildings_v9.ptiles'), '');
    for (const s of ['JP', 'JP-KANTO', 'JP-KANSAI']) {
      writeFileSync(join(root, 'JP', `${s}.buildings_v9.ptiles`), '');
    }
    return root;
  }

  test('finds a country split across region files', () => {
    expect(PtilesClient.discoverScopes(snapshot(), 'JP'))
      .toEqual(['JP', 'JP-KANSAI', 'JP-KANTO']);
  });

  test('finds US states at the snapshot root', () => {
    expect(PtilesClient.discoverScopes(snapshot(), 'US')).toEqual(['CA', 'TN']);
  });

  test('an unknown country yields nothing rather than throwing', () => {
    expect(PtilesClient.discoverScopes(snapshot(), 'FR')).toEqual([]);
  });
});

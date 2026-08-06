// tests/water.test.ts — water header and index structure.
// Scoped to layout invariants rather than the size of any particular build;
// see tests/helpers.ts for why.

import { describe, test, expect } from 'vitest';
import { parseIndex } from '../src/spatial-index.js';
import { loadLayer, describeIfPresent } from './helpers.js';

const FILE = 'TN.water.ptiles';

describeIfPresent('Water layer', FILE, () => {
  test('is identified as water', () => {
    const { buf, header } = loadLayer(FILE);
    expect(new TextDecoder().decode(buf.slice(0, 7))).toBe('PTILESW');
    expect(header.format).toBe('Water');
    expect(header.version).toBeGreaterThanOrEqual(1);
  });

  test('declares a non-empty file', () => {
    const { header } = loadLayer(FILE);
    expect(header.feature_count).toBeGreaterThan(0);
    expect(header.block_count).toBeGreaterThan(0);
  });

  test('sections appear in order and stay inside the file', () => {
    const { buf, header } = loadLayer(FILE);
    if (header.dict_length > 0) {
      expect(header.dict_offset).toBe(256);
    }
    expect(header.index_offset).toBeGreaterThanOrEqual(256);
    expect(header.index_length).toBeGreaterThan(0);
    expect(header.blocks_offset).toBeGreaterThanOrEqual(
      header.index_offset + header.index_length,
    );
    expect(buf.length).toBeGreaterThan(header.blocks_offset);
  });

  test('index entries are well formed', () => {
    const { buf, header } = loadLayer(FILE);
    const entries = parseIndex(
      buf.slice(header.index_offset, header.index_offset + header.index_length),
    );
    expect(entries.length).toBeGreaterThan(0);
    for (const e of entries) {
      expect(e.block_length).toBeGreaterThan(0);
      expect(Number(e.block_offset)).toBeGreaterThanOrEqual(0);
      // A block that claims to run past the file is the failure worth
      // catching; an upper bound in megabytes is not.
      const abs = Number(e.block_offset) < header.blocks_offset
        ? header.blocks_offset + Number(e.block_offset)
        : Number(e.block_offset);
      expect(abs + e.block_length).toBeLessThanOrEqual(buf.length);
    }
  });

  test('entries are ordered by cell', () => {
    const { buf, header } = loadLayer(FILE);
    const entries = parseIndex(
      buf.slice(header.index_offset, header.index_offset + header.index_length),
    );
    let prev = -1n;
    for (const e of entries) {
      expect(e.h3_cell > prev).toBe(true);
      prev = e.h3_cell;
    }
  });
});

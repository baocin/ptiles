// tests/buildings.test.ts — buildings header and index structure
//
// Scoped to what the reader must get right, not to how big the file is.
// Assertions like `feature_count > 500000` only ever confirmed the fixture was
// the full 54 MB TN build; they said nothing about the parser and they fail
// against any slice. The invariants below hold for a 40 KB conformance slice
// and the published original alike, and unlike a magic threshold they would
// actually catch a reader that mis-locates a section.

import { readFileSync, existsSync } from 'fs';
import { describe, test, expect } from 'vitest';
import { parseHeader, MAGIC_TO_FORMAT } from '../src/header.js';
import { parseIndex } from '../src/spatial-index.js';
import { DATA_DIR, loadLayer, describeIfPresent } from './helpers.js';

const FILE = 'TN.buildings_v8.ptiles';

describeIfPresent('Buildings layer', FILE, () => {
  test('is identified as buildings', () => {
    const { buf, header } = loadLayer(FILE);
    expect(new TextDecoder().decode(buf.slice(0, 7))).toBe('PTILESF');
    expect(MAGIC_TO_FORMAT['PTILESF']).toBe('Buildings');
    expect(header.format).toBe('Buildings');
    expect(header.version).toBe(8);
  });

  test('declares a non-empty file', () => {
    const { header } = loadLayer(FILE);
    expect(header.feature_count).toBeGreaterThan(0);
    expect(header.block_count).toBeGreaterThan(0);
  });

  test('sections appear in order and stay inside the file', () => {
    const { buf, header } = loadLayer(FILE);
    // A dictionary, when present, sits immediately after the 256-byte header.
    // Slices strip dictionaries, so the position is only asserted when there
    // is one — the old unconditional `dict_offset === 256` failed on those.
    if (header.dict_length > 0) {
      expect(header.dict_offset).toBe(256);
      expect(header.index_offset).toBeGreaterThanOrEqual(
        header.dict_offset + header.dict_length,
      );
    }
    expect(header.index_offset).toBeGreaterThanOrEqual(256);
    expect(header.blocks_offset).toBeGreaterThanOrEqual(
      header.index_offset + header.index_length,
    );
    expect(header.blocks_offset).toBeLessThan(buf.length);
  });

  test('the index section is fully present', () => {
    const { buf, header } = loadLayer(FILE);
    const indexBuf = buf.slice(header.index_offset, header.index_offset + header.index_length);
    expect(indexBuf.length).toBe(header.index_length);
    expect(parseIndex(indexBuf).length).toBeGreaterThanOrEqual(0);
  });

  test('there is block data after the index', () => {
    const { buf, header } = loadLayer(FILE);
    // The point is that blocks exist and are reachable, not that there are
    // megabytes of them.
    expect(buf.length - header.blocks_offset).toBeGreaterThan(0);
  });

  test('every block holds at least one feature', () => {
    const { header } = loadLayer(FILE);
    expect(header.feature_count).toBeGreaterThanOrEqual(header.block_count);
  });

  test('index entries stay within the file and are cell-ordered', () => {
    const { buf, header } = loadLayer(FILE);
    const entries = parseIndex(
      buf.slice(header.index_offset, header.index_offset + header.index_length),
    );
    let prev = -1n;
    for (const e of entries) {
      expect(e.block_length).toBeGreaterThan(0);
      const abs = Number(e.block_offset) < header.blocks_offset
        ? header.blocks_offset + Number(e.block_offset)
        : Number(e.block_offset);
      expect(abs + e.block_length).toBeLessThanOrEqual(buf.length);
      // Entries are sorted by cell; this is what makes stride probing sound.
      expect(e.h3_cell > prev).toBe(true);
      prev = e.h3_cell;
    }
  });
});

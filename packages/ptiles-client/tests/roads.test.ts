// tests/roads.test.ts — test nearest on TN.roads.ptiles

import { readFileSync, existsSync } from 'fs';
import { describe, test, expect } from 'vitest';
import { parseHeader, MAGIC_TO_FORMAT } from '../src/header.js';
import { parseIndex, detectRelativeOffsets, lookupCell, resolveBlockOffset } from '../src/index.js';

const DATA_DIR = process.env.PTILES_DATA_DIR || '/home/aoi/kino/projects/ptiles/data/states';

describe('Roads layer', () => {
  test('TN.roads.ptiles has correct structure', () => {
    const path = `${DATA_DIR}/TN.roads.ptiles`;
    expect(existsSync(path)).toBe(true);

    const fileData = readFileSync(path);
    const buf = new Uint8Array(fileData.buffer, fileData.byteOffset, fileData.byteLength);
    const header = parseHeader(buf);

    expect(header.format).toBe('Roads');
    expect(header.version).toBe(2);
    expect(header.feature_count).toBe(1190884);
    expect(header.block_count).toBe(23087);
  });

  test('TN.roads.ptiles index entries are valid', () => {
    const path = `${DATA_DIR}/TN.roads.ptiles`;
    const fileData = readFileSync(path);
    const buf = new Uint8Array(fileData.buffer, fileData.byteOffset, fileData.byteLength);
    const header = parseHeader(buf);

    const indexBuf = buf.slice(header.index_offset, header.index_offset + header.index_length);
    const entries = parseIndex(indexBuf);

    expect(entries.length).toBeGreaterThan(20000);

    // Verify absolute offsets resolve correctly
    const relative = detectRelativeOffsets(entries, header.blocks_offset);
    expect(relative).toBe(false); // roads uses absolute offsets

    // First block should be at exactly blocks_offset
    const firstAbs = resolveBlockOffset(entries[0], header.blocks_offset, relative);
    expect(firstAbs).toBe(entries[0].block_offset);

    // Each entry should point to valid file positions
    for (const entry of entries) {
      const absPos = resolveBlockOffset(entry, header.blocks_offset, relative);
      expect(absPos).toBeGreaterThanOrEqual(header.blocks_offset);
      expect(absPos + entry.block_length).toBeLessThanOrEqual(buf.length);
    }
  });

  test('TN.roads.ptiles index entries are sorted by h3_cell', () => {
    const path = `${DATA_DIR}/TN.roads.ptiles`;
    const fileData = readFileSync(path);
    const buf = new Uint8Array(fileData.buffer, fileData.byteOffset, fileData.byteLength);
    const header = parseHeader(buf);

    const indexBuf = buf.slice(header.index_offset, header.index_offset + header.index_length);
    const entries = parseIndex(indexBuf);

    for (let i = 1; i < entries.length; i++) {
      expect(entries[i].h3_cell > entries[i - 1].h3_cell).toBe(true);
    }
  });
});

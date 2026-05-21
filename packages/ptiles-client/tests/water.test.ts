// tests/water.test.ts — test TN.water.ptiles header and index

import { readFileSync, existsSync } from 'fs';
import { describe, test, expect } from 'vitest';
import { parseHeader, MAGIC_TO_FORMAT } from '../src/header.js';
import { parseIndex } from '../src/spatial-index.js';

const DATA_DIR = process.env.PTILES_DATA_DIR || '/home/aoi/kino/projects/ptiles/data/states';

describe('Water layer', () => {
  test('TN.water.ptiles has correct magic', () => {
    const path = `${DATA_DIR}/TN.water.ptiles`;
    expect(existsSync(path)).toBe(true);

    const fileData = readFileSync(path);
    const buf = new Uint8Array(fileData.buffer, fileData.byteOffset, fileData.byteLength);

    const magicStr = new TextDecoder().decode(buf.slice(0, 7));
    expect(magicStr).toBe('PTILESW');
    expect(MAGIC_TO_FORMAT[magicStr]).toBe('Water');
  });

  test('TN.water.ptiles header parses correctly', () => {
    const path = `${DATA_DIR}/TN.water.ptiles`;
    const fileData = readFileSync(path);
    const buf = new Uint8Array(fileData.buffer, fileData.byteOffset, fileData.byteLength);

    const header = parseHeader(buf);
    expect(header.format).toBe('Water');
    expect(header.version).toBeGreaterThanOrEqual(1);
    expect(header.feature_count).toBeGreaterThan(100000);
    expect(header.block_count).toBeGreaterThan(1000);
  });

  test('TN.water.ptiles header offsets are consistent', () => {
    const path = `${DATA_DIR}/TN.water.ptiles`;
    const fileData = readFileSync(path);
    const buf = new Uint8Array(fileData.buffer, fileData.byteOffset, fileData.byteLength);
    const header = parseHeader(buf);

    expect(header.dict_offset).toBe(256);
    expect(header.dict_length).toBeGreaterThanOrEqual(0);
    expect(header.index_offset).toBeGreaterThan(header.dict_offset);
    expect(header.index_length).toBeGreaterThan(0);
    expect(header.blocks_offset).toBeGreaterThan(header.index_offset);
    expect(buf.length).toBeGreaterThan(header.blocks_offset);
  });

  test('TN.water.ptiles index entries are valid', () => {
    const path = `${DATA_DIR}/TN.water.ptiles`;
    const fileData = readFileSync(path);
    const buf = new Uint8Array(fileData.buffer, fileData.byteOffset, fileData.byteLength);
    const header = parseHeader(buf);

    const indexBuf = buf.slice(header.index_offset, header.index_offset + header.index_length);
    const entries = parseIndex(indexBuf);

    expect(entries.length).toBe(header.block_count);
    expect(entries.length).toBeGreaterThan(1000);

    for (const entry of entries) {
      expect(typeof entry.h3_cell).toBe('bigint');
      expect(entry.block_offset).toBeGreaterThanOrEqual(0);
      expect(entry.block_length).toBeGreaterThan(0);
      expect(entry.block_length).toBeLessThan(1000000); // < 1MB per block
      expect(entry.feature_count).toBeGreaterThan(0);
    }
  });

  test('TN.water.ptiles index is sorted by h3_cell', () => {
    const path = `${DATA_DIR}/TN.water.ptiles`;
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

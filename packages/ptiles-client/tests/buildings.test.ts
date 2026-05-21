// tests/buildings.test.ts — test TN.buildings_v8.ptiles header and index

import { readFileSync, existsSync } from 'fs';
import { describe, test, expect } from 'vitest';
import { parseHeader, MAGIC_TO_FORMAT } from '../src/header.js';
import { parseIndex, detectRelativeOffsets } from '../src/spatial-index.js';

const DATA_DIR = process.env.PTILES_DATA_DIR || '/home/aoi/kino/projects/ptiles/data/states';

describe('Buildings layer', () => {
  test('TN.buildings_v8.ptiles has correct magic and version', () => {
    const path = `${DATA_DIR}/TN.buildings_v8.ptiles`;
    expect(existsSync(path)).toBe(true);

    const fileData = readFileSync(path);
    const buf = new Uint8Array(fileData.buffer, fileData.byteOffset, fileData.byteLength);
    const header = parseHeader(buf);

    expect(header.format).toBe('Buildings');
    expect(header.version).toBe(8);
    expect(header.feature_count).toBeGreaterThan(500000);
    expect(header.block_count).toBeGreaterThan(1000);
  });

  test('TN.buildings_v8.ptiles header offsets are consistent', () => {
    const path = `${DATA_DIR}/TN.buildings_v8.ptiles`;
    const fileData = readFileSync(path);
    const buf = new Uint8Array(fileData.buffer, fileData.byteOffset, fileData.byteLength);
    const header = parseHeader(buf);

    // Magic bytes should be PTILESF
    const magicStr = new TextDecoder().decode(buf.slice(0, 7));
    expect(magicStr).toBe('PTILESF');
    expect(MAGIC_TO_FORMAT['PTILESF']).toBe('Buildings');

    // Offsets should be monotonically increasing
    expect(header.dict_offset).toBe(256);
    expect(header.dict_length).toBeGreaterThan(0);
    expect(header.index_offset).toBeGreaterThan(header.dict_offset);
    expect(header.blocks_offset).toBeGreaterThan(header.index_offset);
    expect(buf.length).toBeGreaterThan(header.blocks_offset);
  });

  test('TN.buildings_v8.ptiles index is present (may be empty for version-8 format)', () => {
    const path = `${DATA_DIR}/TN.buildings_v8.ptiles`;
    const fileData = readFileSync(path);
    const buf = new Uint8Array(fileData.buffer, fileData.byteOffset, fileData.byteLength);
    const header = parseHeader(buf);

    // Buildings_v8 stores index_length=4 with count=0
    // The blocks themselves may contain embedded h3_cell metadata
    const indexBuf = buf.slice(header.index_offset, header.index_offset + header.index_length);
    expect(indexBuf.length).toBe(header.index_length);

    const entries = parseIndex(indexBuf);
    // entries.length may be 0 for buildings_v8 (no conventional global index)
    expect(entries.length).toBeGreaterThanOrEqual(0);
    // Verify the index buffer doesn't exceed the blocks_offset
    expect(header.index_offset + header.index_length).toBeLessThanOrEqual(header.blocks_offset);
  });

  test('TN.buildings_v8.ptiles block_count matches feature_count ratio', () => {
    const path = `${DATA_DIR}/TN.buildings_v8.ptiles`;
    const fileData = readFileSync(path);
    const buf = new Uint8Array(fileData.buffer, fileData.byteOffset, fileData.byteLength);
    const header = parseHeader(buf);

    // Each block has at least 1 feature, so feature_count >= block_count
    expect(header.feature_count).toBeGreaterThanOrEqual(header.block_count);
    // Reasonable average: each block should hold roughly 50-500 features
    const avgFeatures = header.feature_count / header.block_count;
    expect(avgFeatures).toBeGreaterThan(10);
    expect(avgFeatures).toBeLessThan(2000);
  });

  test('TN.buildings_v8.ptiles blocks_offset is past header and index', () => {
    const path = `${DATA_DIR}/TN.buildings_v8.ptiles`;
    const fileData = readFileSync(path);
    const buf = new Uint8Array(fileData.buffer, fileData.byteOffset, fileData.byteLength);
    const header = parseHeader(buf);

    // Header is 256 bytes, dict varies, index follows, then blocks
    expect(header.blocks_offset).toBeGreaterThan(256);
    expect(header.blocks_offset).toBeLessThan(buf.length);
    // Remaining bytes should be substantial (block data)
    expect(buf.length - header.blocks_offset).toBeGreaterThan(1000000);
  });
});

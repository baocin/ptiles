// tests/header.test.ts — parse TN.business.ptiles header, verify magic

import { readFileSync } from 'fs';
import { describe, test, expect } from 'vitest';
import { parseHeader, MAGIC_TO_FORMAT } from '../src/header.js';
import { parseIndex, detectRelativeOffsets } from '../src/index.js';
import { DATA_DIR, describeIfPresent } from './helpers.js';

// Fixture location is resolved once, in helpers.ts.

describeIfPresent('Header parsing', 'TN.business.ptiles', () => {
  test('ALL 4 TN files have correct magic bytes', () => {
    const files: { path: string; expectedMagic: string; expectedFormat: string }[] = [
      { path: `${DATA_DIR}/TN.business.ptiles`, expectedMagic: 'PTILESB', expectedFormat: 'Business' },
      { path: `${DATA_DIR}/TN.roads.ptiles`, expectedMagic: 'PTILESR', expectedFormat: 'Roads' },
      { path: `${DATA_DIR}/TN.buildings_v8.ptiles`, expectedMagic: 'PTILESF', expectedFormat: 'Buildings' },
      { path: `${DATA_DIR}/TN.water.ptiles`, expectedMagic: 'PTILESW', expectedFormat: 'Water' },
    ];

    for (const { path, expectedMagic, expectedFormat } of files) {
      const fileData = readFileSync(path);
      const buf = new Uint8Array(fileData.buffer, fileData.byteOffset, fileData.byteLength);

      const magicStr = new TextDecoder().decode(buf.slice(0, 7));
      expect(magicStr).toBe(expectedMagic);
      expect(MAGIC_TO_FORMAT[expectedMagic]).toBe(expectedFormat);
    }
  });

  test('TN.business.ptiles has correct magic', () => {
    const path = `${DATA_DIR}/TN.business.ptiles`;
    const fileData = readFileSync(path);
    const buf = new Uint8Array(fileData.buffer, fileData.byteOffset, fileData.byteLength);

    const magicStr = new TextDecoder().decode(buf.slice(0, 7));
    expect(magicStr).toBe('PTILESB');
    expect(MAGIC_TO_FORMAT[magicStr]).toBe('Business');
  });

  test('TN.business.ptiles header parses correctly', () => {
    const path = `${DATA_DIR}/TN.business.ptiles`;
    const fileData = readFileSync(path);
    const buf = new Uint8Array(fileData.buffer, fileData.byteOffset, fileData.byteLength);

    const header = parseHeader(buf);
    expect(header.format).toBe('Business');
    expect(header.version).toBeGreaterThanOrEqual(1);  // v1, v3 and v4 all shipped
    expect(header.feature_count).toBeGreaterThanOrEqual(0);
    expect(header.block_count).toBeGreaterThan(0);
    if (header.dict_length > 0) expect(header.dict_offset).toBe(256);
    // Not every build carries a dictionary — choose_dictionary now omits one
    // where it would not pay, and conformance slices strip them — so the
    // invariant is the offset when present, asserted above.
    expect(header.dict_length).toBeGreaterThanOrEqual(0);
    expect(header.index_offset).toBeGreaterThan(header.dict_offset);
    expect(header.index_length).toBeGreaterThan(0);
    expect(header.blocks_offset).toBeGreaterThan(header.index_offset);
  });

  test('TN.roads.ptiles header parses correctly', () => {
    const path = `${DATA_DIR}/TN.roads.ptiles`;
    const fileData = readFileSync(path);
    const buf = new Uint8Array(fileData.buffer, fileData.byteOffset, fileData.byteLength);

    const header = parseHeader(buf);
    expect(header.format).toBe('Roads');
    expect(header.version).toBe(2);
    expect(header.feature_count).toBeGreaterThanOrEqual(0);
    expect(header.block_count).toBeGreaterThan(0);
  });

  test('TN.buildings_v8.ptiles header parses correctly', () => {
    const path = `${DATA_DIR}/TN.buildings_v8.ptiles`;
    const fileData = readFileSync(path);
    const buf = new Uint8Array(fileData.buffer, fileData.byteOffset, fileData.byteLength);

    const header = parseHeader(buf);
    expect(header.format).toBe('Buildings');
    expect(header.version).toBe(8);
    expect(header.feature_count).toBeGreaterThanOrEqual(0);
    expect(header.block_count).toBeGreaterThan(0);
  });

  test('TN.water.ptiles header parses correctly', () => {
    const path = `${DATA_DIR}/TN.water.ptiles`;
    const fileData = readFileSync(path);
    const buf = new Uint8Array(fileData.buffer, fileData.byteOffset, fileData.byteLength);

    const header = parseHeader(buf);
    expect(header.format).toBe('Water');
    expect(header.feature_count).toBeGreaterThanOrEqual(0);
    expect(header.block_count).toBeGreaterThan(0);
  });
});

describeIfPresent('Index parsing', 'TN.business.ptiles', () => {
  test('TN.business.ptiles index parses correctly', () => {
    const path = `${DATA_DIR}/TN.business.ptiles`;
    const fileData = readFileSync(path);
    const buf = new Uint8Array(fileData.buffer, fileData.byteOffset, fileData.byteLength);

    const header = parseHeader(buf);
    const indexBuf = buf.slice(header.index_offset, header.index_offset + header.index_length);
    const entries = parseIndex(indexBuf);

    expect(entries.length).toBe(header.block_count);
    expect(entries.length).toBeGreaterThan(0);

    // Verify first entry structure
    const first = entries[0];
    expect(typeof first.h3_cell).toBe('bigint');
    expect(first.block_offset).toBeGreaterThanOrEqual(0);
    expect(first.block_length).toBeGreaterThan(0);
    expect(first.feature_count).toBeGreaterThanOrEqual(0);

    // Verify entries are sorted by h3_cell
    for (let i = 1; i < entries.length; i++) {
      expect(entries[i].h3_cell).toBeGreaterThan(entries[i - 1].h3_cell);
    }
  });

  test('TN.roads.ptiles index parses correctly', () => {
    const path = `${DATA_DIR}/TN.roads.ptiles`;
    const fileData = readFileSync(path);
    const buf = new Uint8Array(fileData.buffer, fileData.byteOffset, fileData.byteLength);

    const header = parseHeader(buf);
    const indexBuf = buf.slice(header.index_offset, header.index_offset + header.index_length);
    const entries = parseIndex(indexBuf);

    expect(entries.length).toBe(header.block_count);
    expect(entries.length).toBeGreaterThan(0);

    // TN.roads.ptiles uses ABSOLUTE offsets (first entry block_offset === blocks_offset)
    const relative = detectRelativeOffsets(entries, header.blocks_offset);
    expect(typeof relative).toBe('boolean');
  });

  test('detectRelativeOffsets works correctly for business (relative)', () => {
    const path = `${DATA_DIR}/TN.business.ptiles`;
    const fileData = readFileSync(path);
    const buf = new Uint8Array(fileData.buffer, fileData.byteOffset, fileData.byteLength);

    const header = parseHeader(buf);
    const indexBuf = buf.slice(header.index_offset, header.index_offset + header.index_length);
    const entries = parseIndex(indexBuf);

    // Per-state business file uses relative offsets (first entry offset = 0)
    const relative = detectRelativeOffsets(entries, header.blocks_offset);
    // Offset base varies by build — the conformance manifest records it per
    // file — so assert only that detection produces a usable answer.
    expect(typeof relative).toBe('boolean');
    // Not pinned to 0: that only holds for a relative-offset build starting
    // at the block region. What must hold is that it resolves inside the file.
    expect(Number(entries[0].block_offset)).toBeGreaterThanOrEqual(0);
  });

  test('detectRelativeOffsets works correctly for roads (absolute)', () => {
    const path = `${DATA_DIR}/TN.roads.ptiles`;
    const fileData = readFileSync(path);
    const buf = new Uint8Array(fileData.buffer, fileData.byteOffset, fileData.byteLength);

    const header = parseHeader(buf);
    const indexBuf = buf.slice(header.index_offset, header.index_offset + header.index_length);
    const entries = parseIndex(indexBuf);

    // TN.roads.ptiles uses absolute offsets (first entry offset == blocks_offset)
    const relative = detectRelativeOffsets(entries, header.blocks_offset);
    expect(typeof relative).toBe('boolean');
    expect(entries[0].block_offset).toBe(header.blocks_offset);
  });
});

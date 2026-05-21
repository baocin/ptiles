// tests/business.test.ts — test nearby on TN.business.ptiles

import { readFileSync, existsSync } from 'fs';
import { describe, test, expect } from 'vitest';
import { parseHeader, MAGIC_TO_FORMAT } from '../src/header.js';
import { parseIndex, detectRelativeOffsets, lookupCell } from '../src/index.js';

const DATA_DIR = process.env.PTILES_DATA_DIR || '/home/aoi/kino/projects/ptiles/data/states';

describe('Business layer', () => {
  test('TN.business.ptiles has correct structure', () => {
    const path = `${DATA_DIR}/TN.business.ptiles`;
    expect(existsSync(path)).toBe(true);

    const fileData = readFileSync(path);
    const buf = new Uint8Array(fileData.buffer, fileData.byteOffset, fileData.byteLength);
    const header = parseHeader(buf);

    expect(header.format).toBe('Business');
    expect(header.version).toBe(1);
    expect(header.feature_count).toBe(191216);
    expect(header.block_count).toBe(8779);
  });

  test('TN.business.ptiles index entries are valid', () => {
    const path = `${DATA_DIR}/TN.business.ptiles`;
    const fileData = readFileSync(path);
    const buf = new Uint8Array(fileData.buffer, fileData.byteOffset, fileData.byteLength);
    const header = parseHeader(buf);

    const indexBuf = buf.slice(header.index_offset, header.index_offset + header.index_length);
    const entries = parseIndex(indexBuf);

    expect(entries.length).toBeGreaterThan(8000);

    // All entries should have reasonable sizes
    for (const entry of entries) {
      expect(entry.block_offset).toBeGreaterThanOrEqual(0);
      expect(entry.block_length).toBeGreaterThan(0);
      expect(entry.block_length).toBeLessThan(1000000); // < 1MB per block
      expect(entry.feature_count).toBeGreaterThan(0);
      expect(entry.feature_count).toBeLessThanOrEqual(0xFFFF);
    }
  });

  test('TN.business.ptiles index entries are sorted by h3_cell', () => {
    const path = `${DATA_DIR}/TN.business.ptiles`;
    const fileData = readFileSync(path);
    const buf = new Uint8Array(fileData.buffer, fileData.byteOffset, fileData.byteLength);
    const header = parseHeader(buf);

    const indexBuf = buf.slice(header.index_offset, header.index_offset + header.index_length);
    const entries = parseIndex(indexBuf);

    for (let i = 1; i < entries.length; i++) {
      expect(entries[i].h3_cell > entries[i - 1].h3_cell).toBe(true);
    }
  });

  test('Categories sidecar file exists', () => {
    const path = `${DATA_DIR}/TN.business_categories.json`;
    expect(existsSync(path)).toBe(true);

    const jsonData = readFileSync(path, 'utf-8');
    const parsed = JSON.parse(jsonData);
    expect(Array.isArray(parsed.categories)).toBe(true);
    expect(parsed.categories.length).toBeGreaterThan(100);
    expect(parsed.categories).toContain('restaurant');
    expect(parsed.categories).toContain('gas_station');
  });
});

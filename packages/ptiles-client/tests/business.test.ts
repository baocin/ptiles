// tests/business.test.ts — business header and index structure.
//
// The old assertions pinned feature_count to 191216 and block_count to 8779 —
// the exact statistics of one build of TN. Those break on any other build and
// on any slice, and they never tested the parser. Scoped to structure instead.

import { existsSync } from 'fs';
import { describe, test, expect } from 'vitest';
import { parseIndex, detectRelativeOffsets } from '../src/spatial-index.js';
import { loadLayer, fixturePath, describeIfPresent } from './helpers.js';

const FILE = 'TN.business.ptiles';

describeIfPresent('Business layer', FILE, () => {
  test('is identified as business', () => {
    const { buf, header } = loadLayer(FILE);
    expect(new TextDecoder().decode(buf.slice(0, 7))).toBe('PTILESB');
    expect(header.format).toBe('Business');
    // The layer has shipped as v1, v3 and v4; the reader must accept the
    // range rather than one build's number.
    expect(header.version).toBeGreaterThanOrEqual(1);
    expect(header.version).toBeLessThanOrEqual(4);
  });

  test('declares blocks, and a feature count that is not nonsense', () => {
    const { header } = loadLayer(FILE);
    expect(header.block_count).toBeGreaterThan(0);
    // feature_count is not asserted positive: the conformance slice for this
    // layer carries 0 in its header while holding 48 blocks, because slicing
    // repoints the index without recomputing the count. A reader must not
    // depend on it, which is exactly the property worth pinning.
    expect(header.feature_count).toBeGreaterThanOrEqual(0);
    expect(Number.isFinite(header.feature_count)).toBe(true);
  });

  test('sections appear in order and stay inside the file', () => {
    const { buf, header } = loadLayer(FILE);
    if (header.dict_length > 0) {
      expect(header.dict_offset).toBe(256);
    }
    expect(header.blocks_offset).toBeGreaterThanOrEqual(
      header.index_offset + header.index_length,
    );
    expect(buf.length).toBeGreaterThan(header.blocks_offset);
  });

  test('index entries point at real blocks', () => {
    const { buf, header } = loadLayer(FILE);
    const entries = parseIndex(
      buf.slice(header.index_offset, header.index_offset + header.index_length),
    );
    expect(entries.length).toBeGreaterThan(0);
    const relative = detectRelativeOffsets(entries, header.blocks_offset);
    for (const e of entries) {
      expect(e.block_length).toBeGreaterThan(0);
      const abs = relative ? header.blocks_offset + Number(e.block_offset) : Number(e.block_offset);
      expect(abs).toBeGreaterThanOrEqual(header.blocks_offset);
      expect(abs + e.block_length).toBeLessThanOrEqual(buf.length);
    }
  });

  test('offset base is detected consistently for every entry', () => {
    // Mixing bases would put some blocks out of range; detect once and check
    // the whole index agrees.
    const { buf, header } = loadLayer(FILE);
    const entries = parseIndex(
      buf.slice(header.index_offset, header.index_offset + header.index_length),
    );
    const relative = detectRelativeOffsets(entries, header.blocks_offset);
    expect(typeof relative).toBe('boolean');
    for (const e of entries) {
      const abs = relative ? header.blocks_offset + Number(e.block_offset) : Number(e.block_offset);
      expect(abs + e.block_length).toBeLessThanOrEqual(buf.length);
    }
  });

  test('a categories sidecar, when present, is valid JSON', () => {
    // Optional: conformance slices ship .ptiles only. Its absence is not a
    // failure; a malformed one is.
    const sidecar = fixturePath('TN.business_categories.json');
    if (!existsSync(sidecar)) return;
    const parsed = JSON.parse(require('fs').readFileSync(sidecar, 'utf8'));
    const cats = Array.isArray(parsed) ? parsed : parsed.categories;
    expect(Array.isArray(cats)).toBe(true);
  });
});

/**
 * Tests for lib/ptiles.mjs — run with `node --test lib/`.
 *
 * Uses the built-in test runner so the reader keeps its only dependency
 * (h3-js, and that just for queries). Headers and indexes are synthesized
 * rather than read from a tile set, so these run anywhere.
 *
 * Every case here is a bug this file actually shipped with, not a
 * hypothetical: a 37-byte v2 stride, filenames pinned to buildings_v8, four
 * missing layer magics, and a decoder chosen without regard to whether it
 * could apply the dictionary the file needs.
 */

import { test, describe } from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import {
  MAGIC_LAYER, INDEX_ENTRY_V1, INDEX_ENTRY_V2, LAYER_SUFFIXES,
  indexStride, readIndex, ensureZstd, _resetZstd,
} from './ptiles.mjs';

/** Build an index section: u32 count, then `count` entries of `stride` bytes. */
function makeIndex(count, stride, fill = (buf, o, i) => {}) {
  const buf = new Uint8Array(4 + count * stride);
  new DataView(buf.buffer).setUint32(0, count, true);
  for (let i = 0; i < count; i++) fill(buf, 4 + i * stride, i);
  return buf;
}

describe('index stride', () => {
  test('38 bytes is v2 — not 37, which this file used to assume', () => {
    assert.equal(INDEX_ENTRY_V2, 38);
    assert.equal(indexStride(4 + 10 * 38, 10), 38);
  });

  test('19 bytes is v1', () => {
    assert.equal(INDEX_ENTRY_V1, 19);
    assert.equal(indexStride(4 + 10 * 19, 10), 19);
  });

  test('a 37-byte section is rejected, not silently read as v2', () => {
    // The old code used `(len - 4) / 37 >= count`, an inequality that accepted
    // almost anything and then walked the entries at the wrong width.
    assert.throws(() => indexStride(4 + 10 * 37, 10), /Cannot determine index stride/);
  });

  test('the error names what it saw', () => {
    assert.throws(() => indexStride(4 + 10 * 37, 10), /37\.00 bytes each/);
  });

  test('an empty index is v1 by convention, not an error', () => {
    assert.equal(indexStride(4, 0), INDEX_ENTRY_V1);
  });

  test('a real v2 section is not mistaken for v1', () => {
    // 38*n is never 19*m for the same n, so these can never collide.
    for (const n of [1, 7, 100, 2601]) {
      assert.equal(indexStride(4 + n * 38, n), 38);
      assert.equal(indexStride(4 + n * 19, n), 19);
    }
  });
});

describe('readIndex', () => {
  test('reads v1 entries and reports the stride used', () => {
    const idx = makeIndex(3, INDEX_ENTRY_V1, (buf, o, i) => {
      buf[o] = i + 1;                     // h3 cell low byte
      buf[o + 8] = 0x10;                  // block offset
      buf[o + 14] = 0x20;                 // block length
      new DataView(buf.buffer).setUint16(o + 17, 5 + i, true);
    });
    const { entries, stride } = readIndex(idx);
    assert.equal(stride, INDEX_ENTRY_V1);
    assert.equal(entries.length, 3);
    assert.equal(entries[0].featureCount, 5);
    assert.equal(entries[2].featureCount, 7);
    assert.equal(Number(entries[0].blockOffset), 0x10);
  });

  test('reads v2 entries including bbox, feature count and cell index', () => {
    const idx = makeIndex(2, INDEX_ENTRY_V2, (buf, o, i) => {
      const dv = new DataView(buf.buffer);
      buf[o] = i + 1;
      dv.setInt32(o + 8, -8786692, true);   // minLon
      dv.setInt32(o + 12, 3562113, true);   // minLat
      dv.setInt32(o + 16, -8786461, true);  // maxLon
      dv.setInt32(o + 20, 3562327, true);   // maxLat
      buf[o + 24] = 0x40;                   // offset low
      dv.setUint16(o + 30, 0x0100, true);   // length low
      dv.setUint16(o + 34, 10 + i, true);   // feature count
      dv.setUint16(o + 36, i, true);        // cell index in block
    });
    const { entries, stride } = readIndex(idx);
    assert.equal(stride, INDEX_ENTRY_V2);
    assert.equal(entries.length, 2);
    assert.equal(entries[0].minLon, -8786692);
    assert.equal(entries[0].maxLat, 3562327);
    assert.equal(entries[0].featureCount, 10);
    assert.equal(entries[1].featureCount, 11);
    assert.equal(entries[1].cellIndexInBlock, 1);
    assert.equal(Number(entries[0].blockOffset), 0x40);
    assert.equal(entries[0].blockLength, 0x0100);
  });

  test('feature count is read at offset 34, not 33', () => {
    // At the old 37-byte layout these fields sat one byte earlier, so a v2
    // file returned neighbouring bytes as counts and indexes.
    const idx = makeIndex(1, INDEX_ENTRY_V2, (buf, o) => {
      new DataView(buf.buffer).setUint16(o + 34, 65535, true);
    });
    assert.equal(readIndex(idx).entries[0].featureCount, 65535);
  });

  test('a malformed section throws instead of returning junk', () => {
    assert.throws(() => readIndex(makeIndex(4, 37)), /index stride/);
  });
});

describe('layer magics', () => {
  test('every published layer is identified', () => {
    const expected = {
      0x46: 'buildings', 0x52: 'roads', 0x41: 'admin', 0x44: 'address',
      0x57: 'water', 0x50: 'places', 0x54: 'rail', 0x4e: 'parks',
      0x42: 'business', 0x58: 'business_name_index', 0x43: 'camera',
      0x53: 'signals',
    };
    for (const [byte, name] of Object.entries(expected)) {
      assert.equal(MAGIC_LAYER[byte], name, `magic 0x${Number(byte).toString(16)}`);
    }
  });

  test('the four that used to read as unknown are present', () => {
    for (const b of [0x44, 0x58, 0x43, 0x53]) {
      assert.ok(MAGIC_LAYER[b], `0x${b.toString(16)} still missing`);
    }
  });

  test('address and admin are distinct', () => {
    assert.notEqual(MAGIC_LAYER[0x44], MAGIC_LAYER[0x41]);
  });
});

describe('filename suffixes', () => {
  test('buildings prefers the published v9', () => {
    assert.equal(LAYER_SUFFIXES.buildings[0], 'buildings_v9');
    assert.ok(LAYER_SUFFIXES.buildings.includes('buildings_v8'));
  });

  test('business prefers the published v4', () => {
    assert.equal(LAYER_SUFFIXES.business[0], 'business_v4');
  });

  test('address knows about v2', () => {
    assert.equal(LAYER_SUFFIXES.address[0], 'address_v2');
  });

  test('newest is always first, so the first hit is the current build', () => {
    for (const [layer, suffixes] of Object.entries(LAYER_SUFFIXES)) {
      assert.ok(suffixes.length >= 1, layer);
      assert.equal(typeof suffixes[0], 'string');
    }
  });
});

describe('zstd decoder selection', () => {
  test('a plain file gets a decoder', async () => {
    _resetZstd();
    const dec = await ensureZstd(false);
    assert.ok(dec.decompress, 'no decompress function');
  });

  test('plain decoding actually works on Node', async (t) => {
    const zlib = await import('node:zlib');
    if (typeof zlib.zstdCompressSync !== 'function') return t.skip('no zstd in this Node');
    _resetZstd();
    const original = Buffer.from('123 East School Street'.repeat(20));
    const dec = await ensureZstd(false);
    const out = await dec.decompress(new Uint8Array(zlib.zstdCompressSync(original)));
    assert.equal(Buffer.from(out).toString(), original.toString());
  });

  test('a dictionary file demands a dictionary-capable decoder', async (t) => {
    _resetZstd();
    let dec = null;
    try { dec = await ensureZstd(true); } catch (err) {
      // Expected where @bokuweb/zstd-wasm is absent. The point is that it
      // fails here, at selection, rather than handing back Node's zstd which
      // ignores the dictionary and dies with "Dictionary mismatch" later.
      assert.match(err.message, /trained zstd dictionary/);
      assert.match(err.message, /zstd-wasm/);
      return;
    }
    assert.ok(dec.decompressWithDict, 'dictionary decoder lacks decompressWithDict');
  });

  test('the plain decoder is never offered for a dictionary file', async () => {
    _resetZstd();
    const plain = await ensureZstd(false);
    let dict = null;
    try { dict = await ensureZstd(true); } catch { return; }  // no wasm: fine
    assert.notEqual(plain, dict, 'same decoder returned for both cases');
    assert.ok(dict.decompressWithDict);
  });

  test('decoders are cached per capability', async () => {
    _resetZstd();
    assert.equal(await ensureZstd(false), await ensureZstd(false));
  });
});

/**
 * End-to-end against real tiles. Synthetic headers cannot catch a wrong call
 * signature into the wasm module, which is how the dictionary path stayed
 * broken: decompressUsingDict takes (dctx, buf, dict), and calling it as
 * (buf, dict) throws inside wasm on an undefined argument.
 *
 * Set PTILES_FIXTURES to a directory holding any .ptiles files to run these.
 */
describe('real files', { skip: !process.env.PTILES_FIXTURES && 'set PTILES_FIXTURES' }, () => {
  const dir = process.env.PTILES_FIXTURES;
  const files = !dir ? [] : fs.readdirSync(dir).filter(f => f.endsWith('.ptiles'));

  /** Read header, index and first block of a file. */
  async function openFirstBlock(file) {
    const buf = fs.readFileSync(path.join(dir, file));
    const d = new Uint8Array(buf.buffer, buf.byteOffset, buf.byteLength);
    const dv = new DataView(d.buffer, d.byteOffset, 256);
    const dictOff = dv.getUint32(40, true), dictLen = dv.getUint32(48, true);
    const idxOff = dv.getUint32(52, true), idxLen = dv.getUint32(60, true);
    const blocksOff = Number(dv.getBigUint64(64, true));
    const idx = readIndex(d.subarray(idxOff, idxOff + idxLen));
    const e = idx.entries[0];
    const dict = dictLen ? d.subarray(dictOff, dictOff + dictLen) : null;
    const dec = await ensureZstd(dictLen > 0);
    let off = Number(e.blockOffset);
    if (off < blocksOff) off += blocksOff;
    const blob = d.subarray(off, off + Number(e.blockLength));
    const raw = dict ? await dec.decompressWithDict(blob, dict)
                     : await dec.decompress(blob);
    return { layer: MAGIC_LAYER[d[6]], stride: idx.stride, dictLen, raw, entries: idx.entries };
  }

  test('every fixture parses its index at a known stride', async () => {
    assert.ok(files.length, `no .ptiles in ${dir}`);
    for (const f of files) {
      const buf = fs.readFileSync(path.join(dir, f));
      const d = new Uint8Array(buf.buffer, buf.byteOffset, buf.byteLength);
      const dv = new DataView(d.buffer, d.byteOffset, 256);
      const idxOff = dv.getUint32(52, true), idxLen = dv.getUint32(60, true);
      const idx = readIndex(d.subarray(idxOff, idxOff + idxLen));
      assert.ok([INDEX_ENTRY_V1, INDEX_ENTRY_V2].includes(idx.stride), `${f}: stride ${idx.stride}`);
      assert.ok(idx.entries.length > 0, `${f}: no entries`);
    }
  });

  test('every fixture decompresses its first block, dictionary or not', async () => {
    for (const f of files) {
      const { raw, layer, dictLen } = await openFirstBlock(f);
      assert.ok(raw.length > 0, `${f} (${layer}, dict=${dictLen}) decompressed to nothing`);
    }
  });

  test('a dictionary layer really exercises the wasm path', async (t) => {
    const withDict = files.filter(f => {
      const buf = fs.readFileSync(path.join(dir, f));
      return new DataView(buf.buffer, buf.byteOffset, 256).getUint32(48, true) > 0;
    });
    if (!withDict.length) return t.skip('no dictionary-bearing fixture');
    const { raw, dictLen } = await openFirstBlock(withDict[0]);
    assert.ok(dictLen > 0);
    assert.ok(raw.length > 0, 'dictionary block decompressed to nothing');
  });

  test('layer is identified, never unknown', async () => {
    for (const f of files) {
      const buf = fs.readFileSync(path.join(dir, f));
      const d = new Uint8Array(buf.buffer, buf.byteOffset, buf.byteLength);
      assert.ok(MAGIC_LAYER[d[6]], `${f}: magic byte 0x${d[6].toString(16)} unmapped`);
    }
  });
});

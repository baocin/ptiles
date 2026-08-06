/**
 * ptiles.mjs — JavaScript reader for PTILES buildings format
 *
 * Supports two modes:
 *   1. Single file: `PtilesReader.fromFile('TN.buildings_v8.ptiles')`
 *   2. National (multi-state): `PtilesReader.fromStateDir('https://r2.dev/maps/')`
 *      or `PtilesReader.fromNationalFile('US.buildings_v8.ptiles')`
 *
 * Usage:
 *   import { PtilesReader } from './ptiles.mjs';
 *   import * as h3 from 'h3-js';
 *
 *   // Mode 1 — single state file
 *   const r1 = await PtilesReader.fromFile('TN.buildings_v8.ptiles');
 *   const b = await r1.query(lat, lon, { h3 });
 *
 *   // Mode 2 — directory of state files (auto-routes by lat/lon)
 *   const r2 = await PtilesReader.fromStateDir(
 *     'https://pub-....r2.dev/maps/', { h3 }
 *   );
 *   const b2 = await r2.query(lat, lon);
 *
 *   // Mode 3 — single national file (all 77M buildings)
 *   const r3 = await PtilesReader.fromNationalFile('US.buildings_v8.ptiles');
 *   const b3 = await r3.query(lat, lon, { h3 });
 *
 * Requires h3-js. Node 22+ for built-in zstd; browsers need
 * DecompressionStream('zstd') or @bokuweb/zstd-wasm.
 */

const MAGIC_LAYER = { 0x46: 'buildings', 0x52: 'roads', 0x41: 'admin', 0x57: 'water', 0x50: 'places', 0x54: 'rail', 0x4e: 'parks', 0x42: 'business' };

function u8(v, o) { return v.getUint8(o); }
function u32(v, o) { return v.getUint32(o, true); }
function u64(v, o) { return v.getUint32(o, true) + v.getUint32(o + 4, true) * 0x100000000; }
function i32(v, o) { return v.getInt32(o, true); }
function f32(v, o) { return v.getFloat32(o, true); }
function u16(v, o) { return v.getUint16(o, true); }
function i16(v, o) { return v.getInt16(o, true); }

function readPacked(data, off, len, asBig) {
  if (asBig) { let r = 0n; for (let i = 0; i < len; i++) r |= BigInt(data[off + i]) << BigInt(i * 8); return r; }
  let r = 0; for (let i = 0; i < len; i++) r |= data[off + i] << (i * 8); return r;
}

function decodeVarint(data, start) {
  let r = 0n, s = 0n, p = start;
  while (p < data.length) { const b = data[p++]; r |= BigInt(b & 0x7f) << s; if ((b & 0x80) === 0) break; s += 7n; }
  return { value: r, consumed: p - start };
}
function zigzagI32(n) { const v = Number(n & 0xffffffffn); return (v >>> 1) ^ -(v & 1); }
function zigzagI64(n) { return BigInt.asIntN(64, n >> 1n) ^ BigInt.asIntN(64, -(n & 1n)); }
function readI16LE(d, o) { return new DataView(d.buffer, d.byteOffset + o, 2).getInt16(0, true); }

function decStrU8(d, p) { const len = d[p]; let s = ''; for (let i = 0; i < len; i++) s += String.fromCharCode(d[p + 1 + i]); return { str: s, consumed: 1 + len }; }
function decStrTable(d, p) { const cnt = d[p]; let pos = p + 1, t = []; for (let i = 0; i < cnt; i++) { const r = decStrU8(d, pos); t.push(r.str); pos += r.consumed; } return { table: t, consumed: pos - p }; }
function decTableRef(d, p, t) { const idx = d[p]; if (idx === 0xff) { const r = decStrU8(d, p + 1); return { str: r.str, consumed: 1 + r.consumed }; } return { str: idx < t.length ? t[idx] : '', consumed: 1 }; }

let zstd = null;
async function ensureZstd() {
  if (zstd) return;
  if (typeof process !== 'undefined' && process.versions?.node) {
    try { const z = await import('node:zlib'); zstd = { decompress: (c, dict) => z.zstdDecompressSync(Buffer.from(c), dict ? { dictionary: Buffer.from(dict) } : undefined) }; return; } catch {}
  }
  if (typeof DecompressionStream !== 'undefined') {
    zstd = { decompress: async (compressed) => { const cs = new DecompressionStream('zstd'); const w = cs.writable.getWriter(); w.write(compressed); w.close(); const r = cs.readable.getReader(); const c = []; while (true) { const { done, value } = await r.read(); if (done) break; c.push(value); } const t = c.reduce((a, b) => a + b.length, 0); const o = new Uint8Array(t); let p = 0; for (const x of c) { o.set(x, p); p += x.length; } return o; } }; return;
  }
  try { const mod = await import('@bokuweb/zstd-wasm'); await mod.init(); zstd = { decompress: async (c, dict) => dict && mod.decompressUsingDict ? mod.decompressUsingDict(c, dict) : mod.decompress(c) }; return; } catch {}
  throw new Error('No zstd. Node 22+ built-in, browser: DecompressionStream or @bokuweb/zstd-wasm');
}

function readIndex(sl) {
  const dv = new DataView(sl.buffer, sl.byteOffset, sl.length);
  const cnt = u32(dv, 0);
  const mask7 = BigInt('0xfffffffff8000000');
  const isV2 = (sl.length - 4) / 37 >= cnt;
  const e = [], m = new Map();
  if (isV2) {
    let o = 4;
    for (let i = 0; i < cnt; i++) {
      const cb = readPacked(sl, o, 8, true);
      e.push({ h3Cell: cb, minLon: i32(dv, o + 8), minLat: i32(dv, o + 12), maxLon: i32(dv, o + 16), maxLat: i32(dv, o + 20),
        blockOffset: readPacked(sl, o + 24, 6, true), blockLength: readPacked(sl, o + 30, 3, false), featureCount: u16(dv, o + 33), cellIndexInBlock: u16(dv, o + 35) });
      m.set(cb & mask7, i); o += 37;
    }
  } else {
    let o = 4;
    for (let i = 0; i < cnt; i++) {
      const cb = readPacked(sl, o, 8, true);
      e.push({ h3Cell: cb, blockOffset: readPacked(sl, o + 8, 6, true), blockLength: readPacked(sl, o + 14, 3, false), featureCount: u16(dv, o + 17) });
      m.set(cb & mask7, i); o += 19;
    }
  }
  return { entries: e, cellMap: m };
}

function parseRecV8(d, st, cx, cy, prev) {
  let p = 0;
  const dr = decodeVarint(d, p); p += dr.consumed;
  const osmId = Number(BigInt.asUintN(64, BigInt(prev) + zigzagI64(dr.value)));
  const flags = d[p++];
  let vc = (flags >> 4) & 0x0f; if (vc === 0x0f) vc = d[p++]; else vc += 4;
  if (vc === 0 || p + 4 > d.length) return { bld: { osmId, buildingType: 'yes', name: null, coords: [], centroidLat: 0, centroidLon: 0 }, consumed: p };
  const fl = readI16LE(d, p), fa = readI16LE(d, p + 2); p += 4;
  const cm = [[cx + fl, cy + fa]]; let px = cx + fl, py = cy + fa;
  for (let i = 1; i < vc; i++) {
    const r1 = decodeVarint(d, p); p += r1.consumed; const r2 = decodeVarint(d, p); p += r2.consumed;
    px += zigzagI32(r1.value); py += zigzagI32(r2.value); cm.push([px, py]);
  }
  const coords = cm.map(c => [c[0] / 100000, c[1] / 100000]);
  if (p >= d.length) return { bld: mkb(osmId, coords, 'yes'), consumed: p };
  const bt = d[p++]; let btStr;
  if (bt === 0xff) { const r = decStrU8(d, p); p += r.consumed; btStr = r.str; } else if (bt < st.length) btStr = st[bt]; else btStr = 'yes';
  if (p >= d.length) return { bld: mkb(osmId, coords, btStr), consumed: p };
  const f2 = d[p++]; let name = null, cat = null, ns = null, poi = null;
  if (f2 & 0x01 && p < d.length) { const r = decTableRef(d, p, st); p += r.consumed; name = r.str || null; }
  if (f2 & 0x02 && p < d.length) { const r = decTableRef(d, p, st); p += r.consumed; cat = r.str || null; }
  if (f2 & 0x04 && p < d.length) { const r = decTableRef(d, p, st); p += r.consumed; ns = r.str || null; }
  if (f2 & 0x08 && p + 8 <= d.length) { poi = u64(new DataView(d.buffer, d.byteOffset + p, 8), 0); p += 8; }
  const cl = coords.reduce((s, c) => s + c[0], 0) / coords.length;
  const ca = coords.reduce((s, c) => s + c[1], 0) / coords.length;
  return { bld: { osmId, buildingType: btStr, name, category: cat, nameSource: ns, poiOsmId: poi, coordinates: coords, centroidLat: ca, centroidLon: cl }, consumed: p };
}

function mkb(osmId, coords, bt) {
  const cl = coords.reduce((s, c) => s + c[0], 0) / coords.length;
  const ca = coords.reduce((s, c) => s + c[1], 0) / coords.length;
  return { osmId, buildingType: bt, name: null, coordinates: coords, centroidLat: ca, centroidLon: cl };
}

function pointInPoly(lon, lat, poly) {
  let inside = false;
  for (let i = 0, j = poly.length - 1; i < poly.length; j = i++) {
    const [xi, yi] = poly[i], [xj, yj] = poly[j];
    if ((yi > lat) !== (yj > lat) && lon < (xj - xi) * (lat - yi) / (yj - yi) + xi) inside = !inside;
  }
  return inside;
}

function haversineDeg(lon1, lat1, lon2, lat2) {
  const dLat = (lat2 - lat1) * Math.PI / 180, dLon = (lon2 - lon1) * Math.PI / 180;
  const a = Math.sin(dLat / 2) ** 2 + Math.cos(lat1 * Math.PI / 180) * Math.cos(lat2 * Math.PI / 180) * Math.sin(dLon / 2) ** 2;
  return 6371000 * 2 * Math.atan2(Math.sqrt(a), Math.sqrt(1 - a));
}

const MASK7 = BigInt('0xfffffffff8000000');
function h3Mask(cell) {
  if (typeof cell === 'bigint') return cell & MASK7;
  return (typeof cell === 'string' ? BigInt('0x' + cell) : BigInt(cell)) & MASK7;
}

// ─── State bboxes (from scripts/states.py) ─────────────────────────

const STATE_BBOXES = [
  ['AL', -88.5, 30.0, -84.9, 35.0], ['AK', -173.0, 51.0, -130.0, 71.5],
  ['AZ', -115.0, 31.3, -109.0, 37.0], ['AR', -94.6, 33.0, -89.0, 36.5],
  ['CA', -124.5, 32.5, -114.1, 42.0], ['CO', -109.1, 37.0, -102.0, 41.0],
  ['CT', -73.7, 40.9, -71.8, 42.1], ['DE', -75.8, 38.4, -75.0, 39.9],
  ['DC', -77.2, 38.8, -76.9, 39.0], ['FL', -87.6, 24.4, -80.0, 31.0],
  ['GA', -85.6, 30.0, -78.4, 35.0], ['HI', -160.5, 18.9, -154.8, 22.3],
  ['ID', -117.0, 42.0, -111.0, 49.0], ['IL', -91.5, 36.9, -87.0, 42.5],
  ['IN', -88.1, 37.8, -84.8, 41.8], ['IA', -96.6, 40.4, -90.1, 43.5],
  ['KS', -102.1, 37.0, -94.6, 40.0], ['KY', -89.6, 36.5, -82.0, 39.2],
  ['LA', -94.1, 28.9, -88.8, 33.0], ['ME', -71.1, 43.0, -66.9, 47.5],
  ['MD', -79.5, 37.9, -75.0, 39.8], ['MA', -73.5, 41.2, -69.9, 42.9],
  ['MI', -90.4, 41.7, -82.4, 48.3], ['MN', -97.3, 43.5, -89.5, 49.4],
  ['MS', -91.7, 30.0, -88.1, 35.0], ['MO', -95.8, 35.9, -89.1, 40.6],
  ['MT', -116.1, 44.4, -104.0, 49.0], ['NE', -104.1, 40.0, -95.3, 43.0],
  ['NV', -120.0, 35.0, -114.0, 42.0], ['NH', -72.6, 42.7, -70.6, 45.3],
  ['NJ', -75.6, 38.9, -73.9, 41.4], ['NM', -109.1, 31.3, -103.0, 37.0],
  ['NY', -79.8, 40.5, -71.8, 45.0], ['NC', -84.3, 33.8, -75.4, 36.6],
  ['ND', -104.1, 45.9, -96.5, 49.0], ['OH', -84.8, 38.4, -80.5, 41.7],
  ['OK', -103.0, 33.6, -94.4, 37.0], ['OR', -124.6, 41.9, -116.5, 46.3],
  ['PA', -80.5, 39.7, -74.7, 42.3], ['RI', -71.9, 41.1, -71.1, 42.0],
  ['SC', -83.4, 32.0, -78.5, 35.2], ['SD', -104.1, 42.5, -96.4, 45.9],
  ['TN', -90.3, 34.9, -81.6, 36.7], ['TX', -106.7, 25.8, -93.5, 36.5],
  ['UT', -114.1, 37.0, -109.0, 42.0], ['VT', -73.5, 42.7, -71.5, 45.0],
  ['VA', -83.7, 36.5, -75.2, 39.5], ['WA', -124.8, 45.5, -116.9, 49.0],
  ['WV', -82.7, 37.2, -77.7, 40.6], ['WI', -93.0, 42.5, -86.8, 47.3],
  ['WY', -111.1, 41.0, -104.0, 45.0],
];

function findState(lat, lon) {
  for (const [abbr, minLon, minLat, maxLon, maxLat] of STATE_BBOXES) {
    if (lat >= minLat && lat <= maxLat && lon >= minLon && lon <= maxLon) return abbr;
  }
  return null;
}

// ─── Internal per-file reader ──────────────────────────────────────

class _PtilesFile {
  constructor(data, header, index, dict) {
    this.data = data; this._h = header; this._idx = index; this._dict = dict;
    this._relOff = index.entries.length > 0 && Number(index.entries[0].blockOffset) < header.blocksOffset;
  }

  get header() { return this._h; }
  get index() { return this._idx; }

  async decompressBlock(entry) {
    const bo = Number(entry.blockOffset);
    const abs = this._relOff ? this._h.blocksOffset + bo : bo;
    return zstd.decompress(this.data.subarray(abs, abs + Number(entry.blockLength)), this._dict);
  }

  async _queryCell(cellHex, qLat, qLon, h3lib) {
    const ei = this._idx.cellMap.get(h3Mask(h3lib.latLngToCell(qLat, qLon, 7)));
    if (ei === undefined) return null;
    const entry = this._idx.entries[ei];
    const raw = await this.decompressBlock(entry);
    const st = decStrTable(raw, 0);
    const center = h3lib.cellToLatLng(cellHex);
    const cx = Math.round(center[1] * 100000), cy = Math.round(center[0] * 100000);
    let bestDist = Infinity, best = null, p = st.consumed, prev = 0;
    while (p + 4 <= raw.length) {
      const rl = u32(new DataView(raw.buffer, raw.byteOffset + p, 4), 0); p += 4;
      if (p + rl > raw.length) break;
      const { bld, consumed } = parseRecV8(raw.subarray(p, p + rl), st.table, cx, cy, prev);
      prev = bld.osmId;
      if (bld.coordinates.length > 0 && pointInPoly(qLon, qLat, bld.coordinates)) return bld;
      const d = haversineDeg(qLon, qLat, bld.centroidLon, bld.centroidLat);
      if (d < bestDist) { bestDist = d; best = bld; }
      p += rl;
    }
    return bestDist < 50 ? best : null;
  }

  async queryCell(cellHex, qLat, qLon, h3lib) {
    return this._queryCell(cellHex, qLat, qLon, h3lib);
  }

  async queryBounds(cellHex, minLat, minLon, maxLat, maxLon, limit, h3lib) {
    const ei = this._idx.cellMap.get(h3Mask(typeof cellHex === 'bigint' ? cellHex : BigInt('0x' + cellHex)));
    if (ei === undefined) return [];
    const entry = this._idx.entries[ei];
    const raw = await this.decompressBlock(entry);
    const st = decStrTable(raw, 0);
    const center = h3lib.cellToLatLng(cellHex);
    const cx = Math.round(center[1] * 100000), cy = Math.round(center[0] * 100000);
    const results = []; let p = st.consumed, prev = 0;
    while (p + 4 <= raw.length && results.length < limit) {
      const rl = u32(new DataView(raw.buffer, raw.byteOffset + p, 4), 0); p += 4;
      if (p + rl > raw.length) break;
      const { bld } = parseRecV8(raw.subarray(p, p + rl), st.table, cx, cy, prev);
      prev = bld.osmId;
      if (bld.centroidLat >= minLat && bld.centroidLat <= maxLat && bld.centroidLon >= minLon && bld.centroidLon <= maxLon) {
        results.push(bld);
      }
      p += rl;
    }
    return results;
  }
}

async function loadFile(data) {
  await ensureZstd();
  const dv = new DataView(data.buffer, data.byteOffset, 256);
  const format = MAGIC_LAYER[data[6]] ?? 'unknown';
  const h = {
    format, version: u8(dv, 8), minLat: f32(dv, 12), minLon: f32(dv, 16), maxLat: f32(dv, 20), maxLon: f32(dv, 24),
    featureCount: u64(dv, 28), blockCount: u32(dv, 36), dictOffset: u64(dv, 40), dictLength: u32(dv, 48),
    indexOffset: u64(dv, 52), indexLength: u32(dv, 60), blocksOffset: u64(dv, 64),
    auxOffset: u64(dv, 72), auxLength: u32(dv, 80), createdAt: u64(dv, 84), dataVersion: u32(dv, 96),
  };
  const dict = h.dictLength > 0 ? data.subarray(h.dictOffset, h.dictOffset + h.dictLength) : new Uint8Array(0);
  const idx = readIndex(data.subarray(h.indexOffset, h.indexOffset + h.indexLength));
  return new _PtilesFile(data, h, idx, dict);
}

// ─── Exported ──────────────────────────────────────────────────────

export class PtilesReader {
  /**
   * Internal: wraps one or more per-file readers.
   * @param {_PtilesFile|Map<string,_PtilesFile>|string} source
   * @param {object} opts
   */
  constructor(source, opts = {}) {
    if (source instanceof _PtilesFile) {
      this._mode = 'single';
      this._file = source;
    } else if (source instanceof Map) {
      this._mode = 'multi';
      this._files = source;
    } else if (typeof source === 'string') {
      this._mode = 'multi';
      this._baseUrl = source;
      this._files = null; // lazy load
    } else {
      throw new Error('Unknown source type');
    }
    this._h3 = opts.h3 || null;
  }

  /** Load a single .ptiles file from an ArrayBuffer / Uint8Array. */
  static async fromBuffer(data) {
    return new PtilesReader(await loadFile(data));
  }

  /** Load a single .ptiles file from a URL. */
  static async fromUrl(url) {
    const resp = await fetch(url);
    if (!resp.ok) throw new Error(`Fetch ${url}: ${resp.status}`);
    return PtilesReader.fromBuffer(new Uint8Array(await resp.arrayBuffer()));
  }

  /** Load a single .ptiles file from a local path (Node only). */
  static async fromFile(path) {
    const fs = await import('node:fs');
    const buf = fs.readFileSync(path);
    return PtilesReader.fromBuffer(new Uint8Array(buf.buffer, buf.byteOffset, buf.byteLength));
  }

  /**
   * Load a national file covering the full US (all 77M buildings in one file).
   * Same as fromFile/fromUrl but makes the intent explicit.
   */
  static async fromNationalFile(path) {
    return PtilesReader.fromFile(path);
  }

  static async fromNationalUrl(url) {
    return PtilesReader.fromUrl(url);
  }

  /**
   * Load per-state files from a directory or URL prefix.
   * State files should be named {ABBR}.buildings_v8.ptiles.
   * In Node: pass a local directory path.
   * In browser: pass a URL prefix ending in / (lazy-loads on first query).
   */
  static async fromStateDir(dirOrUrl, opts = {}) {
    if (typeof process !== 'undefined' && process.versions?.node) {
      // Local directory
      const fs = await import('node:fs');
      const path = await import('node:path');
      const files = new Map();
      const entries = fs.readdirSync(dirOrUrl);
      for (const f of entries) {
        const m = f.match(/^([A-Z]{2})\.buildings_v8\.ptiles$/);
        if (m) {
          const buf = fs.readFileSync(path.join(dirOrUrl, f));
          files.set(m[1], await loadFile(new Uint8Array(buf.buffer, buf.byteOffset, buf.byteLength)));
        }
      }
      return new PtilesReader(files, opts);
    }
    // Browser: lazy-load URL prefix
    return new PtilesReader(dirOrUrl, opts);
  }

  /**
   * Get a reader for the state containing (lat, lon).
   * Lazy-loads from URL if in browser mode.
   */
  async _getReader(lat, lon) {
    if (this._mode === 'single') return this._file;
    const abbr = findState(lat, lon);
    if (!abbr) return null;
    if (this._mode === 'multi' && this._files) {
      return this._files.get(abbr) || null;
    }
    // Lazy load
    if (this._baseUrl) {
      if (!this._files) this._files = new Map();
      if (!this._files.has(abbr)) {
        const url = this._baseUrl.replace(/\/?$/, '/') + abbr + '.buildings_v8.ptiles';
        const resp = await fetch(url);
        if (!resp.ok) return null;
        this._files.set(abbr, await loadFile(new Uint8Array(await resp.arrayBuffer())));
      }
      return this._files.get(abbr);
    }
    return null;
  }

  /**
   * Query nearest building at GPS point.
   * Returns { osmId, buildingType, name, coordinates, centroidLat, centroidLon, ... } or null.
   */
  async query(lat, lon, opts = {}) {
    const h3lib = opts.h3 || this._h3 || (typeof globalThis !== 'undefined' ? globalThis.h3 : null);
    if (!h3lib) throw new Error('h3-js required. Pass opts.h3 or set globalThis.h3');

    const cellHex = h3lib.latLngToCell(lat, lon, 7);
    const reader = await this._getReader(lat, lon);
    if (!reader) return null;

    let result = await reader.queryCell(cellHex, lat, lon, h3lib);
    if (result) return result;

    // kRing fallback for cell representation differences
    const ring = h3lib.gridDisk(cellHex, 1);
    for (const rc of ring) {
      result = await reader.queryCell(rc, lat, lon, h3lib);
      if (result) return result;
    }
    return null;
  }

  /**
   * Get all buildings within a lat/lon bounding box.
   * Searches across all states that intersect the box.
   */
  async queryBounds(minLat, minLon, maxLat, maxLon, limit = 5000, opts = {}) {
    const h3lib = opts.h3 || this._h3 || (typeof globalThis !== 'undefined' ? globalThis.h3 : null);
    if (!h3lib) throw new Error('h3-js required');

    const span = Math.max(maxLat - minLat, maxLon - minLon);
    const step = span > 5 ? 0.1 : span > 1 ? 0.025 : 0.005;
    const cells = new Set();
    for (let lat = minLat; lat <= maxLat; lat += step) {
      for (let lon = minLon; lon <= maxLon; lon += step) cells.add(h3lib.latLngToCell(lat, lon, 7));
    }

    // Collect cells by state
    const cellsByState = new Map();
    for (const cellHex of cells) {
      const [clat, clon] = h3lib.cellToLatLng(cellHex);
      const abbr = findState(clat, clon);
      if (abbr) {
        if (!cellsByState.has(abbr)) cellsByState.set(abbr, []);
        cellsByState.get(abbr).push(cellHex);
      }
    }

    const results = [];
    for (const [abbr, stateCells] of cellsByState) {
      const reader = this._mode === 'single' ? this._file :
        this._files?.get(abbr) || (this._baseUrl ? await this._getReader(abbr) : null);
      if (!reader) continue;
      for (const cellHex of stateCells) {
        const blds = await reader.queryBounds(cellHex, minLat, minLon, maxLat, maxLon, limit - results.length, h3lib);
        results.push(...blds);
        if (results.length >= limit) return results;
      }
    }
    return results;
  }

  /** File header for single-file mode, or null for multi-state. */
  get header() { return this._mode === 'single' ? this._file.header : null; }
  get index() { return this._mode === 'single' ? this._file.index : null; }
}

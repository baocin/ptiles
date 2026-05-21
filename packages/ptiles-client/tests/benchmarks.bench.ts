// tests/benchmarks.test.ts — performance benchmarks for ptiles-client
//
// These benchmarks measure the operations that ARE implemented in JS:
// header parsing, index parsing, codec operations, and file I/O.

import { describe, bench } from 'vitest';
import { readFileSync } from 'fs';
import { parseHeader } from '../src/header.js';
import { parseIndex, lookupCell, detectRelativeOffsets } from '../src/spatial-index.js';
import {
  readVarint,
  zigzagDecode,
  encodeZigzagVarint,
} from '../src/codec.js';

const DATA_DIR = '/home/aoi/kino/projects/ptiles/data/states';
const FILES = {
  business: `${DATA_DIR}/TN.business.ptiles`,
  roads: `${DATA_DIR}/TN.roads.ptiles`,
  buildings: `${DATA_DIR}/TN.buildings_v8.ptiles`,
  water: `${DATA_DIR}/TN.water.ptiles`,
};

// ---------------------------------------------------------------------------
// Pre-load all file buffers so header/index parse benchmarks measure
// only parsing, not disk I/O.
// ---------------------------------------------------------------------------
const bufBusiness = new Uint8Array(readFileSync(FILES.business));
const bufRoads = new Uint8Array(readFileSync(FILES.roads));
const bufBuildings = new Uint8Array(readFileSync(FILES.buildings));
const bufWater = new Uint8Array(readFileSync(FILES.water));

// Pre-parse headers for index-related benchmarks
const headerBusiness = parseHeader(bufBusiness);
const headerRoads = parseHeader(bufRoads);
const headerBuildings = parseHeader(bufBuildings);
const headerWater = parseHeader(bufWater);

// Pre-slice index buffers
const idxBufBusiness = bufBusiness.slice(
  headerBusiness.index_offset,
  headerBusiness.index_offset + headerBusiness.index_length,
);
const idxBufRoads = bufRoads.slice(
  headerRoads.index_offset,
  headerRoads.index_offset + headerRoads.index_length,
);

// Pre-parse indices for lookup benchmark
const entriesBusiness = parseIndex(idxBufBusiness);
const entriesRoads = parseIndex(idxBufRoads);

// ---------------------------------------------------------------------------
// Build a varint buffer for decode benchmark (1000 varints)
// ---------------------------------------------------------------------------
function buildVarintBuffer(count: number): Uint8Array {
  const bytes: number[] = [];
  for (let i = 0; i < count; i++) {
    // Encode (i * 7 + 5) — a nice spread of small/medium varints
    let v = (i * 7 + 5) >>> 0;
    while (v >= 0x80) {
      bytes.push((v & 0x7f) | 0x80);
      v >>>= 7;
    }
    bytes.push(v & 0x7f);
  }
  return new Uint8Array(bytes);
}

const VARINT_BUF = buildVarintBuffer(1000);

// ---------------------------------------------------------------------------
// Generate 1000 zigzag test values (mix of positive, negative, small, large)
// ---------------------------------------------------------------------------
const ZIGZAG_VALUES: number[] = [];
for (let i = 0; i < 1000; i++) {
  ZIGZAG_VALUES.push(i % 2 === 0 ? i * 123 + 17 : -(i * 89 + 3));
}

// ---------------------------------------------------------------------------
// Pick a known cell from the business index for lookup benchmark
// ---------------------------------------------------------------------------
const lookupCellTarget = entriesBusiness.length > 0
  ? entriesBusiness[Math.floor(entriesBusiness.length / 2)].h3_cell
  : 0n;

// ---------------------------------------------------------------------------
// BENCHMARKS
// ---------------------------------------------------------------------------

describe('header_parse', () => {
  bench('header_parse_business', () => {
    parseHeader(bufBusiness);
  });

  bench('header_parse_roads', () => {
    parseHeader(bufRoads);
  });

  bench('header_parse_buildings', () => {
    parseHeader(bufBuildings);
  });

  bench('header_parse_water', () => {
    parseHeader(bufWater);
  });
});

describe('index_parse', () => {
  bench('index_parse_business', () => {
    parseIndex(idxBufBusiness);
  });

  bench('index_parse_roads', () => {
    parseIndex(idxBufRoads);
  });
});

describe('index_lookup', () => {
  bench('index_lookup', () => {
    // Perform 1000 binary-search lookups
    for (let i = 0; i < 1000; i++) {
      lookupCell(entriesBusiness, lookupCellTarget);
    }
  });
});

describe('codec', () => {
  bench('varint_decode', () => {
    let offset = 0;
    for (let i = 0; i < 1000; i++) {
      const [_, consumed] = readVarint(VARINT_BUF, offset);
      offset += consumed;
    }
  });

  bench('zigzag_roundtrip', () => {
    for (let i = 0; i < 1000; i++) {
      const v = ZIGZAG_VALUES[i];
      const bytes = encodeZigzagVarint(v);
      // Decode back from the bytes
      const buf = new Uint8Array(bytes);
      const [raw, _] = readVarint(buf, 0);
      const decoded = zigzagDecode(raw);
      // Keep decoded alive (prevent dead-code elimination)
      if (decoded !== v) {
        throw new Error(`Zigzag roundtrip mismatch: ${v} !== ${decoded}`);
      }
    }
  });
});

describe('cross_file', () => {
  bench('cross_file_all_headers', () => {
    // Read and parse headers of all 4 TN files sequentially
    const b1 = readFileSync(FILES.business);
    parseHeader(new Uint8Array(b1.buffer, b1.byteOffset, b1.byteLength));

    const b2 = readFileSync(FILES.roads);
    parseHeader(new Uint8Array(b2.buffer, b2.byteOffset, b2.byteLength));

    const b3 = readFileSync(FILES.buildings);
    parseHeader(new Uint8Array(b3.buffer, b3.byteOffset, b3.byteLength));

    const b4 = readFileSync(FILES.water);
    parseHeader(new Uint8Array(b4.buffer, b4.byteOffset, b4.byteLength));
  });
});

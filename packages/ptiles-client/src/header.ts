// src/header.ts — parseHeader, Format enum

import { Header } from './types.js';

/**
 * Seven-byte magic codes identifying each PTILES layer.
 */
export const MAGIC_TO_FORMAT: Record<string, string> = {
  'PTILESF': 'Buildings',
  'PTILESR': 'Roads',
  'PTILESA': 'Admin',
  'PTILESW': 'Water',
  'PTILESP': 'Places',
  'PTILEST': 'Rail',
  'PTILESN': 'Parks',
  'PTILESB': 'Business',
  'PTILESU': 'Routing',
};

export const FORMAT_TO_MAGIC: Record<string, string> = {
  'Buildings': 'PTILESF',
  'Roads': 'PTILESR',
  'Admin': 'PTILESA',
  'Water': 'PTILESW',
  'Places': 'PTILESP',
  'Rail': 'PTILEST',
  'Parks': 'PTILESN',
  'Business': 'PTILESB',
  'Routing': 'PTILESU',
};

/**
 * File suffixes for each format.
 */
export const FORMAT_SUFFIX: Record<string, string[]> = {
  'Buildings': ['buildings_v8'],
  'Roads': ['roads'],
  'Water': ['water'],
  'Business': ['business'],
  'Places': ['places'],
  'Rail': ['rail'],
  'Parks': ['parks'],
  'Admin': ['admin'],
  'Routing': ['routing'],
};

/**
 * Parse a 256-byte PTILES header buffer.
 * Throws on magic mismatch.
 */
export function parseHeader(buffer: Uint8Array): Header {
  if (buffer.length < 100) {
    throw new Error(`Header too short: ${buffer.length} bytes (expected >= 100)`);
  }

  const magicBytes = new TextDecoder().decode(buffer.slice(0, 7));
  const format = MAGIC_TO_FORMAT[magicBytes];

  if (!format) {
    const hex = Array.from(buffer.slice(0, 7))
      .map(b => b.toString(16).padStart(2, '0'))
      .join(' ');
    throw new Error(`Unknown PTILES magic: ${magicBytes} (hex: ${hex})`);
  }

  const version = buffer[8];

  const minLat = readF32LE(buffer, 12);
  const minLon = readF32LE(buffer, 16);
  const maxLat = readF32LE(buffer, 20);
  const maxLon = readF32LE(buffer, 24);
  const featureCount = readU64LE(buffer, 28);
  const blockCount = readU32LE(buffer, 36);
  const dictOffset = readU64LE(buffer, 40);
  const dictLength = readU32LE(buffer, 48);
  const indexOffset = readU64LE(buffer, 52);
  const indexLength = readU32LE(buffer, 60);
  const blocksOffset = readU64LE(buffer, 64);
  const auxOffset = readU64LE(buffer, 72);
  const auxLength = readU32LE(buffer, 80);
  const createdAt = readU64LE(buffer, 84);
  const dataVersion = readU32LE(buffer, 96);

  return {
    format,
    version,
    min_lat: minLat,
    min_lon: minLon,
    max_lat: maxLat,
    max_lon: maxLon,
    feature_count: featureCount,
    block_count: blockCount,
    dict_offset: dictOffset,
    dict_length: dictLength,
    index_offset: indexOffset,
    index_length: indexLength,
    blocks_offset: blocksOffset,
    aux_offset: auxOffset,
    aux_length: auxLength,
    created_at: createdAt,
    data_version: dataVersion,
  };
}

function readF32LE(data: Uint8Array, offset: number): number {
  const view = new DataView(data.buffer, data.byteOffset + offset, 4);
  return view.getFloat32(0, true);
}

function readU32LE(data: Uint8Array, offset: number): number {
  return data[offset] | (data[offset + 1] << 8) | (data[offset + 2] << 16) | (data[offset + 3] << 24);
}

function readU64LE(data: Uint8Array, offset: number): number {
  const low = readU32LE(data, offset);
  const high = readU32LE(data, offset + 4);
  // JS numbers are safe up to 2^53
  return high * 0x100000000 + low;
}

// src/header.ts — parseHeader, Format enum

import { Header } from './types.js';

/**
 * Seven-byte magic codes identifying each PTILES layer.
 *
 * Every magic that appears in a published file must be listed here, because
 * parseHeader throws on anything it does not recognise — a file the map is
 * missing is a file the client cannot open at all.
 */
export const MAGIC_TO_FORMAT: Record<string, string> = {
  'PTILESF': 'Buildings',
  'PTILESR': 'Roads',
  'PTILESA': 'Admin',
  'PTILESD': 'Address',
  'PTILESW': 'Water',
  'PTILESP': 'Places',
  'PTILEST': 'Rail',
  'PTILESN': 'Parks',
  'PTILESB': 'Business',
  'PTILESX': 'BusinessNameIndex',
  'PTILESC': 'Camera',
  'PTILESS': 'Signals',
  'PTILESU': 'Routing',
};

export const FORMAT_TO_MAGIC: Record<string, string> = {
  'Buildings': 'PTILESF',
  'Roads': 'PTILESR',
  'Admin': 'PTILESA',
  'Address': 'PTILESD',
  'Water': 'PTILESW',
  'Places': 'PTILESP',
  'Rail': 'PTILEST',
  'Parks': 'PTILESN',
  'Business': 'PTILESB',
  'BusinessNameIndex': 'PTILESX',
  'Camera': 'PTILESC',
  'Signals': 'PTILESS',
  'Routing': 'PTILESU',
};

/**
 * Filename suffixes for each format, newest first.
 *
 * Used to resolve the Admin/Address magic collision described below, so the
 * order matters: the first entry is what the current build publishes.
 */
export const FORMAT_SUFFIX: Record<string, string[]> = {
  'Buildings': ['buildings_v9', 'buildings_v8', 'buildings'],
  'Roads': ['highways_v2', 'roads'],
  'Water': ['water_v1', 'water'],
  'Business': ['business_v4', 'business'],
  'BusinessNameIndex': ['business_name_index'],
  'Places': ['places_v1', 'places'],
  'Rail': ['rail_v1', 'rail'],
  'Parks': ['parks_v1', 'parks'],
  'Admin': ['admin'],
  'Address': ['address_v1', 'address', 'addr'],
  'Camera': ['camera'],
  'Signals': ['signals'],
  'Routing': ['routing'],
};

/**
 * Address files published up to and including v4-20260711 carry PTILESA — the
 * Admin magic — because the builder set a nine-byte magic that the header's
 * seven-byte field truncated. The magic alone therefore cannot distinguish an
 * Address file from an Admin one, and no version byte helps: both are v1.
 *
 * The filename can. Admin ships as a single national US.admin.ptiles; Address
 * ships per state as {ST}.address_v1.ptiles.
 */
const LEGACY_ADDRESS_MAGIC = 'PTILESA';

function looksLikeAddress(source: string): boolean {
  const name = source.split(/[\\/]/).pop() ?? source;
  return FORMAT_SUFFIX['Address'].some(
    suffix => name.includes(`.${suffix}.`) || name.endsWith(`.${suffix}.ptiles`),
  );
}

/**
 * Parse a 256-byte PTILES header buffer.
 * Throws on an unrecognised magic.
 *
 * Pass the filename or URL as `source` when you have it. It is only consulted
 * to resolve the Admin/Address magic collision in files built before the
 * PTILESD fix; every other layer is identified by magic alone.
 */
export function parseHeader(buffer: Uint8Array, source?: string): Header {
  if (buffer.length < 100) {
    throw new Error(`Header too short: ${buffer.length} bytes (expected >= 100)`);
  }

  const magicBytes = new TextDecoder().decode(buffer.slice(0, 7));
  let format = MAGIC_TO_FORMAT[magicBytes];

  if (!format) {
    const hex = Array.from(buffer.slice(0, 7))
      .map(b => b.toString(16).padStart(2, '0'))
      .join(' ');
    throw new Error(`Unknown PTILES magic: ${magicBytes} (hex: ${hex})`);
  }

  // Reclassify a legacy address file that a truncated magic left looking like
  // Admin. Only ever narrows PTILESA -> Address, and only on filename
  // evidence, so an Admin file without a telling name is untouched.
  let legacyMagic = false;
  if (magicBytes === LEGACY_ADDRESS_MAGIC && source && looksLikeAddress(source)) {
    format = 'Address';
    legacyMagic = true;
  }

  // A genuinely ambiguous case: PTILESA with nothing to go on.
  const formatAmbiguous =
    magicBytes === LEGACY_ADDRESS_MAGIC && (!source || !looksLikeAddress(source));

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
    magic: magicBytes,
    legacy_magic: legacyMagic,
    format_ambiguous: formatAmbiguous,
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

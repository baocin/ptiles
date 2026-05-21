// src/codec.ts — varint, zigzag, coordinate decode, string decode

/**
 * Read a LEB128 unsigned varint from a Uint8Array at the given offset.
 * Returns [value, bytes_consumed].
 */
export function readVarint(data: Uint8Array, offset: number): [number, number] {
  let result = 0;
  let shift = 0;
  let pos = offset;
  while (pos < data.length) {
    const byte = data[pos++];
    result |= (byte & 0x7f) << shift;
    if (!(byte & 0x80)) {
      return [result, pos - offset];
    }
    shift += 7;
    if (shift > 63) {
      throw new Error('Varint too long');
    }
  }
  throw new Error('Unexpected end of data while reading varint');
}

/**
 * Decode a zigzag-encoded signed integer back to signed.
 * Works for both 32-bit and 64-bit values.
 */
export function zigzagDecode(value: number): number {
  return (value >>> 1) ^ -(value & 1);
}

/**
 * Decode a 64-bit zigzag value as a JavaScript number.
 * JS numbers are safe up to 2^53.
 */
export function zigzagDecode64(value: number): number {
  return (value >>> 1) ^ -(value & 1);
}

/**
 * Encode a signed integer to zigzag + varint.
 * Returns the varint bytes.
 */
export function encodeZigzagVarint(value: number): number[] {
  const unsigned = (value << 1) ^ (value >> 31);
  const bytes: number[] = [];
  let v = unsigned >>> 0;
  while (v >= 0x80) {
    bytes.push((v & 0x7f) | 0x80);
    v >>>= 7;
  }
  bytes.push(v & 0x7f);
  return bytes;
}

/**
 * Read a zigzag-varint (signed) from data at offset.
 * Returns [value, bytes_consumed].
 */
export function readZigzagVarint(data: Uint8Array, offset: number): [number, number] {
  const [raw, consumed] = readVarint(data, offset);
  return [zigzagDecode(raw), consumed];
}

/**
 * Decode coordinates from a buffer.
 * First vertex is absolute (i32 lon, i32 lat) — 8 bytes.
 * Subsequent vertices are zigzag varint deltas.
 * Returns [lon, lat] pairs as float64 degrees.
 */
export function decodeCoordinates(
  data: Uint8Array,
  offset: number,
  vertexCount: number,
  version?: number
): { coords: [number, number][]; bytesConsumed: number } {
  const coords: [number, number][] = [];
  let pos = offset;

  if (vertexCount === 0) {
    return { coords, bytesConsumed: 0 };
  }

  // First vertex: absolute i32 (lon, lat) = 8 bytes little-endian
  const firstLon = readI32LE(data, pos);
  pos += 4;
  const firstLat = readI32LE(data, pos);
  pos += 4;

  coords.push([firstLon / 100_000, firstLat / 100_000]);

  let prevLon = firstLon;
  let prevLat = firstLat;

  if (version !== undefined && version >= 7) {
    // Wall-segment encoding: (angle_byte, length_byte) for each subsequent vertex
    for (let i = 1; i < vertexCount; i++) {
      const angleByte = data[pos++];
      const lengthByte = data[pos++];
      const bearingRad = (angleByte * 360 / 256) * Math.PI / 180;
      const lengthM = lengthByte * 0.2;
      const prevLatRad = prevLat / 100_000 * Math.PI / 180;
      const dLat = (lengthM * Math.cos(bearingRad)) / 111320;
      const dLon = (lengthM * Math.sin(bearingRad)) / (111320 * Math.cos(prevLatRad));
      const newLat = prevLat / 100_000 + dLat;
      const newLon = prevLon / 100_000 + dLon;
      coords.push([newLon, newLat]);
      prevLon = newLon * 100_000;
      prevLat = newLat * 100_000;
    }
  } else {
    // Zigzag varint deltas
    for (let i = 1; i < vertexCount; i++) {
      const [dlon, c1] = readZigzagVarint(data, pos);
      pos += c1;
      const [dlat, c2] = readZigzagVarint(data, pos);
      pos += c2;
      prevLon += dlon;
      prevLat += dlat;
      coords.push([prevLon / 100_000, prevLat / 100_000]);
    }
  }

  return { coords, bytesConsumed: pos - offset };
}

function readI32LE(data: Uint8Array, offset: number): number {
  return data[offset] | (data[offset + 1] << 8) | (data[offset + 2] << 16) | (data[offset + 3] << 24);
}

/**
 * Read a u8-length-prefixed string (max 255 bytes).
 */
export function readU8String(data: Uint8Array, offset: number): [string, number] {
  const len = data[offset];
  const str = new TextDecoder().decode(data.slice(offset + 1, offset + 1 + len));
  return [str, 1 + len];
}

/**
 * Read a u16-length-prefixed string (max 65535 bytes).
 */
export function readU16String(data: Uint8Array, offset: number): [string, number] {
  const len = data[offset] | (data[offset + 1] << 8);
  const str = new TextDecoder().decode(data.slice(offset + 2, offset + 2 + len));
  return [str, 2 + len];
}

/**
 * Read an indexed_or_custom string:
 *   - if byte == 255, a u8-string follows
 *   - otherwise, it's an index into a table
 * Returns [value, bytes_consumed, is_index]
 */
export function readIndexedOrCustom(
  data: Uint8Array,
  offset: number,
  table: string[]
): [string, number, boolean] {
  const idx = data[offset];
  if (idx === 255) {
    const [s, bytes] = readU8String(data, offset + 1);
    return [s, 1 + bytes, false];
  }
  if (idx < table.length) {
    return [table[idx], 1, true];
  }
  return [`unknown_${idx}`, 1, true];
}

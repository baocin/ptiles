// tests/codec.test.ts — unit tests for codec functions

import { describe, test, expect } from 'vitest';
import {
  readVarint,
  zigzagDecode,
  encodeZigzagVarint,
  readZigzagVarint,
  decodeCoordinates,
  readU16String,
  readU8String,
} from '../src/codec.js';

/**
 * Helper: encode an unsigned integer as LEB128 varint bytes.
 * Used to construct test inputs for readVarint.
 */
function encodeVarint(value: number): number[] {
  const bytes: number[] = [];
  let v = value >>> 0;
  while (v >= 0x80) {
    bytes.push((v & 0x7f) | 0x80);
    v >>>= 7;
  }
  bytes.push(v & 0x7f);
  return bytes;
}

describe('Varint codec', () => {
  test('reads single-byte varint values', () => {
    // 0, 1, 127 are all single-byte (high bit clear)
    for (const val of [0, 1, 127]) {
      const bytes = new Uint8Array(encodeVarint(val));
      const [decoded, consumed] = readVarint(bytes, 0);
      expect(decoded).toBe(val);
      expect(consumed).toBe(1);
    }
  });

  test('reads multi-byte varint: 128', () => {
    const bytes = new Uint8Array(encodeVarint(128));
    const [decoded, consumed] = readVarint(bytes, 0);
    expect(decoded).toBe(128);
    expect(consumed).toBe(2);
  });

  test('reads multi-byte varint: 65535', () => {
    const bytes = new Uint8Array(encodeVarint(65535));
    const [decoded, consumed] = readVarint(bytes, 0);
    expect(decoded).toBe(65535);
    expect(consumed).toBe(3);
  });

  test('reads multi-byte varint up to 2^31-1', () => {
    // 2^31-1 = 2147483647 — largest value that round-trips without signed overflow
    const val = 2147483647;
    const bytes = new Uint8Array(encodeVarint(val));
    const [decoded, consumed] = readVarint(bytes, 0);
    expect(decoded).toBe(val);
    expect(consumed).toBe(5);
  });

  test('varint roundtrip on common values', () => {
    const testValues = [0, 1, 127, 128, 16383, 65535, 2097151, 268435455];
    for (const val of testValues) {
      const bytes = new Uint8Array(encodeVarint(val));
      const [decoded] = readVarint(bytes, 0);
      expect(decoded).toBe(val);
    }
  });

  test('throws on truncated varint', () => {
    // A varint with continuation bit set on last byte — will try to read past buffer
    const bytes = new Uint8Array([0x80]);
    expect(() => readVarint(bytes, 0)).toThrow();
  });

  test('reads varint at non-zero offset', () => {
    // Prefix with a byte, then encode 42
    const raw = [0xAA, ...encodeVarint(42), 0xBB];
    const bytes = new Uint8Array(raw);
    const [decoded, consumed] = readVarint(bytes, 1);
    expect(decoded).toBe(42);
    expect(consumed).toBe(1);
  });
});

describe('Zigzag codec', () => {
  test('encodeZigzagVarint roundtrip: 0', () => {
    const bytes = encodeZigzagVarint(0);
    const buf = new Uint8Array(bytes);
    const [decoded] = readZigzagVarint(buf, 0);
    expect(decoded).toBe(0);
  });

  test('encodeZigzagVarint roundtrip: -1', () => {
    const bytes = encodeZigzagVarint(-1);
    const buf = new Uint8Array(bytes);
    const [decoded] = readZigzagVarint(buf, 0);
    expect(decoded).toBe(-1);
  });

  test('encodeZigzagVarint roundtrip: 1', () => {
    const bytes = encodeZigzagVarint(1);
    const buf = new Uint8Array(bytes);
    const [decoded] = readZigzagVarint(buf, 0);
    expect(decoded).toBe(1);
  });

  test('encodeZigzagVarint roundtrip: -1000000', () => {
    const bytes = encodeZigzagVarint(-1000000);
    const buf = new Uint8Array(bytes);
    const [decoded] = readZigzagVarint(buf, 0);
    expect(decoded).toBe(-1000000);
  });

  test('encodeZigzagVarint roundtrip: 1000000', () => {
    const bytes = encodeZigzagVarint(1000000);
    const buf = new Uint8Array(bytes);
    const [decoded] = readZigzagVarint(buf, 0);
    expect(decoded).toBe(1000000);
  });

  test('zigzagDecode works for known values', () => {
    // zigzag(0) = 0
    expect(zigzagDecode(0)).toBe(0);
    // zigzag(-1) = 1, so zigzagDecode(1) = -1
    expect(zigzagDecode(1)).toBe(-1);
    // zigzag(1) = 2, so zigzagDecode(2) = 1
    expect(zigzagDecode(2)).toBe(1);
  });
});

describe('Coordinate decode', () => {
  test('decodes a single-vertex coordinate (lon=-86.0, lat=36.0)', () => {
    // Build bytes: i32 lon = -8600000, i32 lat = 3600000 (first vertex, absolute)
    const lon = -86.0 * 100_000; // -8600000
    const lat = 36.0 * 100_000;  // 3600000
    const buf = new Uint8Array(8);
    const dv = new DataView(buf.buffer);
    dv.setInt32(0, lon, true);
    dv.setInt32(4, lat, true);

    const result = decodeCoordinates(buf, 0, 1);
    expect(result.coords).toHaveLength(1);
    expect(result.coords[0][0]).toBeCloseTo(-86.0, 5);
    expect(result.coords[0][1]).toBeCloseTo(36.0, 5);
  });

  test('decodes two vertices with zigzag deltas', () => {
    // First vertex: (-86.0, 36.0) as i32 LE
    // Delta to second: (+0.01, -0.01) -> dlon_micro = 1000, dlat_micro = -1000
    // Encode deltas as zigzag varint

    const lonArr = new Uint8Array(4);
    const latArr = new Uint8Array(4);
    new DataView(lonArr.buffer).setInt32(0, -8600000, true);
    new DataView(latArr.buffer).setInt32(0, 3600000, true);
    const bytes = Array.from(lonArr).concat(Array.from(latArr));

    // Second vertex: zigzag varint deltas
    // dlon = 1000 -> zigzag(1000) = 2000, varint(2000) = [0x90, 0x0F]... let's calculate:
    //   2000 & 0x7f = 0x50 = 80, 80 | 0x80 = 0xD0... actually:
    //   2000 % 128 = 80, byte 0 = 80 | 128 = 208 = 0xD0
    //   2000 >>> 7 = 15, byte 1 = 15 = 0x0F
    //   So [0xD0, 0x0F]
    // dlat = -1000 -> zigzag(-1000) = 1999
    //   1999 % 128 = 79, byte 0 = 79 | 128 = 207 = 0xCF
    //   1999 >>> 7 = 15, byte 1 = 15 = 0x0F
    //   So [0xCF, 0x0F]

    bytes.push(0xD0, 0x0F, 0xCF, 0x0F);

    const buf = new Uint8Array(bytes);
    const result = decodeCoordinates(buf, 0, 2);

    expect(result.coords).toHaveLength(2);
    expect(result.coords[0][0]).toBeCloseTo(-86.0, 5);
    expect(result.coords[0][1]).toBeCloseTo(36.0, 5);
    expect(result.coords[1][0]).toBeCloseTo(-85.99, 4);
    expect(result.coords[1][1]).toBeCloseTo(35.99, 4);
  });

  test('returns empty array for vertexCount=0', () => {
    const buf = new Uint8Array(0);
    const result = decodeCoordinates(buf, 0, 0);
    expect(result.coords).toHaveLength(0);
    expect(result.bytesConsumed).toBe(0);
  });
});

describe('String decode', () => {
  test('readU16String decodes a u16-length-prefixed string', () => {
    // Encode "hello" as u16-string: length=5 as u16 LE, then bytes
    const strBytes = new TextEncoder().encode('hello');
    const buf = new Uint8Array(2 + strBytes.length);
    buf[0] = strBytes.length & 0xFF;        // low byte
    buf[1] = (strBytes.length >> 8) & 0xFF; // high byte
    buf.set(strBytes, 2);

    const [decoded, consumed] = readU16String(buf, 0);
    expect(decoded).toBe('hello');
    expect(consumed).toBe(7); // 2 + 5
  });

  test('readU16String handles empty string', () => {
    const buf = new Uint8Array(2); // length = 0
    buf[0] = 0;
    buf[1] = 0;

    const [decoded, consumed] = readU16String(buf, 0);
    expect(decoded).toBe('');
    expect(consumed).toBe(2);
  });

  test('readU16String handles longer strings', () => {
    const text = 'Nashville, Tennessee';
    const strBytes = new TextEncoder().encode(text);
    const buf = new Uint8Array(2 + strBytes.length);
    buf[0] = strBytes.length & 0xFF;
    buf[1] = (strBytes.length >> 8) & 0xFF;
    buf.set(strBytes, 2);

    const [decoded, consumed] = readU16String(buf, 0);
    expect(decoded).toBe(text);
    expect(consumed).toBe(2 + strBytes.length);
  });

  test('readU16String reads at offset', () => {
    // Prefix byte then a u16-string
    const text = 'test';
    const strBytes = new TextEncoder().encode(text);
    const buf = new Uint8Array(1 + 2 + strBytes.length);
    buf[0] = 0xFF; // prefix
    buf[1] = strBytes.length & 0xFF;
    buf[2] = (strBytes.length >> 8) & 0xFF;
    buf.set(strBytes, 3);

    const [decoded, consumed] = readU16String(buf, 1);
    expect(decoded).toBe('test');
    expect(consumed).toBe(2 + strBytes.length);
  });

  test('readU8String works for short strings', () => {
    const text = 'abc';
    const strBytes = new TextEncoder().encode(text);
    const buf = new Uint8Array(1 + strBytes.length);
    buf[0] = strBytes.length; // u8 length
    buf.set(strBytes, 1);

    const [decoded, consumed] = readU8String(buf, 0);
    expect(decoded).toBe('abc');
    expect(consumed).toBe(4); // 1 + 3
  });
});

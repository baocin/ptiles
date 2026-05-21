// src/binary-reader.ts — base class for reading binary files

/**
 * BinaryReader provides low-level binary reading primitives
 * over a Uint8Array view of a file's contents.
 */
export class BinaryReader {
  readonly data: Uint8Array;
  offset: number;

  constructor(data: Uint8Array) {
    this.data = data;
    this.offset = 0;
  }

  /** Read a single byte (u8). */
  readU8(): number {
    return this.data[this.offset++];
  }

  /** Read a little-endian u16. */
  readU16(): number {
    const val = this.data[this.offset] | (this.data[this.offset + 1] << 8);
    this.offset += 2;
    return val;
  }

  /** Read a little-endian u32. */
  readU32(): number {
    const val = this.data[this.offset] |
      (this.data[this.offset + 1] << 8) |
      (this.data[this.offset + 2] << 16) |
      (this.data[this.offset + 3] << 24);
    this.offset += 4;
    return val >>> 0;
  }

  /** Read a little-endian u64 (as number; safe up to 2^53). */
  readU64(): number {
    const low = this.readU32();
    const high = this.readU32();
    return high * 0x100000000 + low;
  }

  /** Read a little-endian i32 (signed). */
  readI32(): number {
    const val = this.readU32();
    return val | 0;
  }

  /** Read a little-endian f32. */
  readF32(): number {
    const view = new DataView(this.data.buffer, this.data.byteOffset + this.offset, 4);
    const val = view.getFloat32(0, true);
    this.offset += 4;
    return val;
  }

  /** Read a u48 (6 bytes, little-endian) as number. */
  readU48(): number {
    const low = this.readU16();
    const mid = this.readU16();
    const high = this.readU16();
    return low + (mid << 16) + (high << 32);
  }

  /** Read a u24 (3 bytes, little-endian) as number. */
  readU24(): number {
    const val = this.data[this.offset] |
      (this.data[this.offset + 1] << 8) |
      (this.data[this.offset + 2] << 16);
    this.offset += 3;
    return val;
  }

  /** Read a string of given byte length. */
  readString(length: number): string {
    const str = new TextDecoder().decode(this.data.slice(this.offset, this.offset + length));
    this.offset += length;
    return str;
  }

  /** Read a u8-length-prefixed string (max 255 bytes). */
  readU8String(): string {
    const len = this.readU8();
    return this.readString(len);
  }

  /** Read a u16-length-prefixed string (max 65535 bytes). */
  readU16String(): string {
    const len = this.readU16();
    return this.readString(len);
  }

  /** Read bytes into a new Uint8Array. */
  readBytes(length: number): Uint8Array {
    const slice = this.data.slice(this.offset, this.offset + length);
    this.offset += length;
    return slice;
  }

  /** Seek to an absolute position. */
  seek(pos: number): void {
    this.offset = pos;
  }

  /** Get current position. */
  tell(): number {
    return this.offset;
  }

  /** Total length of the buffer. */
  size(): number {
    return this.data.length;
  }

  /** Check if there's still data to read. */
  hasMore(): boolean {
    return this.offset < this.data.length;
  }

  /** Read remaining bytes. */
  readRemaining(): Uint8Array {
    return this.readBytes(this.data.length - this.offset);
  }
}

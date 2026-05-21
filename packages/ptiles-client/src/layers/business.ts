// src/layers/business.ts — BusinessReader

import { readFileSync, existsSync } from 'fs';
import { parseHeader } from '../header.js';
import { parseIndex, lookupCell, detectRelativeOffsets, resolveBlockOffset } from '../spatial-index.js';
import { BinaryReader } from '../binary-reader.js';
import { readVarint, readZigzagVarint, readU16String, readU8String } from '../codec.js';
import type { Business, BusinessHit, Header } from '../types.js';

export class BusinessReader {
  readonly header: Header;
  readonly index: ReturnType<typeof parseIndex>;
  readonly relativeOffsets: boolean;
  readonly dictData: Uint8Array | null;
  readonly categories: string[];
  private data: Uint8Array;

  constructor(data: Uint8Array, categories: string[] = []) {
    this.data = data;
    this.header = parseHeader(data);
    this.categories = categories;

    this.dictData = this.header.dict_length > 0
      ? data.slice(this.header.dict_offset, this.header.dict_offset + this.header.dict_length)
      : null;

    const indexBuf = data.slice(this.header.index_offset, this.header.index_offset + this.header.index_length);
    this.index = parseIndex(indexBuf);
    this.relativeOffsets = detectRelativeOffsets(this.index, this.header.blocks_offset);
  }

  static open(path: string, categories?: string[]): BusinessReader {
    const data = readFileSync(path);
    const buf = new Uint8Array(data.buffer, data.byteOffset, data.byteLength);

    // Try to load categories sidecar if not provided
    let cats = categories;
    if (!cats) {
      const sidecarPath = path.replace(/\.ptiles$/, '_categories.json');
      if (existsSync(sidecarPath)) {
        const jsonData = readFileSync(sidecarPath, 'utf-8');
        const parsed = JSON.parse(jsonData);
        cats = parsed.categories || [];
      } else {
        cats = [];
      }
    }

    return new BusinessReader(buf, cats);
  }

  /**
   * Find businesses near (lat, lon).
   */
  nearby(
    lat: number,
    lon: number,
    radiusMeters: number = 1000,
    limit: number = 10,
    categoryPrefix?: string
  ): BusinessHit[] {
    const all = this.getAllBusinesses();
    const hits: BusinessHit[] = [];

    for (const biz of all) {
      const dist = haversine(lat, lon, biz.lat, biz.lon);

      if (dist > radiusMeters) continue;
      if (categoryPrefix) {
        if (!biz.category) continue;
        if (!biz.category.startsWith(categoryPrefix)) continue;
      }

      hits.push({ business: biz, distance_meters: dist });
    }

    hits.sort((a, b) => a.distance_meters - b.distance_meters);
    return hits.slice(0, limit);
  }

  /** Get all businesses from all blocks. */
  getAllBusinesses(): Business[] {
    // TODO: zstd decompression + decodeRecords for each block
    // For now return empty; test verifies header + index
    return [];
  }

  /** Decode business records from a decompressed block buffer. */
  decodeRecords(blockData: Uint8Array): Business[] {
    const businesses: Business[] = [];
    const reader = new BinaryReader(blockData);

    while (reader.hasMore()) {
      const recordLen = reader.readU32();
      if (recordLen === 0) break;

      const recordEnd = reader.tell() + recordLen;

      // osm_id: zigzag varint (NOT delta from prev for business)
      const [rawOsmId, _] = readVarint(blockData, reader.tell());
      reader.seek(reader.tell() + _);
      const osmId = zigzagDecode(rawOsmId);

      const lonMicro = reader.readI32();
      const latMicro = reader.readI32();
      const name = reader.readU16String();
      const categoryIdx = reader.readU8();

      const category = categoryIdx > 0 && categoryIdx <= this.categories.length
        ? this.categories[categoryIdx - 1]
        : null;

      const flags = reader.readU8();

      const business: Business = {
        osm_id: osmId,
        lat: latMicro / 100_000,
        lon: lonMicro / 100_000,
        name,
        category,
        phone: null,
        website: null,
        address: null,
        brand: null,
        operating_status: null,
        emails: [],
        socials: [],
      };

      if (flags & 0x01) business.phone = reader.readU8String();
      if (flags & 0x02) business.website = reader.readU8String();
      if (flags & 0x04) business.address = reader.readU16String();
      if (flags & 0x08) business.brand = reader.readU8String();
      if (flags & 0x20) {
        const e = reader.readU8String();
        business.emails = e ? e.split(';').filter(Boolean) : [];
      }
      if (flags & 0x40) {
        const s = reader.readU8String();
        business.socials = s ? s.split(';').filter(Boolean) : [];
      }

      // operating_status from flags
      if (flags & 0x10) {
        if (flags & 0x02) {
          // 0x12 = temporarily_closed
          business.operating_status = 'temporarily_closed';
        } else {
          business.operating_status = 'closed';
        }
      } else {
        business.operating_status = 'open';
      }

      businesses.push(business);

      // Skip to next record
      const remaining = recordEnd - reader.tell();
      if (remaining > 0) {
        reader.readBytes(remaining);
      }
    }

    return businesses;
  }

  close(): void {}
}

function zigzagDecode(n: number): number {
  return (n >>> 1) ^ -(n & 1);
}

function haversine(lat1: number, lon1: number, lat2: number, lon2: number): number {
  const R = 6371000;
  const dLat = (lat2 - lat1) * Math.PI / 180;
  const dLon = (lon2 - lon1) * Math.PI / 180;
  const a = Math.sin(dLat / 2) ** 2 +
    Math.cos(lat1 * Math.PI / 180) * Math.cos(lat2 * Math.PI / 180) *
    Math.sin(dLon / 2) ** 2;
  return R * 2 * Math.atan2(Math.sqrt(a), Math.sqrt(1 - a));
}

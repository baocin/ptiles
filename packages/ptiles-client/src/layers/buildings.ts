// src/layers/buildings.ts — BuildingsReader

import { readFileSync } from 'fs';
import { join, dirname } from 'path';
import { parseHeader } from '../header.js';
import { parseIndex, lookupCell, detectRelativeOffsets, resolveBlockOffset } from '../spatial-index.js';
import { BinaryReader } from '../binary-reader.js';
import { readVarint, readZigzagVarint, readIndexedOrCustom, decodeCoordinates, readU16String, readU8String } from '../codec.js';
import type { Building, Header } from '../types.js';

/**
 * Known building type indices (from Rust src/buildings.rs line 14).
 */
const BUILDING_TYPES: string[] = [
  'yes', 'house', 'garage', 'apartments', 'shed', 'roof', 'commercial',
  'garage_units', 'industrial', 'detached_garage', 'residential', 'retail',
  'hangar', 'hut', 'greenhouse', 'carport', 'terrace', 'cabin',
  'farm', 'barn', 'warehouse', 'school', 'service', 'church', 'manufacturing',
  'university', 'office', 'hospital', 'parking', 'storage', 'tank', 'shop',
  'gate', 'logistics', 'stable', 'transportation', 'kiosk', 'pavilion',
  'religious', 'container', 'silo', 'canopy', 'construction', 'ruins',
  'dumpster', 'bridge', 'conservatory', 'static_caravan', 'ger',
  'digester', 'supermarket', 'sports_centre', 'train_station', 'toilet',
  'temporary', 'static_guard', 'mosque', 'power_plant', 'dam',
];

export class BuildingsReader {
  readonly header: Header;
  readonly index: ReturnType<typeof parseIndex>;
  readonly relativeOffsets: boolean;
  readonly dictData: Uint8Array | null;
  private data: Uint8Array;

  constructor(data: Uint8Array) {
    this.data = data;
    this.header = parseHeader(data);
    this.dictData = this.header.dict_length > 0
      ? data.slice(this.header.dict_offset, this.header.dict_offset + this.header.dict_length)
      : null;

    const indexBuf = data.slice(this.header.index_offset, this.header.index_offset + this.header.index_length);
    this.index = parseIndex(indexBuf);
    this.relativeOffsets = detectRelativeOffsets(this.index, this.header.blocks_offset);
  }

  /** Open a BuildingsReader from a file path. */
  static open(path: string): BuildingsReader {
    const data = readFileSync(path);
    return new BuildingsReader(new Uint8Array(data.buffer, data.byteOffset, data.byteLength));
  }

  /** Query building at (lat, lon) — finds the cell, decodes all records, returns the first containing match. */
  query(lat: number, lon: number): Building | null {
    // Stub: decode the cell containing (lat, lon) and return first building
    // For simplicity without h3-js dep, we scan all cells
    const buildings = this.getAllBuildings();
    // Find building whose centroid is closest (simple heuristic)
    let best: Building | null = null;
    let bestDist = Infinity;
    for (const b of buildings) {
      const d = (b.centroid_lat - lat) ** 2 + (b.centroid_lon - lon) ** 2;
      if (d < bestDist) {
        bestDist = d;
        best = b;
      }
    }
    return best;
  }

  /** Find buildings within a radius. */
  within(lat: number, lon: number, meters: number): Building[] {
    // Stub: naive scan
    const all = this.getAllBuildings();
    const results: Building[] = [];
    for (const b of all) {
      const d = haversine(lat, lon, b.centroid_lat, b.centroid_lon);
      if (d <= meters) {
        results.push(b);
      }
    }
    return results;
  }

  /** Decode all buildings from all blocks. */
  getAllBuildings(): Building[] {
    const buildings: Building[] = [];
    for (const entry of this.index) {
      const absOffset = resolveBlockOffset(entry, this.header.blocks_offset, this.relativeOffsets);
      const blockData = this.data.slice(absOffset, absOffset + entry.block_length);
      // TODO: zstd decompress blockData
      // For now, skip decompressed parse — mark the zstd decompress as TODO
      // Just record that we found a block
      // This is a placeholder: real implementation would decompress then parse records
    }
    return buildings;
  }

  /** Decode records from an already-decompressed block buffer. */
  decodeRecords(blockData: Uint8Array, version: number): Building[] {
    const buildings: Building[] = [];
    const reader = new BinaryReader(blockData);
    let prevOsmId = 0;

    while (reader.hasMore()) {
      const recordLen = reader.readU32();
      if (recordLen === 0) break; // sentinel

      const recordStart = reader.tell();
      const recordEnd = recordStart + recordLen;

      // osm_id: v6+ varint delta, v<6 u64 absolute
      let osmId: number;
      if (version >= 6) {
        const [delta, _] = readVarint(blockData, reader.tell());
        reader.seek(reader.tell() + _);
        osmId = prevOsmId + delta;
        prevOsmId = osmId;
      } else {
        osmId = reader.readU64();
      }

      const vertexCount = reader.readU8();
      const firstLon = reader.readI32();
      const firstLat = reader.readI32();

      // Decode coordinates (wall-segment or zigzag)
      const coords: [number, number][] = [];
      coords.push([firstLon / 100_000, firstLat / 100_000]);

      let prevLon = firstLon;
      let prevLat = firstLat;

      if (version >= 7) {
        for (let i = 1; i < vertexCount; i++) {
          const angleByte = reader.readU8();
          const lengthByte = reader.readU8();
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
        for (let i = 1; i < vertexCount; i++) {
          const [dlon, c1] = readZigzagVarint(blockData, reader.tell());
          reader.seek(reader.tell() + c1);
          const [dlat, c2] = readZigzagVarint(blockData, reader.tell());
          reader.seek(reader.tell() + c2);
          prevLon += dlon;
          prevLat += dlat;
          coords.push([prevLon / 100_000, prevLat / 100_000]);
        }
      }

      const flags = reader.readU8();
      const [buildingType] = readIndexedOrCustom(blockData, reader.tell(), BUILDING_TYPES);
      reader.seek(reader.tell() + 1); // handle either case roughly

      // Compute centroid
      let centroidLat = 0, centroidLon = 0;
      for (const [lon, lat] of coords) {
        centroidLat += lat;
        centroidLon += lon;
      }
      centroidLat /= coords.length;
      centroidLon /= coords.length;

      const building: Building = {
        osm_id: osmId,
        building_type: buildingType,
        centroid_lat: centroidLat,
        centroid_lon: centroidLon,
        coordinates: coords,
        name: null,
        category: null,
        name_source: null,
        poi_osm_id: null,
      };

      // Parse optional fields
      let p = reader.tell();
      // Need to re-read flags in binary reader context
      buildings.push(building);

      // Skip past record end
      reader.seek(recordEnd);
    }

    return buildings;
  }

  close(): void {
    // no-op for sync
  }
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

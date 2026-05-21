// src/layers/roads.ts — RoadsReader

import { readFileSync } from 'fs';
import { parseHeader } from '../header.js';
import { parseIndex, lookupCell, detectRelativeOffsets, resolveBlockOffset } from '../spatial-index.js';
import { BinaryReader } from '../binary-reader.js';
import { readVarint, readZigzagVarint, readIndexedOrCustom, readU16String, readU8String } from '../codec.js';
import type { RoadSegment, NearestRoad, Header, Intersection, RoadProfile } from '../types.js';

/**
 * Known road class indices (from Rust src/roads.rs line 13).
 */
const ROAD_CLASSES: string[] = [
  'motorway', 'trunk', 'primary', 'secondary', 'tertiary',
  'unclassified', 'residential', 'motorway_link', 'trunk_link',
  'primary_link', 'secondary_link', 'tertiary_link', 'living_street',
  'service', 'pedestrian', 'track', 'bus_guideway', 'path',
  'cycleway', 'footway', 'bridleway', 'steps', 'corridor',
  'road', 'construction', 'proposed', 'raceway', 'rest_area',
  'services', 'escape', 'busway', 'bus_stop', 'sidewalk',
  'crossing', 'traffic_island', 'driveway', 'parking_aisle',
  'alley', 'entrance', 'emergency_bay', 'runway', 'taxiway',
  'platform', 'yes', 'farm', 'forest', 'plantation',
  'orchard', 'vineyard', 'drive_through',
];

const SURFACE_TYPES: string[] = [
  'paved', 'unpaved', 'asphalt', 'concrete', 'concrete:lanes',
  'concrete:plates', 'paving_stones', 'sett', 'cobblestone',
  'metal', 'wood', 'compacted', 'fine_gravel', 'gravel',
  'pebblestone', 'dirt', 'earth', 'grass', 'grass_paver',
  'mud', 'sand', 'ground',
];

const INTERSECTION_NAMES: string[] = [
  'traffic_signals', 'stop', 'give_way', 'roundabout',
];

export class RoadsReader {
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

  static open(path: string): RoadsReader {
    const data = readFileSync(path);
    return new RoadsReader(new Uint8Array(data.buffer, data.byteOffset, data.byteLength));
  }

  /**
   * Find the nearest road segment to a point.
   * Searches the cell containing (lat, lon) plus `rings` rings of neighbors.
   */
  nearest(
    lat: number,
    lon: number,
    radiusMeters: number = 100,
    profile?: RoadProfile,
    rings: number = 1
  ): NearestRoad | null {
    // For now, do a naive scan over all decoded roads
    const allRoads = this.getAllRoads();
    let best: NearestRoad | null = null;
    let bestDist = radiusMeters;

    for (const road of allRoads) {
      const result = nearestPointOnSegment(lat, lon, road.coords);
      if (result !== null && result.distance < bestDist) {
        // Check profile filter
        if (profile && !matchesProfile(road.road_class, profile)) {
          continue;
        }
        best = {
          road,
          distance_meters: result.distance,
          snapped_lat: result.lat,
          snapped_lon: result.lon,
          segment_index: result.segmentIndex,
          along_fraction: result.fraction,
        };
        bestDist = result.distance;
      }
    }

    return best;
  }

  /** Get all road segments from all blocks (without zstd decompress — placeholder). */
  getAllRoads(): RoadSegment[] {
    // TODO: zstd decompression for each block, then decodeRecords
    // For now return empty — test should verify header + index parsing works
    return [];
  }

  /** Decode records from an already-decompressed block. */
  decodeRecords(blockData: Uint8Array, version: number): { segments: RoadSegment[]; intersections: Intersection[] } {
    const segments: RoadSegment[] = [];
    const reader = new BinaryReader(blockData);
    let prevOsmId = 0;

    while (reader.hasMore()) {
      const recordLen = reader.readU32();
      if (recordLen === 0) break; // sentinel

      const recordStart = reader.tell();
      const [osmDelta, _] = readVarint(blockData, reader.tell());
      reader.seek(reader.tell() + _);
      prevOsmId += osmDelta;
      const osmId = prevOsmId;

      const vertexCount = reader.readU16();
      const firstLon = reader.readI32();
      const firstLat = reader.readI32();

      const coords: [number, number][] = [[firstLon / 100_000, firstLat / 100_000]];
      let prevLon = firstLon;
      let prevLat = firstLat;

      for (let i = 1; i < vertexCount; i++) {
        const [dlon, c1] = readZigzagVarint(blockData, reader.tell());
        reader.seek(reader.tell() + c1);
        const [dlat, c2] = readZigzagVarint(blockData, reader.tell());
        reader.seek(reader.tell() + c2);
        prevLon += dlon;
        prevLat += dlat;
        coords.push([prevLon / 100_000, prevLat / 100_000]);
      }

      const flags = reader.readU8();
      const roadClass = readIndexedOrCustom(blockData, reader.tell(), ROAD_CLASSES);
      reader.seek(reader.tell() + 1);

      const segment: RoadSegment = {
        osm_id: osmId,
        road_class: roadClass[0],
        coords,
        name: null,
        ref_tag: null,
        oneway: null,
        speed_limit_kmh: null,
        lanes: null,
        surface: null,
        bridge_tunnel: null,
      };

      if (flags & 0x01) segment.name = reader.readU16String();
      if (flags & 0x02) segment.ref_tag = reader.readU8String();
      if (flags & 0x04) {
        const ow = reader.readU8();
        segment.oneway = ow === 1 ? 'forward' : ow === 2 ? 'reverse' : 'no';
      }
      if (flags & 0x08) segment.speed_limit_kmh = reader.readU8();
      if (flags & 0x10) segment.lanes = reader.readU8();
      if (flags & 0x20) {
        const surf = readIndexedOrCustom(blockData, reader.tell(), SURFACE_TYPES);
        reader.seek(reader.tell() + 1);
        segment.surface = surf[0];
      }
      if (flags & 0x40) {
        const bt = reader.readU8();
        segment.bridge_tunnel = bt === 1 ? 'bridge' : bt === 2 ? 'tunnel' : null;
      }

      segments.push(segment);
    }

    // Parse intersections (version >= 2)
    const intersections: Intersection[] = [];
    if (version >= 2 && reader.hasMore()) {
      const intCount = reader.readU16();
      for (let i = 0; i < intCount; i++) {
        const lonMicro = reader.readI32();
        const latMicro = reader.readI32();
        const intType = reader.readU8();
        intersections.push({
          lon_micro: lonMicro,
          lat_micro: latMicro,
          intersection_type: intType >= 1 && intType <= 4
            ? INTERSECTION_NAMES[intType - 1] as Intersection['intersection_type']
            : 'traffic_signals',
        });
      }
    }

    return { segments, intersections };
  }

  close(): void {}
}

/**
 * Calculate nearest point on a polyline to (lat, lon).
 */
function nearestPointOnSegment(
  lat: number, lon: number,
  coords: [number, number][]
): { distance: number; lat: number; lon: number; segmentIndex: number; fraction: number } | null {
  let bestDist = Infinity;
  let bestLat = 0;
  let bestLon = 0;
  let bestIdx = 0;
  let bestFrac = 0;

  for (let i = 0; i < coords.length - 1; i++) {
    const [lon1, lat1] = coords[i];
    const [lon2, lat2] = coords[i + 1];
    const result = pointToSegmentDistance(lat, lon, lat1, lon1, lat2, lon2);
    if (result.distance < bestDist) {
      bestDist = result.distance;
      bestLat = result.lat;
      bestLon = result.lon;
      bestIdx = i;
      bestFrac = result.fraction;
    }
  }

  return { distance: bestDist, lat: bestLat, lon: bestLon, segmentIndex: bestIdx, fraction: bestFrac };
}

/**
 * Point-to-line-segment distance in meters (planar approximation).
 */
function pointToSegmentDistance(
  px: number, py: number,
  ax: number, ay: number,
  bx: number, by: number
): { distance: number; lat: number; lon: number; fraction: number } {
  const dx = bx - ax;
  const dy = by - ay;
  const lenSq = dx * dx + dy * dy;

  let t = 0;
  if (lenSq > 0) {
    t = ((px - ax) * dx + (py - ay) * dy) / lenSq;
    t = Math.max(0, Math.min(1, t));
  }

  const projX = ax + t * dx;
  const projY = ay + t * dy;
  const distX = px - projX;
  const distY = py - projY;

  // Approximate meters per degree
  const latScale = Math.cos((projY * Math.PI / 180));
  const distMeters = Math.sqrt(
    (distX * 111320 * latScale) ** 2 + (distY * 111320) ** 2
  );

  return { distance: distMeters, lat: projY, lon: projX, fraction: t };
}

function matchesProfile(roadClass: string, profile: RoadProfile): boolean {
  if (profile === 'driving') {
    const drivingClasses = ['motorway', 'trunk', 'primary', 'secondary', 'tertiary',
      'motorway_link', 'trunk_link', 'primary_link', 'secondary_link', 'tertiary_link',
      'unclassified', 'residential', 'living_street', 'service', 'road'];
    return drivingClasses.includes(roadClass);
  }
  if (profile === 'walking') {
    const walkingClasses = ['footway', 'pedestrian', 'path', 'steps', 'sidewalk',
      'crossing', 'residential', 'living_street', 'service', 'track'];
    return walkingClasses.includes(roadClass);
  }
  if (profile === 'cycling') {
    const cyclingClasses = ['cycleway', 'path', 'residential', 'living_street',
      'service', 'track', 'unclassified'];
    return cyclingClasses.includes(roadClass);
  }
  return true;
}

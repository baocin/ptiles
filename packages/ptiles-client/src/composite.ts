// src/composite.ts — PtilesClient

import { join } from 'path';
import { existsSync, readdirSync, readFileSync } from 'fs';
import { BuildingsReader } from './layers/buildings.js';
import { RoadsReader } from './layers/roads.js';
import { WaterReader } from './layers/water.js';
import { BusinessReader } from './layers/business.js';
import type { PointReport, Header } from './types.js';

export interface PointQueryOpts {
  includeBuildings?: boolean;
  includeAdmin?: boolean;
  includeNearestRoad?: boolean;
  nearbyBusinessLimit?: number;
  nearbyBusinessRadiusMeters?: number;
  waterRadiusMeters?: number;
}

/** A published manifest, as written by scripts/gen_manifest.py. */
export interface Manifest {
  built: string;
  source: string;
  countries: Record<string, string[]>;
  layers: Record<string, {
    /** Null when countries in this snapshot sit at different versions. */
    version: number | null;
    pattern: string | null;
    /** Version per country, e.g. { US: 9, FR: 10 }. */
    versions?: Record<string, number | null>;
    scopes: Record<string, {
      country: string; path: string; bounds: number[] | null; version?: number | null;
    }>;
  }>;
}

/** Filename suffixes per layer, newest first. Only used without a manifest. */
const SUFFIXES: Record<string, string[]> = {
  buildings: ['buildings_v9', 'buildings_v8', 'buildings'],
  roads: ['highways_v2', 'roads'],
  water: ['water_v1', 'water'],
  business: ['business_v4', 'business'],
  // v2 of these carries name:en and brand; a v1 file still reads, with the
  // alternative-name flag bits simply clear.
  places: ['places_v2', 'places_v1', 'places'],
  parks: ['parks_v2', 'parks_v1', 'parks'],
  rail: ['rail_v2', 'rail_v1', 'rail'],
  trails: ['trails_v2', 'trails_v1', 'trails'],
  ev: ['ev_v2', 'ev_v1', 'ev'],
};

/**
 * Country a scope belongs to. A bare two-letter scope is a US state -- the US
 * set was published first and owns the unprefixed namespace -- while a
 * hyphenated scope names its country first (`JP-KANTO` -> `JP`).
 */
const US_STATES = new Set(
  ('AL AK AZ AR CA CO CT DE DC FL GA HI ID IL IN IA KS KY LA ME MD MA MI MN MS ' +
   'MO MT NE NV NH NJ NM NY NC ND OH OK OR PA RI SC SD TN TX UT VT VA WA WV WI WY').split(' ')
);

export function countryOf(scope: string): string {
  const m = /^([A-Z]{2,3})(?:-([A-Z0-9]+))?$/.exec(scope);
  if (!m) throw new Error(`not a scope: ${scope}`);
  const [, head, sub] = m;
  if (sub) return head;              // already qualified: JP-KANTO, CA-ON
  if (head.length === 3) return head; // alpha-3 is never a US state abbreviation
  return US_STATES.has(head) || head === 'US' ? 'US' : head;
}

/** Whether a country's ISO alpha-2 is already a US state abbreviation. */
export function collidesWithUsState(alpha2: string): boolean {
  return US_STATES.has(alpha2.toUpperCase());
}

/** Where a file sits in a snapshot: US at the root, other countries in a dir. */
export function publishRelpath(scope: string, filename: string): string {
  const country = countryOf(scope);
  return country === 'US' ? filename : `${country}/${filename}`;
}

/**
 * Where a manifest says a file lives, relative to the snapshot root.
 * Manifests published before countries existed carry no per-scope `path`, so
 * fall back to the layer's filename pattern and apply the layout rule.
 */
export function manifestRelpath(
  scope: string,
  layer: string,
  entry: { version?: number | null; pattern?: string | null },
  meta?: { path?: string; version?: number | null }
): string | null {
  if (meta?.path) return meta.path;
  // The scope's own version wins over the layer's: once countries can sit at
  // different versions the layer-level one is null, and only the per-scope
  // value says what this file is actually called.
  const version = meta?.version ?? entry?.version;
  const filename = version
    ? `${scope}.${layer}_v${version}.ptiles`
    : entry?.pattern
      ? entry.pattern.replace('{scope}', scope)
      : `${scope}.${layer}.ptiles`;
  try {
    return publishRelpath(scope, filename);
  } catch {
    return null; // not a scope-shaped name; cannot be placed
  }
}

/** One opened file, tagged with the scope it covers. */
interface Held<T> {
  scope: string;
  reader: T & { header: Header };
}

/**
 * Whether `lon` falls in west..east going eastward.
 * A box crossing the antimeridian has west > east (Fiji is 177E..-179E), and
 * `west <= lon <= east` is then false for every longitude on Earth -- the file
 * gets skipped and the query returns nothing, which looks identical to "no
 * features here". Affects Fiji, NZ's Chathams, Russia, Kiribati, Alaska.
 */
export function lonWithin(west: number, east: number, lon: number, pad = 0): boolean {
  if (west <= east) return lon >= west - pad && lon <= east + pad;
  return lon >= west - pad || lon <= east + pad;
}

function covers(h: Header, lat: number, lon: number, pad = 0.05): boolean {
  // A file with no usable bbox (PTLR roads carry none) is always consulted.
  if (!h || typeof h.min_lat !== 'number') return true;
  if (h.min_lat === 0 && h.max_lat === 0) return true;
  return (
    lat >= h.min_lat - pad && lat <= h.max_lat + pad &&
    lonWithin(h.min_lon, h.max_lon, lon, pad)
  );
}

export class PtilesClient {
  // Several files can back one layer: Japan's buildings are eight regional
  // files while its other layers are country-wide, so a client holding "Japan"
  // holds eight building readers. Queries pick among them by bounds.
  buildingsAll: Held<BuildingsReader>[] = [];
  roadsAll: Held<RoadsReader>[] = [];
  waterAll: Held<WaterReader>[] = [];
  businessAll: Held<BusinessReader>[] = [];

  /** First-opened reader per layer. Kept so single-scope callers still work. */
  get buildings(): BuildingsReader | null { return this.buildingsAll[0]?.reader ?? null; }
  get roads(): RoadsReader | null { return this.roadsAll[0]?.reader ?? null; }
  get water(): WaterReader | null { return this.waterAll[0]?.reader ?? null; }
  get business(): BusinessReader | null { return this.businessAll[0]?.reader ?? null; }

  private add(layer: string, scope: string, path: string): void {
    switch (layer) {
      case 'buildings':
        this.buildingsAll.push({ scope, reader: BuildingsReader.open(path) });
        break;
      case 'roads':
        this.roadsAll.push({ scope, reader: RoadsReader.open(path) });
        break;
      case 'water':
        this.waterAll.push({ scope, reader: WaterReader.open(path) });
        break;
      case 'business':
        this.businessAll.push({ scope, reader: BusinessReader.open(path) });
        break;
    }
  }

  /**
   * Open all available layers for one scope.
   * Published snapshots keep the US at the root and give every other country
   * its own directory, so both are probed.
   */
  static openScope(scope: string, dataDir: string): PtilesClient {
    const client = new PtilesClient();
    client.loadScope(scope.toUpperCase(), dataDir);
    return client;
  }

  /** Original name for openScope, from before non-US countries existed. */
  static openState(state: string, dataDir: string): PtilesClient {
    return PtilesClient.openScope(state, dataDir);
  }

  /**
   * Open every scope belonging to one country.
   *
   * A manifest.json beside the data is authoritative and is used when present.
   * It is optional: without one the directory is scanned and filenames are
   * probed, which is what a partial or hand-assembled download looks like.
   */
  static openCountry(country: string, dataDir: string, scopes?: string[]): PtilesClient {
    const manifest = join(dataDir, 'manifest.json');
    if (!scopes && existsSync(manifest)) {
      return PtilesClient.fromManifest(manifest, dataDir, { country });
    }
    const client = new PtilesClient();
    const want = scopes ?? PtilesClient.discoverScopes(dataDir, country);
    for (const scope of want.slice().sort()) client.loadScope(scope, dataDir);
    return client;
  }

  /** Scopes of one country present in a local snapshot directory. */
  static discoverScopes(dataDir: string, country: string): string[] {
    const upper = country.toUpperCase();
    const found = new Set<string>();
    for (const dir of [dataDir, join(dataDir, upper)]) {
      let names: string[];
      try {
        names = readdirSync(dir);
      } catch {
        continue;
      }
      for (const name of names) {
        if (!name.endsWith('.ptiles')) continue;
        const scope = name.split('.')[0];
        try {
          if (countryOf(scope) === upper) found.add(scope);
        } catch {
          // not a scope-shaped name; ignore
        }
      }
    }
    return [...found].sort();
  }

  /**
   * Open the files a published manifest declares.
   * The manifest states the layer versions and each file's path, so nothing
   * here has to guess filenames from a hardcoded suffix list.
   */
  static fromManifest(
    manifest: Manifest | string,
    dataDir: string,
    opts: { country?: string; scopes?: string[] } = {}
  ): PtilesClient {
    let m: Manifest;
    if (typeof manifest === 'string') {
      // A manifest that is present but unreadable is a real fault: raise
      // rather than falling back to probing, which would hide a corrupt
      // publish behind a client that merely looks like it works.
      if (!existsSync(manifest)) throw new Error(`no manifest at ${manifest}`);
      const text = readFileSync(manifest, 'utf8');
      try {
        m = JSON.parse(text);
      } catch (e) {
        throw new Error(`manifest at ${manifest} is not valid JSON: ${e}`);
      }
    } else {
      m = manifest;
    }
    if (!m || typeof m !== 'object' || Array.isArray(m)) {
      throw new Error('manifest must be a JSON object');
    }

    const client = new PtilesClient();
    const country = opts.country?.toUpperCase();
    let want: Set<string> | null = null;
    if (opts.scopes) {
      want = new Set(opts.scopes);
    } else if (country) {
      const declared = m.countries?.[country];
      if (declared) {
        want = new Set(declared);
      } else {
        // Pre-countries manifest: work membership out from the scope names.
        want = new Set<string>();
        for (const entry of Object.values(m.layers ?? {})) {
          for (const scope of Object.keys(entry.scopes ?? {})) {
            try {
              if (countryOf(scope) === country) want.add(scope);
            } catch {
              // not a scope-shaped name; ignore
            }
          }
        }
      }
    }

    for (const [layer, entry] of Object.entries(m.layers ?? {})) {
      if (!(layer in SUFFIXES)) continue; // no reader for this layer yet
      for (const [scope, meta] of Object.entries(entry.scopes ?? {})) {
        if (want && !want.has(scope)) continue;
        const rel = manifestRelpath(scope, layer, entry, meta);
        if (!rel) continue;
        // Try the declared path, then the bare filename, so a manifest also
        // works against a flat directory of downloaded files.
        const path = [join(dataDir, rel), join(dataDir, rel.split('/').pop()!)]
          .find(p => existsSync(p));
        if (!path) continue; // partially downloaded snapshot
        try {
          client.add(layer, scope, path);
        } catch {
          // a file we cannot parse should not take the whole client down
        }
      }
    }
    return client;
  }

  private loadScope(scope: string, dataDir: string): void {
    const country = countryOf(scope);
    for (const [layer, suffixes] of Object.entries(SUFFIXES)) {
      for (const suffix of suffixes) {
        const name = `${scope}.${suffix}.ptiles`;
        const path = [join(dataDir, name), join(dataDir, country, name)]
          .find(p => existsSync(p));
        if (path) {
          try {
            this.add(layer, scope, path);
          } catch {
            // skip unreadable file, keep the rest of the client usable
          }
          break; // first matching suffix wins
        }
      }
    }
  }

  /** Readers whose file can contain this point. */
  private pick<T>(held: Held<T>[], lat: number, lon: number): Held<T>[] {
    return held.filter(h => covers(h.reader.header, lat, lon));
  }

  /**
   * Query a single point across all opened layers.
   */
  queryPoint(lat: number, lon: number, opts: PointQueryOpts = {}): PointReport {
    const {
      includeBuildings = true,
      includeNearestRoad = true,
      nearbyBusinessLimit = 5,
      nearbyBusinessRadiusMeters = 500,
    } = opts;

    const report: PointReport = {
      building: null,
      admin: null,
      nearest_road: null,
      nearby_roads: [],
      water: [],
      parks: [],
      places: [],
      businesses: [],
    };

    if (includeBuildings) {
      // Region files overlap at their seams, so a file can cover the point and
      // still hold nothing there. Only a hit may set the answer, or a miss from
      // a neighbouring region would erase it.
      for (const { reader } of this.pick(this.buildingsAll, lat, lon)) {
        const hit = reader.query(lat, lon);
        if (hit) { report.building = hit; break; }
      }
    }

    if (includeNearestRoad) {
      for (const { reader } of this.pick(this.roadsAll, lat, lon)) {
        const near = reader.nearest(lat, lon, 100);
        // Keep the closest across files, not the last file's answer.
        if (near && (!report.nearest_road ||
            near.distance_meters < report.nearest_road.distance_meters)) {
          report.nearest_road = near;
        }
      }
    }

    if (nearbyBusinessLimit > 0) {
      for (const { reader } of this.pick(this.businessAll, lat, lon)) {
        report.businesses.push(
          ...reader.nearby(lat, lon, nearbyBusinessRadiusMeters, nearbyBusinessLimit)
        );
      }
    }

    return report;
  }

  close(): void {
    for (const h of [...this.buildingsAll, ...this.roadsAll,
                     ...this.waterAll, ...this.businessAll]) {
      (h.reader as { close?: () => void }).close?.();
    }
  }
}

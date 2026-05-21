// src/composite.ts — PtilesClient

import { join, dirname } from 'path';
import { existsSync } from 'fs';
import { FORMAT_SUFFIX } from './header.js';
import { BuildingsReader } from './layers/buildings.js';
import { RoadsReader } from './layers/roads.js';
import { WaterReader } from './layers/water.js';
import { BusinessReader } from './layers/business.js';
import type { PointReport, Building, RoadSegment, WaterFeature, ParkFeature, Place, NearestRoad, BusinessHit, Header } from './types.js';

export interface PointQueryOpts {
  includeBuildings?: boolean;
  includeAdmin?: boolean;
  includeNearestRoad?: boolean;
  nearbyBusinessLimit?: number;
  nearbyBusinessRadiusMeters?: number;
  waterRadiusMeters?: number;
}

export class PtilesClient {
  buildings: BuildingsReader | null = null;
  roads: RoadsReader | null = null;
  water: WaterReader | null = null;
  business: BusinessReader | null = null;
  // Admin, Places, Rail, Parks — stubs for now

  /**
   * Open all available layers for a given state from a data directory.
   * Convention: <dataDir>/<STATE>.<suffix>.ptiles
   */
  static openState(state: string, dataDir: string): PtilesClient {
    const client = new PtilesClient();

    const suffixMap: Record<string, string[]> = {
      'buildings': ['buildings_v8'],
      'roads': ['roads'],
      'water': ['water'],
      'business': ['business'],
    };

    for (const [layer, suffixes] of Object.entries(suffixMap)) {
      for (const suffix of suffixes) {
        const path = join(dataDir, `${state}.${suffix}.ptiles`);
        if (existsSync(path)) {
          switch (layer) {
            case 'buildings':
              client.buildings = BuildingsReader.open(path);
              break;
            case 'roads':
              client.roads = RoadsReader.open(path);
              break;
            case 'water':
              client.water = WaterReader.open(path);
              break;
            case 'business':
              client.business = BusinessReader.open(path);
              break;
          }
          break; // use first matching suffix
        }
      }
    }

    return client;
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
      waterRadiusMeters = 100,
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

    if (includeBuildings && this.buildings) {
      report.building = this.buildings.query(lat, lon);
    }

    if (includeNearestRoad && this.roads) {
      report.nearest_road = this.roads.nearest(lat, lon, 100);
      // TODO: nearby_roads (nearestN)
    }

    if (this.business && nearbyBusinessLimit > 0) {
      report.businesses = this.business.nearby(
        lat, lon,
        nearbyBusinessRadiusMeters,
        nearbyBusinessLimit
      );
    }

    return report;
  }

  close(): void {
    this.buildings?.close();
    this.roads?.close();
    this.water?.close();
    this.business?.close();
  }
}

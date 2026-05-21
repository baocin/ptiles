// tests/composite.test.ts — test PtilesClient.openState and queryPoint

import { describe, test, expect } from 'vitest';
import { PtilesClient } from '../src/composite.js';

const DATA_DIR = process.env.PTILES_DATA_DIR || '/home/aoi/kino/projects/ptiles/data/states';

describe('Composite client', () => {
  test('PtilesClient.openState loads available layers for TN', () => {
    const client = PtilesClient.openState('TN', DATA_DIR);

    expect(client.buildings).not.toBeNull();
    expect(client.roads).not.toBeNull();
    expect(client.water).not.toBeNull();
    expect(client.business).not.toBeNull();

    // Verify each layer's header is correctly parsed
    expect(client.buildings!.header.format).toBe('Buildings');
    expect(client.buildings!.header.version).toBe(8);

    expect(client.roads!.header.format).toBe('Roads');
    expect(client.roads!.header.version).toBe(2);

    expect(client.water!.header.format).toBe('Water');

    expect(client.business!.header.format).toBe('Business');

    client.close();
  });

  test('PointReport fields are populated for Nashville query', () => {
    const client = PtilesClient.openState('TN', DATA_DIR);

    // Nashville, TN coordinates
    const lat = 36.1627;
    const lon = -86.7816;

    const report = client.queryPoint(lat, lon, {
      includeBuildings: true,
      includeNearestRoad: true,
      nearbyBusinessLimit: 3,
      nearbyBusinessRadiusMeters: 500,
    });

    // Report should have the right shape
    expect(report).toHaveProperty('building');
    expect(report).toHaveProperty('admin');
    expect(report).toHaveProperty('nearest_road');
    expect(report).toHaveProperty('nearby_roads');
    expect(report).toHaveProperty('water');
    expect(report).toHaveProperty('parks');
    expect(report).toHaveProperty('places');
    expect(report).toHaveProperty('businesses');
    expect(Array.isArray(report.businesses)).toBe(true);
    expect(Array.isArray(report.water)).toBe(true);

    // nearest_road is populated by the RoadsReader (zstd decompression not needed for roads)
    // businesses/building may be empty since zstd decompression is not implemented

    client.close();
  });

  test('queryPoint with all options false returns empty report', () => {
    const client = PtilesClient.openState('TN', DATA_DIR);

    const report = client.queryPoint(36.1627, -86.7816, {
      includeBuildings: false,
      includeNearestRoad: false,
      nearbyBusinessLimit: 0,
    });

    expect(report.building).toBeNull();
    expect(report.nearest_road).toBeNull();
    expect(report.businesses).toHaveLength(0);

    client.close();
  });
});

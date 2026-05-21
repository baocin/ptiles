// src/types.ts — TypeScript interfaces matching Rust data models (section 2.2 of spec)

export interface Building {
  osm_id: number;
  building_type: string;
  centroid_lat: number;
  centroid_lon: number;
  coordinates: [number, number][];
  name: string | null;
  category: string | null;
  name_source: string | null;
  poi_osm_id: number | null;
}

export interface RoadSegment {
  osm_id: number;
  road_class: string;
  coords: [number, number][];
  name: string | null;
  ref_tag: string | null;
  oneway: 'no' | 'forward' | 'reverse' | null;
  speed_limit_kmh: number | null;
  lanes: number | null;
  surface: string | null;
  bridge_tunnel: 'bridge' | 'tunnel' | null;
}

export interface Intersection {
  lon_micro: number;
  lat_micro: number;
  intersection_type: 'traffic_signals' | 'stop' | 'give_way' | 'roundabout';
}

export interface Business {
  osm_id: number;
  lat: number;
  lon: number;
  name: string;
  category: string | null;
  phone: string | null;
  website: string | null;
  address: string | null;
  brand: string | null;
  operating_status: 'open' | 'closed' | 'temporarily_closed' | null;
  emails: string[];
  socials: string[];
}

export interface BusinessHit {
  business: Business;
  distance_meters: number;
}

export interface NearestRoad {
  road: RoadSegment;
  distance_meters: number;
  snapped_lat: number;
  snapped_lon: number;
  segment_index: number;
  along_fraction: number;
}

export interface Route {
  distance_meters: number;
  duration_seconds: number;
  from_cell: bigint;
  to_cell: bigint;
  segments: number;
  path: [number, number][];
  profile: string;
}

export interface AdminInfo {
  country: string;
  state: string;
  county: string;
  zip: string;
  timezone: string;
  boundary_flags: number;
}

export interface AdminPolygon {
  name: string;
  admin_level: number;
  coordinates: [number, number][];
}

export interface WaterFeature {
  osm_id: number;
  water_type: string;
  geom_type: 'polygon' | 'linestring' | 'reference';
  coords: [number, number][];
  ref_feature_id: number | null;
  name: string | null;
  width: number | null;
}

export interface LargeWaterBody {
  feature_id: number;
  name: string;
  water_type: string;
  coords: [number, number][];
}

export interface Place {
  osm_id: number;
  lat: number;
  lon: number;
  place_type: string;
  population: number;
  name: string;
  alt_name: string | null;
  admin_level: number | null;
}

export interface RailFeature {
  osm_id: number;
  rail_type: string;
  geom_type: number; // 0=linestring, 1=point
  coords: [number, number][];
  name: string | null;
}

export interface ParkFeature {
  osm_id: number;
  park_type: string;
  coords: [number, number][];
  name: string | null;
}

export interface Header {
  format: string;
  version: number;
  min_lat: number;
  min_lon: number;
  max_lat: number;
  max_lon: number;
  feature_count: number;
  block_count: number;
  dict_offset: number;
  dict_length: number;
  index_offset: number;
  index_length: number;
  blocks_offset: number;
  aux_offset: number;
  aux_length: number;
  created_at: number;
  data_version: number;
}

export interface PointReport {
  building: Building | null;
  admin: AdminInfo | null;
  nearest_road: NearestRoad | null;
  nearby_roads: NearestRoad[];
  water: WaterFeature[];
  parks: ParkFeature[];
  places: Place[];
  businesses: BusinessHit[];
}

export interface CorridorReport {
  buildings: Building[];
  business: Business[];
  roads: RoadSegment[];
  parks: ParkFeature[];
  water: WaterFeature[];
}

export type RoadProfile = 'driving' | 'walking' | 'cycling';

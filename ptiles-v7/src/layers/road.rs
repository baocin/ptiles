/// Roads layer record (LineString geometry with highway metadata)
///
/// Binary layout:
///   osm_id: varint (delta from previous in block)
///   vertex_count: uint8
///   first_lon: int32 (microdegrees)
///   first_lat: int32 (microdegrees)
///   deltas: varint zigzag pairs × (vertex_count - 1)
///   flags: uint8
///   road_type_idx: uint8
///   [road_type_str]: uint8 len + UTF-8 (if road_type_idx == 255)
///   [name]: uint16 len + UTF-8 (if flags & 0x01)
///   [maxspeed]: uint8 (mph, if flags & 0x02)
///   [layer_z]: int8 (if flags & 0x04, bridge/tunnel level)

use crate::varint;

pub static ROAD_TYPE_REVERSE: [&str; 24] = [
    "motorway", "trunk", "primary", "secondary", "tertiary",
    "unclassified", "residential", "motorway_link", "trunk_link",
    "primary_link", "secondary_link", "tertiary_link",
    "living_street", "service", "pedestrian", "track",
    "bus_guideway", "escape", "raceway", "road",
    "footway", "bridleway", "steps", "path",
];

pub fn road_type_index(s: &str) -> u8 {
    ROAD_TYPE_REVERSE.iter().position(|&t| t == s).map(|i| i as u8).unwrap_or(255)
}

#[derive(Debug, Clone)]
pub struct RoadRecord {
    pub osm_id: u64,
    /// LineString geometry (lon, lat) in degrees.
    pub geometry: Vec<(f64, f64)>,
    pub centroid_lat: f64,
    pub centroid_lon: f64,
    pub road_type: String,
    pub name: Option<String>,
    pub maxspeed_mph: Option<u8>,
    pub layer_z: Option<i8>,
}

impl RoadRecord {
    /// Decode a single road record.
    pub fn decode(data: &[u8], offset: usize, prev_osm_id: u64) -> (Self, usize, u64) {
        let mut pos = offset;

        // OSM ID delta
        let (delta, consumed) = varint::decode_varint(data, pos);
        pos += consumed;
        let osm_id = prev_osm_id + delta;

        // Vertex count
        let vertex_count = data[pos] as usize;
        pos += 1;

        // First coordinates
        let first_lon = i32::from_le_bytes(data[pos..pos + 4].try_into().unwrap());
        pos += 4;
        let first_lat = i32::from_le_bytes(data[pos..pos + 4].try_into().unwrap());
        pos += 4;

        // Decode deltas
        let mut lons = vec![first_lon as f64 / 100_000.0];
        let mut lats = vec![first_lat as f64 / 100_000.0];
        let mut prev_lon = first_lon as i64;
        let mut prev_lat = first_lat as i64;

        for _ in 1..vertex_count {
            let (dl_raw, c1) = varint::decode_varint(data, pos);
            pos += c1;
            let (dla_raw, c2) = varint::decode_varint(data, pos);
            pos += c2;
            prev_lon += varint::zigzag_decode(dl_raw);
            prev_lat += varint::zigzag_decode(dla_raw);
            lons.push(prev_lon as f64 / 100_000.0);
            lats.push(prev_lat as f64 / 100_000.0);
        }

        let geometry: Vec<(f64, f64)> = lons.into_iter().zip(lats).collect();
        let centroid_lon = geometry.iter().map(|c| c.0).sum::<f64>() / geometry.len() as f64;
        let centroid_lat = geometry.iter().map(|c| c.1).sum::<f64>() / geometry.len() as f64;

        // Flags
        let flags = data[pos];
        pos += 1;
        let has_name = flags & 0x01 != 0;
        let has_maxspeed = flags & 0x02 != 0;
        let has_layer_z = flags & 0x04 != 0;

        // Road type
        let rtype_idx = data[pos];
        pos += 1;
        let road_type = if rtype_idx == 255 {
            let rlen = data[pos] as usize;
            pos += 1;
            let s = String::from_utf8_lossy(&data[pos..pos + rlen]).to_string();
            pos += rlen;
            s
        } else {
            ROAD_TYPE_REVERSE.get(rtype_idx as usize).unwrap_or(&"road").to_string()
        };

        // Optional fields
        let name = if has_name {
            let nlen = u16::from_le_bytes(data[pos..pos + 2].try_into().unwrap()) as usize;
            pos += 2;
            let s = String::from_utf8_lossy(&data[pos..pos + nlen]).to_string();
            pos += nlen;
            Some(s)
        } else {
            None
        };

        let maxspeed_mph = if has_maxspeed {
            let v = data[pos];
            pos += 1;
            Some(v)
        } else {
            None
        };

        let layer_z = if has_layer_z {
            let v = data[pos] as i8;
            pos += 1;
            Some(v)
        } else {
            None
        };

        let record = RoadRecord {
            osm_id,
            geometry,
            centroid_lat,
            centroid_lon,
            road_type,
            name,
            maxspeed_mph,
            layer_z,
        };

        (record, pos - offset, osm_id)
    }

    /// Decode all roads from a decompressed block.
    pub fn decode_block(data: &[u8]) -> Vec<Self> {
        let mut records = Vec::new();
        let mut pos = 0;
        let mut prev_osm_id: u64 = 0;

        while pos + 4 < data.len() {
            let record_len = u32::from_le_bytes(data[pos..pos + 4].try_into().unwrap()) as usize;
            pos += 4;
            if pos + record_len > data.len() {
                break;
            }
            let (record, _consumed, new_id) = Self::decode(data, pos, prev_osm_id);
            prev_osm_id = new_id;
            records.push(record);
            pos += record_len;
        }

        records
    }
}

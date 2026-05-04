//! Road record (Layer 1, Linestring).

use crate::records::{decode_coordinates, microdeg_to_deg};
use crate::varint;

/// Indexed road class table.
pub const ROAD_CLASS_TABLE: [&str; 15] = [
    "motorway", "trunk", "primary", "secondary", "motorway_link", "trunk_link",
    "primary_link", "secondary_link", "tertiary", "unclassified", "residential",
    "service", "track", "pedestrian", "path",
];

#[derive(Debug, Clone)]
pub struct Road {
    pub osm_id: u64,
    pub coordinates: Vec<(f64, f64)>,
    pub road_class: String,
    pub name: Option<String>,
    pub ref_num: Option<String>,
    pub maxspeed: Option<u8>,
    pub oneway: Option<bool>,
    pub lanes: Option<u8>,
    pub surface: Option<String>,
}

pub fn decode_road(data: &[u8], pos: usize, prev_osm_id: u64) -> (Road, usize) {
    let start = pos;
    let mut cursor = pos;

    // OSM ID delta
    let (osm_id_delta, consumed) = varint::decode_varint(data, cursor);
    cursor += consumed;
    let osm_id = prev_osm_id + osm_id_delta;

    // Vertex count (uint16)
    let vertex_count = u16::from_le_bytes([data[cursor], data[cursor + 1]]) as usize;
    cursor += 2;

    // First coordinate
    let first_lon = i32::from_le_bytes([data[cursor], data[cursor+1], data[cursor+2], data[cursor+3]]);
    let first_lat = i32::from_le_bytes([data[cursor+4], data[cursor+5], data[cursor+6], data[cursor+7]]);
    cursor += 8;

    // Deltas
    let (micro_coords, consumed) = decode_coordinates(data, cursor, first_lon, first_lat, vertex_count);
    cursor += consumed;
    let coordinates: Vec<(f64, f64)> = micro_coords.iter().map(|&(lon, lat)| (microdeg_to_deg(lon), microdeg_to_deg(lat))).collect();

    // Flags
    let flags = data[cursor]; cursor += 1;
    let has_name = flags & 0x01 != 0;
    let has_ref = flags & 0x02 != 0;
    let has_maxspeed = flags & 0x04 != 0;
    let has_oneway = flags & 0x08 != 0;
    let has_lanes = flags & 0x10 != 0;
    let has_surface = flags & 0x20 != 0;

    // Road class
    let class_idx = data[cursor]; cursor += 1;
    let road_class = if class_idx == 255 {
        let len = data[cursor] as usize; cursor += 1;
        let s = String::from_utf8_lossy(&data[cursor..cursor+len]).into_owned();
        cursor += len; s
    } else {
        ROAD_CLASS_TABLE.get(class_idx as usize).map(|&s| s.to_string()).unwrap_or_else(|| "unclassified".to_string())
    };

    let name = if has_name { let len = u16::from_le_bytes([data[cursor], data[cursor+1]]) as usize; cursor += 2; let s = String::from_utf8_lossy(&data[cursor..cursor+len]).into_owned(); cursor += len; Some(s) } else { None };
    let ref_num = if has_ref { let len = data[cursor] as usize; cursor += 1; let s = String::from_utf8_lossy(&data[cursor..cursor+len]).into_owned(); cursor += len; Some(s) } else { None };
    let maxspeed = if has_maxspeed { let v = data[cursor]; cursor += 1; Some(v) } else { None };
    let oneway = if has_oneway { let v = data[cursor]; cursor += 1; Some(v != 0) } else { None };
    let lanes = if has_lanes { let v = data[cursor]; cursor += 1; Some(v) } else { None };
    let surface = if has_surface { let len = data[cursor] as usize; cursor += 1; let s = String::from_utf8_lossy(&data[cursor..cursor+len]).into_owned(); cursor += len; Some(s) } else { None };

    (Road { osm_id, coordinates, road_class, name, ref_num, maxspeed, oneway, lanes, surface }, cursor - start)
}

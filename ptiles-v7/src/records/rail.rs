//! Rail record (Layer 5, Linestring).

use crate::records::{decode_coordinates, microdeg_to_deg};
use crate::varint;

pub const RAIL_TYPE_TABLE: [&str; 13] = [
    "rail", "light_rail", "tram", "narrow_gauge", "preserved", "abandoned",
    "construction", "subway", "monorail", "funicular", "miniature", "disused", "spur",
];

pub const USAGE_TABLE: [&str; 5] = ["main", "branch", "industrial", "military", "tourism"];
pub const ELECTRIFIED_TABLE: [&str; 3] = ["no", "contact_line", "rail"];

#[derive(Debug, Clone)]
pub struct Railway {
    pub osm_id: u64,
    pub coordinates: Vec<(f64, f64)>,
    pub rail_type: String,
    pub name: Option<String>,
    pub usage: Option<String>,
    pub gauge_mm: Option<u16>,
    pub electrified: Option<String>,
    pub tracks: Option<u8>,
}

pub fn decode_rail(data: &[u8], pos: usize, prev_osm_id: u64) -> (Railway, usize) {
    let start = pos;
    let mut cursor = pos;

    let (osm_id_delta, c) = varint::decode_varint(data, cursor); cursor += c;
    let osm_id = prev_osm_id + osm_id_delta;

    let vertex_count = u16::from_le_bytes([data[cursor], data[cursor+1]]) as usize; cursor += 2;
    let first_lon = i32::from_le_bytes([data[cursor], data[cursor+1], data[cursor+2], data[cursor+3]]);
    let first_lat = i32::from_le_bytes([data[cursor+4], data[cursor+5], data[cursor+6], data[cursor+7]]);
    cursor += 8;

    let (micro_coords, c) = decode_coordinates(data, cursor, first_lon, first_lat, vertex_count); cursor += c;
    let coordinates: Vec<(f64, f64)> = micro_coords.iter().map(|&(lon, lat)| (microdeg_to_deg(lon), microdeg_to_deg(lat))).collect();

    let flags = data[cursor]; cursor += 1;
    let has_name = flags & 0x01 != 0;
    let has_usage = flags & 0x02 != 0;
    let has_gauge = flags & 0x04 != 0;
    let has_electrified = flags & 0x08 != 0;
    let has_tracks = flags & 0x10 != 0;

    let type_idx = data[cursor]; cursor += 1;
    let rail_type = if type_idx == 255 {
        let len = data[cursor] as usize; cursor += 1;
        let s = String::from_utf8_lossy(&data[cursor..cursor+len]).into_owned(); cursor += len; s
    } else {
        RAIL_TYPE_TABLE.get(type_idx as usize).map(|&s| s.to_string()).unwrap_or_else(|| "rail".to_string())
    };

    let name = if has_name { let len = u16::from_le_bytes([data[cursor], data[cursor+1]]) as usize; cursor += 2; let s = String::from_utf8_lossy(&data[cursor..cursor+len]).into_owned(); cursor += len; Some(s) } else { None };
    let usage = if has_usage { let u = data[cursor]; cursor += 1; Some(USAGE_TABLE.get(u as usize).map(|&s| s.to_string()).unwrap_or_else(|| "main".to_string())) } else { None };
    let gauge_mm = if has_gauge { let v = u16::from_le_bytes([data[cursor], data[cursor+1]]); cursor += 2; Some(v) } else { None };
    let electrified = if has_electrified { let e = data[cursor]; cursor += 1; Some(ELECTRIFIED_TABLE.get(e as usize).map(|&s| s.to_string()).unwrap_or_else(|| "no".to_string())) } else { None };
    let tracks = if has_tracks { let v = data[cursor]; cursor += 1; Some(v) } else { None };

    (Railway { osm_id, coordinates, rail_type, name, usage, gauge_mm, electrified, tracks }, cursor - start)
}

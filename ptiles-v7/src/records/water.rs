//! Water body record (Layer 4, Polygon).

use crate::records::{decode_coordinates, microdeg_to_deg};
use crate::varint;

pub const WATER_TYPE_TABLE: [&str; 13] = [
    "lake", "pond", "river", "stream", "ocean", "sea", "riverbank",
    "reservoir", "basin", "canal", "bay", "strait", "wetland",
];

#[derive(Debug, Clone)]
pub struct WaterBody {
    pub osm_id: u64,
    pub coordinates: Vec<(f64, f64)>,
    pub water_type: String,
    pub name: Option<String>,
}

pub fn decode_water(data: &[u8], pos: usize, prev_osm_id: u64) -> (WaterBody, usize) {
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

    let type_idx = data[cursor]; cursor += 1;
    let water_type = if type_idx == 255 {
        let len = data[cursor] as usize; cursor += 1;
        let s = String::from_utf8_lossy(&data[cursor..cursor+len]).into_owned(); cursor += len; s
    } else {
        WATER_TYPE_TABLE.get(type_idx as usize).map(|&s| s.to_string()).unwrap_or_else(|| "water".to_string())
    };

    let name = if has_name { let len = u16::from_le_bytes([data[cursor], data[cursor+1]]) as usize; cursor += 2; let s = String::from_utf8_lossy(&data[cursor..cursor+len]).into_owned(); cursor += len; Some(s) } else { None };

    (WaterBody { osm_id, coordinates, water_type, name }, cursor - start)
}

//! Park / protected area record (Layer 6, Polygon).

use crate::records::{decode_coordinates, microdeg_to_deg};
use crate::varint;

pub const LEISURE_TYPE_TABLE: [&str; 15] = [
    "park", "nature_reserve", "forest", "wood", "common", "recreation_ground",
    "national_park", "protected_area", "garden", "golf_course", "pitch", "playground",
    "beach", "meadow", "campground",
];

pub const ACCESS_TABLE: [&str; 4] = ["yes", "permissive", "private", "customers"];

#[derive(Debug, Clone)]
pub struct Park {
    pub osm_id: u64,
    pub coordinates: Vec<(f64, f64)>,
    pub leisure_type: String,
    pub name: Option<String>,
    pub operator: Option<String>,
    pub access: Option<String>,
    pub website: Option<String>,
}

pub fn decode_park(data: &[u8], pos: usize, prev_osm_id: u64) -> (Park, usize) {
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
    let has_operator = flags & 0x02 != 0;
    let has_access = flags & 0x04 != 0;
    let has_website = flags & 0x08 != 0;

    let type_idx = data[cursor]; cursor += 1;
    let leisure_type = if type_idx == 255 {
        let len = data[cursor] as usize; cursor += 1;
        let s = String::from_utf8_lossy(&data[cursor..cursor+len]).into_owned(); cursor += len; s
    } else {
        LEISURE_TYPE_TABLE.get(type_idx as usize).map(|&s| s.to_string()).unwrap_or_else(|| "park".to_string())
    };

    let name = if has_name { let len = u16::from_le_bytes([data[cursor], data[cursor+1]]) as usize; cursor += 2; let s = String::from_utf8_lossy(&data[cursor..cursor+len]).into_owned(); cursor += len; Some(s) } else { None };
    let operator = if has_operator { let len = data[cursor] as usize; cursor += 1; let s = String::from_utf8_lossy(&data[cursor..cursor+len]).into_owned(); cursor += len; Some(s) } else { None };
    let access = if has_access { let a = data[cursor]; cursor += 1; Some(ACCESS_TABLE.get(a as usize).map(|&s| s.to_string()).unwrap_or_else(|| "yes".to_string())) } else { None };
    let website = if has_website { let len = u16::from_le_bytes([data[cursor], data[cursor+1]]) as usize; cursor += 2; let s = String::from_utf8_lossy(&data[cursor..cursor+len]).into_owned(); cursor += len; Some(s) } else { None };

    (Park { osm_id, coordinates, leisure_type, name, operator, access, website }, cursor - start)
}

//! Admin boundary record (Layer 3, Polygon).

use crate::records::{decode_coordinates, microdeg_to_deg};
use crate::varint;

#[derive(Debug, Clone)]
pub struct AdminBoundary {
    pub osm_id: u64,
    pub coordinates: Vec<(f64, f64)>,
    pub admin_level: u8,
    pub name: Option<String>,
    pub iso_code: Option<String>,
    pub wikidata: Option<String>,
}

pub fn decode_admin(data: &[u8], pos: usize, prev_osm_id: u64) -> (AdminBoundary, usize) {
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
    let has_iso = flags & 0x02 != 0;
    let has_wikidata = flags & 0x04 != 0;

    let admin_level = data[cursor]; cursor += 1;

    let name = if has_name { let len = u16::from_le_bytes([data[cursor], data[cursor+1]]) as usize; cursor += 2; let s = String::from_utf8_lossy(&data[cursor..cursor+len]).into_owned(); cursor += len; Some(s) } else { None };
    let iso_code = if has_iso { let len = data[cursor] as usize; cursor += 1; let s = String::from_utf8_lossy(&data[cursor..cursor+len]).into_owned(); cursor += len; Some(s) } else { None };
    let wikidata = if has_wikidata { let len = data[cursor] as usize; cursor += 1; let s = String::from_utf8_lossy(&data[cursor..cursor+len]).into_owned(); cursor += len; Some(s) } else { None };

    (AdminBoundary { osm_id, coordinates, admin_level, name, iso_code, wikidata }, cursor - start)
}

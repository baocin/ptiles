//! Place record (Layer 2, Point).

use crate::varint;

pub const PLACE_TYPE_TABLE: [&str; 15] = [
    "city", "town", "village", "hamlet", "county", "state", "country", "region",
    "suburb", "neighbourhood", "locality", "island", "farm", "continent", "ocean",
];

#[derive(Debug, Clone)]
pub struct Place {
    pub osm_id: u64,
    pub lon: f64,
    pub lat: f64,
    pub place_type: String,
    pub name: Option<String>,
    pub population: Option<u32>,
    pub capital_admin_level: Option<u8>,
    pub wikidata: Option<String>,
}

/// Decode a place record. Places use absolute coordinates at ×1,000,000 precision.
pub fn decode_place(data: &[u8], pos: usize, prev_osm_id: u64) -> (Place, usize) {
    let start = pos;
    let mut cursor = pos;

    let (osm_id_delta, c) = varint::decode_varint(data, cursor); cursor += c;
    let osm_id = prev_osm_id + osm_id_delta;

    let lon_raw = i32::from_le_bytes([data[cursor], data[cursor+1], data[cursor+2], data[cursor+3]]); cursor += 4;
    let lat_raw = i32::from_le_bytes([data[cursor], data[cursor+1], data[cursor+2], data[cursor+3]]); cursor += 4;
    let lon = lon_raw as f64 / 1_000_000.0;
    let lat = lat_raw as f64 / 1_000_000.0;

    let flags = data[cursor]; cursor += 1;
    let has_name = flags & 0x01 != 0;
    let has_population = flags & 0x02 != 0;
    let has_capital = flags & 0x04 != 0;
    let has_wikidata = flags & 0x08 != 0;

    let type_idx = data[cursor]; cursor += 1;
    let place_type = if type_idx == 255 {
        let len = data[cursor] as usize; cursor += 1;
        let s = String::from_utf8_lossy(&data[cursor..cursor+len]).into_owned(); cursor += len; s
    } else {
        PLACE_TYPE_TABLE.get(type_idx as usize).map(|&s| s.to_string()).unwrap_or_else(|| "locality".to_string())
    };

    let name = if has_name { let len = u16::from_le_bytes([data[cursor], data[cursor+1]]) as usize; cursor += 2; let s = String::from_utf8_lossy(&data[cursor..cursor+len]).into_owned(); cursor += len; Some(s) } else { None };
    let population = if has_population { let v = u32::from_le_bytes([data[cursor], data[cursor+1], data[cursor+2], data[cursor+3]]); cursor += 4; Some(v) } else { None };
    let capital_admin_level = if has_capital { let v = data[cursor]; cursor += 1; Some(v) } else { None };
    let wikidata = if has_wikidata { let len = data[cursor] as usize; cursor += 1; let s = String::from_utf8_lossy(&data[cursor..cursor+len]).into_owned(); cursor += len; Some(s) } else { None };

    (Place { osm_id, lon, lat, place_type, name, population, capital_admin_level, wikidata }, cursor - start)
}

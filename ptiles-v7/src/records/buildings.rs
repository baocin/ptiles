//! Building record (Layer 0, Polygon).
//!
//! V6-compatible building footprint format with delta-encoded coordinates,
//! indexed building types, and optional name/category/height fields.

use crate::records::{decode_coordinates, microdeg_to_deg};
use crate::varint;

/// Indexed building type lookup table (same 20 types as v6).
pub const BTYPE_TABLE: [&str; 20] = [
    "yes",
    "house",
    "residential",
    "commercial",
    "industrial",
    "retail",
    "garage",
    "apartments",
    "office",
    "warehouse",
    "shed",
    "detached",
    "terrace",
    "school",
    "church",
    "hospital",
    "hotel",
    "roof",
    "construction",
    "barn",
];

/// A decoded building record.
#[derive(Debug, Clone)]
pub struct Building {
    /// OpenStreetMap ID.
    pub osm_id: u64,

    /// Polygon coordinates in (lon_deg, lat_deg).
    pub coordinates: Vec<(f64, f64)>,

    /// Centroid latitude.
    pub centroid_lat: f64,

    /// Centroid longitude.
    pub centroid_lon: f64,

    /// Building type string (e.g. "house", "commercial").
    pub building_type: String,

    /// Optional building name.
    pub name: Option<String>,

    /// Optional category.
    pub category: Option<String>,

    /// Optional name source.
    pub name_source: Option<String>,

    /// Optional POI OSM ID (if building has a POI node).
    pub poi_osm_id: Option<u64>,

    /// Optional height in meters.
    pub height_m: Option<f64>,
}

/// Decode a single building record from binary data.
///
/// Returns the decoded building and total bytes consumed.
pub fn decode_building(data: &[u8], pos: usize, prev_osm_id: u64) -> (Building, usize) {
    let start = pos;
    let mut cursor = pos;

    // OSM ID delta
    let (osm_id_delta, consumed) = varint::decode_varint(data, cursor);
    cursor += consumed;
    let osm_id = prev_osm_id + osm_id_delta;

    // Vertex count
    let vertex_count = data[cursor] as usize;
    cursor += 1;

    // First coordinate (int32 lon, int32 lat)
    let first_lon = i32::from_le_bytes([
        data[cursor],
        data[cursor + 1],
        data[cursor + 2],
        data[cursor + 3],
    ]);
    let first_lat = i32::from_le_bytes([
        data[cursor + 4],
        data[cursor + 5],
        data[cursor + 6],
        data[cursor + 7],
    ]);
    cursor += 8;

    // Delta coordinates
    let (micro_coords, consumed) = decode_coordinates(data, cursor, first_lon, first_lat, vertex_count);
    cursor += consumed;

    let coordinates: Vec<(f64, f64)> = micro_coords
        .iter()
        .map(|&(lon, lat)| (microdeg_to_deg(lon), microdeg_to_deg(lat)))
        .collect();

    // Flags
    let flags = data[cursor];
    cursor += 1;
    let has_name = flags & 0x01 != 0;
    let has_category = flags & 0x02 != 0;
    let has_name_source = flags & 0x04 != 0;
    let has_poi_osm_id = flags & 0x08 != 0;
    let has_height = flags & 0x10 != 0;

    // Building type
    let btype_idx = data[cursor];
    cursor += 1;
    let building_type = if btype_idx == 255 {
        let len = data[cursor] as usize;
        cursor += 1;
        let s = String::from_utf8_lossy(&data[cursor..cursor + len]).into_owned();
        cursor += len;
        s
    } else {
        BTYPE_TABLE
            .get(btype_idx as usize)
            .map(|&s| s.to_string())
            .unwrap_or_else(|| "yes".to_string())
    };

    // Centroid
    let centroid_lat = coordinates.iter().map(|c| c.1).sum::<f64>() / coordinates.len() as f64;
    let centroid_lon = coordinates.iter().map(|c| c.0).sum::<f64>() / coordinates.len() as f64;

    // Optional fields
    let name = if has_name {
        let len = u16::from_le_bytes([data[cursor], data[cursor + 1]]) as usize;
        cursor += 2;
        let s = String::from_utf8_lossy(&data[cursor..cursor + len]).into_owned();
        cursor += len;
        Some(s)
    } else {
        None
    };

    let category = if has_category {
        let len = data[cursor] as usize;
        cursor += 1;
        let s = String::from_utf8_lossy(&data[cursor..cursor + len]).into_owned();
        cursor += len;
        Some(s)
    } else {
        None
    };

    let name_source = if has_name_source {
        let len = data[cursor] as usize;
        cursor += 1;
        let s = String::from_utf8_lossy(&data[cursor..cursor + len]).into_owned();
        cursor += len;
        Some(s)
    } else {
        None
    };

    let poi_osm_id = if has_poi_osm_id {
        let v = u64::from_le_bytes([
            data[cursor],
            data[cursor + 1],
            data[cursor + 2],
            data[cursor + 3],
            data[cursor + 4],
            data[cursor + 5],
            data[cursor + 6],
            data[cursor + 7],
        ]);
        cursor += 8;
        Some(v)
    } else {
        None
    };

    let height_m = if has_height {
        let h = data[cursor] as f64 * 0.5;
        cursor += 1;
        Some(h)
    } else {
        None
    };

    let building = Building {
        osm_id,
        coordinates,
        centroid_lat,
        centroid_lon,
        building_type,
        name,
        category,
        name_source,
        poi_osm_id,
        height_m,
    };

    (building, cursor - start)
}

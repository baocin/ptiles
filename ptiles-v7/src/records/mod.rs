//! Per-layer record formats for PTiles v7.
//!
//! Each layer has its own record structure optimized for the geometry type
//! and metadata typical of that OSM feature class.

pub mod admin;
pub mod buildings;
pub mod parks;
pub mod places;
pub mod rail;
pub mod roads;
pub mod water;

use crate::varint;

/// Decode a sequence of zigzag delta-encoded coordinates.
///
/// Reads `vertex_count - 1` delta pairs from `data` starting at `pos`,
/// applying each delta to the `first_lon`/`first_lat` origin.
/// Returns the full coordinate list and bytes consumed.
///
/// Coordinates are in microdegrees (degrees × 100,000).
pub fn decode_coordinates(
    data: &[u8],
    mut pos: usize,
    first_lon: i32,
    first_lat: i32,
    vertex_count: usize,
) -> (Vec<(i32, i32)>, usize) {
    let start_pos = pos;
    let mut coords = Vec::with_capacity(vertex_count);
    let mut prev_lon = first_lon;
    let mut prev_lat = first_lat;

    coords.push((prev_lon, prev_lat));

    for _ in 1..vertex_count {
        let (delta_lon_raw, consumed) = varint::decode_varint(data, pos);
        pos += consumed;
        let (delta_lat_raw, consumed) = varint::decode_varint(data, pos);
        pos += consumed;

        let delta_lon = varint::zigzag_decode(delta_lon_raw) as i32;
        let delta_lat = varint::zigzag_decode(delta_lat_raw) as i32;

        prev_lon = prev_lon.wrapping_add(delta_lon);
        prev_lat = prev_lat.wrapping_add(delta_lat);
        coords.push((prev_lon, prev_lat));
    }

    (coords, pos - start_pos)
}

/// Convert a coordinate from microdegrees to degrees.
#[inline]
pub fn microdeg_to_deg(microdeg: i32) -> f64 {
    microdeg as f64 / 100_000.0
}

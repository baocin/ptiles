/// Buildings layer record (polygon footprints with metadata)
///
/// Binary layout (same as v6 building format):
///   osm_id: varint (delta from previous in block)
///   vertex_count: uint8
///   first_lon: int32 (microdegrees)
///   first_lat: int32 (microdegrees)
///   deltas: varint zigzag pairs (delta_lon, delta_lat) × (vertex_count - 1)
///   flags: uint8
///   btype_idx: uint8
///   [btype_str]: uint8 len + UTF-8 (if btype_idx == 255)
///   [name]: uint16 len + UTF-8 (if flags & 0x01)
///   [category]: uint8 len + UTF-8 (if flags & 0x02)
///   [name_source]: uint8 len + UTF-8 (if flags & 0x04)
///   [poi_osm_id]: uint64 (if flags & 0x08)
///   [height]: uint8 (if flags & 0x10, 0.5m steps)

use crate::varint;

/// Reverse lookup: index → building type string.
pub static BTYPE_REVERSE: [&str; 20] = [
    "yes", "house", "residential", "commercial", "industrial",
    "retail", "garage", "apartments", "office", "warehouse",
    "shed", "detached", "terrace", "school", "church",
    "hospital", "hotel", "roof", "construction", "barn",
];

/// Forward lookup: building type string → index.
pub fn btype_index(s: &str) -> u8 {
    BTYPE_REVERSE.iter().position(|&t| t == s).map(|i| i as u8).unwrap_or(255)
}

#[derive(Debug, Clone)]
pub struct BuildingRecord {
    pub osm_id: u64,
    /// Polygon coordinates as (lon, lat) pairs in degrees.
    pub geometry: Vec<(f64, f64)>,
    pub centroid_lat: f64,
    pub centroid_lon: f64,
    pub building_type: String,
    pub name: Option<String>,
    pub category: Option<String>,
    pub name_source: Option<String>,
    pub poi_osm_id: Option<u64>,
    pub height_m: Option<f64>,
}

impl BuildingRecord {
    /// Decode a single building from bytes at `offset`.
    /// Returns (record, bytes_consumed, new_osm_id).
    pub fn decode(data: &[u8], offset: usize, prev_osm_id: u64) -> (Self, usize, u64) {
        let mut pos = offset;

        // OSM ID delta
        let (delta, consumed) = varint::decode_varint(data, pos);
        pos += consumed;
        let osm_id = prev_osm_id + delta;

        // Vertex count
        let vertex_count = data[pos] as usize;
        pos += 1;

        // First coordinates (int32 lon, int32 lat in microdegrees)
        let first_lon = i32::from_le_bytes(data[pos..pos + 4].try_into().unwrap());
        pos += 4;
        let first_lat = i32::from_le_bytes(data[pos..pos + 4].try_into().unwrap());
        pos += 4;

        // Decode coordinate deltas
        let mut lons: Vec<f64> = Vec::with_capacity(vertex_count);
        let mut lats: Vec<f64> = Vec::with_capacity(vertex_count);
        lons.push(first_lon as f64 / 100_000.0);
        lats.push(first_lat as f64 / 100_000.0);

        let mut prev_lon = first_lon as i64;
        let mut prev_lat = first_lat as i64;

        for _ in 1..vertex_count {
            let (delta_lon_raw, c1) = varint::decode_varint(data, pos);
            pos += c1;
            let (delta_lat_raw, c2) = varint::decode_varint(data, pos);
            pos += c2;

            let delta_lon = varint::zigzag_decode(delta_lon_raw);
            let delta_lat = varint::zigzag_decode(delta_lat_raw);

            prev_lon += delta_lon;
            prev_lat += delta_lat;

            lons.push(prev_lon as f64 / 100_000.0);
            lats.push(prev_lat as f64 / 100_000.0);
        }

        let geometry: Vec<(f64, f64)> = lons.into_iter().zip(lats).collect();

        // Centroid
        let centroid_lon = geometry.iter().map(|c| c.0).sum::<f64>() / geometry.len() as f64;
        let centroid_lat = geometry.iter().map(|c| c.1).sum::<f64>() / geometry.len() as f64;

        // Flags
        let flags = data[pos];
        pos += 1;
        let has_name = flags & 0x01 != 0;
        let has_category = flags & 0x02 != 0;
        let has_name_source = flags & 0x04 != 0;
        let has_poi_osm_id = flags & 0x08 != 0;
        let has_height = flags & 0x10 != 0;

        // Building type
        let btype_idx = data[pos];
        pos += 1;
        let building_type = if btype_idx == 255 {
            let btype_len = data[pos] as usize;
            pos += 1;
            let s = String::from_utf8_lossy(&data[pos..pos + btype_len]).to_string();
            pos += btype_len;
            s
        } else {
            BTYPE_REVERSE
                .get(btype_idx as usize)
                .unwrap_or(&"yes")
                .to_string()
        };

        // Optional fields
        let mut name = None;
        let mut category = None;
        let mut name_source = None;
        let mut poi_osm_id = None;
        let mut height_m = None;

        if has_name {
            let name_len = u16::from_le_bytes(data[pos..pos + 2].try_into().unwrap()) as usize;
            pos += 2;
            name = Some(String::from_utf8_lossy(&data[pos..pos + name_len]).to_string());
            pos += name_len;
        }
        if has_category {
            let cat_len = data[pos] as usize;
            pos += 1;
            category = Some(String::from_utf8_lossy(&data[pos..pos + cat_len]).to_string());
            pos += cat_len;
        }
        if has_name_source {
            let src_len = data[pos] as usize;
            pos += 1;
            name_source = Some(String::from_utf8_lossy(&data[pos..pos + src_len]).to_string());
            pos += src_len;
        }
        if has_poi_osm_id {
            poi_osm_id = Some(u64::from_le_bytes(data[pos..pos + 8].try_into().unwrap()));
            pos += 8;
        }
        if has_height {
            height_m = Some(data[pos] as f64 * 0.5);
            pos += 1;
        }

        let record = BuildingRecord {
            osm_id,
            geometry,
            centroid_lat,
            centroid_lon,
            building_type,
            name,
            category,
            name_source,
            poi_osm_id,
            height_m,
        };

        (record, pos - offset, osm_id)
    }

    /// Decode all buildings from a decompressed block.
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

    /// Encode buildings into a block (for writing/builder tools).
    pub fn encode_block(records: &[Self]) -> Vec<u8> {
        let mut block = Vec::new();
        let mut prev_osm_id: u64 = 0;

        for rec in records {
            let mut record_data = Vec::new();

            // Delta OSM ID
            let delta = rec.osm_id.wrapping_sub(prev_osm_id);
            varint::encode_varint(delta, &mut record_data);
            prev_osm_id = rec.osm_id;

            // Vertex count
            let vc = rec.geometry.len().min(255);
            record_data.push(vc as u8);

            // First coordinates in microdegrees
            let first_lon = (rec.geometry[0].0 * 100_000.0) as i32;
            let first_lat = (rec.geometry[0].1 * 100_000.0) as i32;
            record_data.extend_from_slice(&first_lon.to_le_bytes());
            record_data.extend_from_slice(&first_lat.to_le_bytes());

            // Delta coordinates
            let mut prev_lon = first_lon as i64;
            let mut prev_lat = first_lat as i64;
            for i in 1..vc {
                let cur_lon = (rec.geometry[i].0 * 100_000.0) as i64;
                let cur_lat = (rec.geometry[i].1 * 100_000.0) as i64;
                let delta_lon = cur_lon - prev_lon;
                let delta_lat = cur_lat - prev_lat;
                varint::encode_varint(varint::zigzag_encode(delta_lon), &mut record_data);
                varint::encode_varint(varint::zigzag_encode(delta_lat), &mut record_data);
                prev_lon = cur_lon;
                prev_lat = cur_lat;
            }

            // Flags
            let mut flags: u8 = 0;
            if rec.name.is_some() { flags |= 0x01; }
            if rec.category.is_some() { flags |= 0x02; }
            if rec.name_source.is_some() { flags |= 0x04; }
            if rec.poi_osm_id.is_some() { flags |= 0x08; }
            if rec.height_m.is_some() { flags |= 0x10; }
            record_data.push(flags);

            // Building type
            let btype_idx = btype_index(&rec.building_type);
            record_data.push(btype_idx);
            if btype_idx == 255 {
                let s = rec.building_type.as_bytes();
                record_data.push(s.len() as u8);
                record_data.extend_from_slice(s);
            }

            // Optional fields
            if let Some(ref name) = rec.name {
                let name_bytes = name.as_bytes();
                record_data.extend_from_slice(&(name_bytes.len() as u16).to_le_bytes());
                record_data.extend_from_slice(name_bytes);
            }
            if let Some(ref cat) = rec.category {
                record_data.push(cat.len() as u8);
                record_data.extend_from_slice(cat.as_bytes());
            }
            if let Some(ref src) = rec.name_source {
                record_data.push(src.len() as u8);
                record_data.extend_from_slice(src.as_bytes());
            }
            if let Some(poi_id) = rec.poi_osm_id {
                record_data.extend_from_slice(&poi_id.to_le_bytes());
            }
            if let Some(h) = rec.height_m {
                record_data.push((h / 0.5) as u8);
            }

            // Write record_length + record_data
            block.extend_from_slice(&(record_data.len() as u32).to_le_bytes());
            block.extend_from_slice(&record_data);
        }

        block
    }
}

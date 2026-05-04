/// Water layer record (polygon geometry: lakes, rivers, reservoirs, etc.)
///
/// Binary layout:
///   osm_id: varint (delta)
///   vertex_count: uint8
///   first_lon: int32 (microdegrees)
///   first_lat: int32 (microdegrees)
///   deltas: varint zigzag pairs × (vertex_count - 1)
///   flags: uint8
///   water_type_idx: uint8
///   [water_type_str]: uint8 len + UTF-8 (if water_type_idx == 255)
///   [name]: uint16 len + UTF-8 (if flags & 0x01)

use crate::varint;

pub static WATER_TYPE_REVERSE: [&str; 12] = [
    "lake", "reservoir", "river", "stream", "canal",
    "pond", "basin", "lagoon", "oxbow", "riverbank",
    "wetland", "bay",
];

pub fn water_type_index(s: &str) -> u8 {
    WATER_TYPE_REVERSE.iter().position(|&t| t == s).map(|i| i as u8).unwrap_or(255)
}

#[derive(Debug, Clone)]
pub struct WaterRecord {
    pub osm_id: u64,
    pub geometry: Vec<(f64, f64)>,
    pub centroid_lat: f64,
    pub centroid_lon: f64,
    pub water_type: String,
    pub name: Option<String>,
}

impl WaterRecord {
    pub fn decode(data: &[u8], offset: usize, prev_osm_id: u64) -> (Self, usize, u64) {
        let mut pos = offset;

        let (delta, consumed) = varint::decode_varint(data, pos);
        pos += consumed;
        let osm_id = prev_osm_id + delta;

        let vertex_count = data[pos] as usize;
        pos += 1;

        let first_lon = i32::from_le_bytes(data[pos..pos + 4].try_into().unwrap());
        pos += 4;
        let first_lat = i32::from_le_bytes(data[pos..pos + 4].try_into().unwrap());
        pos += 4;

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

        let flags = data[pos];
        pos += 1;
        let has_name = flags & 0x01 != 0;

        let wtype_idx = data[pos];
        pos += 1;
        let water_type = if wtype_idx == 255 {
            let wlen = data[pos] as usize;
            pos += 1;
            let s = String::from_utf8_lossy(&data[pos..pos + wlen]).to_string();
            pos += wlen;
            s
        } else {
            WATER_TYPE_REVERSE.get(wtype_idx as usize).unwrap_or(&"lake").to_string()
        };

        let name = if has_name {
            let nlen = u16::from_le_bytes(data[pos..pos + 2].try_into().unwrap()) as usize;
            pos += 2;
            let s = String::from_utf8_lossy(&data[pos..pos + nlen]).to_string();
            pos += nlen;
            Some(s)
        } else {
            None
        };

        let record = WaterRecord {
            osm_id,
            geometry,
            centroid_lat,
            centroid_lon,
            water_type,
            name,
        };

        (record, pos - offset, osm_id)
    }

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

/// Admin boundaries layer record (polygon geometry with admin metadata)
///
/// Binary layout:
///   osm_id: varint (delta)
///   admin_level: uint8
///   vertex_count: uint8
///   first_lon: int32 (microdegrees)
///   first_lat: int32 (microdegrees)
///   deltas: varint zigzag pairs × (vertex_count - 1)
///   flags: uint8
///   name: uint16 len + UTF-8 (always present)
///   [iso_code]: uint8 len + UTF-8 (if flags & 0x01)

use crate::varint;

#[derive(Debug, Clone)]
pub struct AdminRecord {
    pub osm_id: u64,
    pub admin_level: u8,
    pub geometry: Vec<(f64, f64)>,
    pub centroid_lat: f64,
    pub centroid_lon: f64,
    pub name: String,
    pub iso_code: Option<String>,
}

impl AdminRecord {
    pub fn decode(data: &[u8], offset: usize, prev_osm_id: u64) -> (Self, usize, u64) {
        let mut pos = offset;

        let (delta, consumed) = varint::decode_varint(data, pos);
        pos += consumed;
        let osm_id = prev_osm_id + delta;

        let admin_level = data[pos];
        pos += 1;

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
        let has_iso = flags & 0x01 != 0;

        let name_len = u16::from_le_bytes(data[pos..pos + 2].try_into().unwrap()) as usize;
        pos += 2;
        let name = String::from_utf8_lossy(&data[pos..pos + name_len]).to_string();
        pos += name_len;

        let iso_code = if has_iso {
            let ilen = data[pos] as usize;
            pos += 1;
            let s = String::from_utf8_lossy(&data[pos..pos + ilen]).to_string();
            pos += ilen;
            Some(s)
        } else {
            None
        };

        let record = AdminRecord {
            osm_id,
            admin_level,
            geometry,
            centroid_lat,
            centroid_lon,
            name,
            iso_code,
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

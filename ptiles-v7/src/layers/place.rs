/// Places layer record (point geometry: cities, towns, villages, etc.)
///
/// Binary layout:
///   osm_id: varint (delta from previous in block)
///   lon: int32 (microdegrees)
///   lat: int32 (microdegrees)
///   flags: uint8
///   place_type_idx: uint8
///   [place_type_str]: uint8 len + UTF-8 (if place_type_idx == 255)
///   name: uint16 len + UTF-8 (always present)
///   [population]: varint (if flags & 0x01)

use crate::varint;

pub static PLACE_TYPE_REVERSE: [&str; 14] = [
    "city", "town", "village", "hamlet", "suburb",
    "quarter", "neighbourhood", "isolated_dwelling", "farm",
    "county", "state", "country", "continent", "locality",
];

pub fn place_type_index(s: &str) -> u8 {
    PLACE_TYPE_REVERSE.iter().position(|&t| t == s).map(|i| i as u8).unwrap_or(255)
}

#[derive(Debug, Clone)]
pub struct PlaceRecord {
    pub osm_id: u64,
    pub lon: f64,
    pub lat: f64,
    pub place_type: String,
    pub name: String,
    pub population: Option<u64>,
}

impl PlaceRecord {
    pub fn decode(data: &[u8], offset: usize, prev_osm_id: u64) -> (Self, usize, u64) {
        let mut pos = offset;

        let (delta, consumed) = varint::decode_varint(data, pos);
        pos += consumed;
        let osm_id = prev_osm_id + delta;

        let lon = i32::from_le_bytes(data[pos..pos + 4].try_into().unwrap()) as f64 / 100_000.0;
        pos += 4;
        let lat = i32::from_le_bytes(data[pos..pos + 4].try_into().unwrap()) as f64 / 100_000.0;
        pos += 4;

        let flags = data[pos];
        pos += 1;
        let has_population = flags & 0x01 != 0;

        let ptype_idx = data[pos];
        pos += 1;
        let place_type = if ptype_idx == 255 {
            let plen = data[pos] as usize;
            pos += 1;
            let s = String::from_utf8_lossy(&data[pos..pos + plen]).to_string();
            pos += plen;
            s
        } else {
            PLACE_TYPE_REVERSE.get(ptype_idx as usize).unwrap_or(&"locality").to_string()
        };

        let name_len = u16::from_le_bytes(data[pos..pos + 2].try_into().unwrap()) as usize;
        pos += 2;
        let name = String::from_utf8_lossy(&data[pos..pos + name_len]).to_string();
        pos += name_len;

        let population = if has_population {
            let (pop, c) = varint::decode_varint(data, pos);
            pos += c;
            Some(pop)
        } else {
            None
        };

        let record = PlaceRecord {
            osm_id,
            lon,
            lat,
            place_type,
            name,
            population,
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

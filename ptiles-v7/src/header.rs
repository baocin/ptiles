/// PTiles v7 Header (256 bytes)
///
/// Magic: "PTILEST\x00" (T = timeline)
/// Version: 7
///
/// Layout:
///   Offset | Size | Type   | Field
///   -------|------|--------|-------
///   0      | 8    | bytes  | magic ("PTILEST\x00")
///   8      | 1    | uint8  | version (7)
///   9      | 1    | uint8  | layer_count
///   10     | 6    | -      | reserved
///   16     | 4    | float  | min_lat
///   20     | 4    | float  | min_lon
///   24     | 4    | float  | max_lat
///   28     | 4    | float  | max_lon
///   32     | 8    | uint64 | total_poi_count
///   40     | 8    | uint64 | dict_offset
///   48     | 4    | uint32 | dict_length
///   52     | 8    | uint64 | layer_dir_offset
///   60     | 4    | uint32 | layer_dir_length
///   64     | 192  | -      | reserved (future use)

use std::io::{Read, Seek, SeekFrom};
use byteorder::{ByteOrder, LittleEndian, ReadBytesExt};

pub const HEADER_SIZE: usize = 256;
pub const MAGIC: &[u8; 8] = b"PTILEST\x00";
pub const VERSION: u8 = 7;

#[derive(Debug, Clone)]
pub struct Header {
    pub version: u8,
    pub layer_count: u8,
    pub min_lat: f32,
    pub min_lon: f32,
    pub max_lat: f32,
    pub max_lon: f32,
    pub total_poi_count: u64,
    pub dict_offset: u64,
    pub dict_length: u32,
    pub layer_dir_offset: u64,
    pub layer_dir_length: u32,
}

impl Header {
    /// Read and validate the v7 header from a reader.
    pub fn read<R: Read + Seek>(reader: &mut R) -> std::io::Result<Self> {
        reader.seek(SeekFrom::Start(0))?;

        // Magic
        let mut magic = [0u8; 8];
        reader.read_exact(&mut magic)?;
        if &magic != MAGIC {
            return Err(std::io::Error::new(
                std::io::ErrorKind::InvalidData,
                format!(
                    "Invalid magic: expected {:?}, got {:?}",
                    std::str::from_utf8(MAGIC).unwrap_or("??"),
                    std::str::from_utf8(&magic).unwrap_or("??")
                ),
            ));
        }

        // Version
        let version = reader.read_u8()?;
        if version != VERSION {
            return Err(std::io::Error::new(
                std::io::ErrorKind::InvalidData,
                format!("Unsupported version: {version}, expected {VERSION}"),
            ));
        }

        let layer_count = reader.read_u8()?;

        // Reserved (6 bytes)
        let mut reserved = [0u8; 6];
        reader.read_exact(&mut reserved)?;

        // Bounding box
        let min_lat = reader.read_f32::<LittleEndian>()?;
        let min_lon = reader.read_f32::<LittleEndian>()?;
        let max_lat = reader.read_f32::<LittleEndian>()?;
        let max_lon = reader.read_f32::<LittleEndian>()?;

        // Counts and offsets
        let total_poi_count = reader.read_u64::<LittleEndian>()?;
        let dict_offset = reader.read_u64::<LittleEndian>()?;
        let dict_length = reader.read_u32::<LittleEndian>()?;
        let layer_dir_offset = reader.read_u64::<LittleEndian>()?;
        let layer_dir_length = reader.read_u32::<LittleEndian>()?;

        // Skip remaining reserved bytes
        reader.seek(SeekFrom::Start(HEADER_SIZE as u64))?;

        Ok(Header {
            version,
            layer_count,
            min_lat,
            min_lon,
            max_lat,
            max_lon,
            total_poi_count,
            dict_offset,
            dict_length,
            layer_dir_offset,
            layer_dir_length,
        })
    }

    /// Serialize header to bytes.
    pub fn to_bytes(&self) -> Vec<u8> {
        let mut buf = vec![0u8; HEADER_SIZE];
        buf[0..8].copy_from_slice(MAGIC);
        buf[8] = VERSION;
        buf[9] = self.layer_count;
        // bytes 10-15 are reserved (already zeroed)
        LittleEndian::write_f32(&mut buf[16..20], self.min_lat);
        LittleEndian::write_f32(&mut buf[20..24], self.min_lon);
        LittleEndian::write_f32(&mut buf[24..28], self.max_lat);
        LittleEndian::write_f32(&mut buf[28..32], self.max_lon);
        LittleEndian::write_u64(&mut buf[32..40], self.total_poi_count);
        LittleEndian::write_u64(&mut buf[40..48], self.dict_offset);
        LittleEndian::write_u32(&mut buf[48..52], self.dict_length);
        LittleEndian::write_u64(&mut buf[52..60], self.layer_dir_offset);
        LittleEndian::write_u32(&mut buf[60..64], self.layer_dir_length);
        // bytes 64..256 are reserved
        buf
    }
}

//! PTiles v7 file header (512 bytes).
//!
//! The header identifies the file format, version, bounding box, and
//! byte offsets to the dictionary, layer directory, spatial index, and data blocks.

use byteorder::{LittleEndian, ReadBytesExt};
use std::io::{self, Read};
use thiserror::Error;

/// Magic bytes for PTiles v7 files: "PTILES7\0"
pub const MAGIC: &[u8; 8] = b"PTILES7\x00";

/// Current version number.
pub const VERSION: u8 = 7;

/// Header size in bytes.
pub const HEADER_SIZE: usize = 512;

/// Error type for header parsing.
#[derive(Error, Debug)]
pub enum HeaderError {
    #[error("invalid magic bytes: expected 'PTILES7\\0', got {0:?}")]
    InvalidMagic([u8; 8]),

    #[error("unsupported version: {0} (expected {VERSION})")]
    UnsupportedVersion(u8),

    #[error("I/O error: {0}")]
    Io(#[from] io::Error),
}

/// PTiles v7 file header.
#[derive(Debug, Clone)]
pub struct Header {
    /// File version (must be 7).
    pub version: u8,

    /// Minimum latitude (southern bound).
    pub min_lat: f32,

    /// Minimum longitude (western bound).
    pub min_lon: f32,

    /// Maximum latitude (northern bound).
    pub max_lat: f32,

    /// Maximum longitude (eastern bound).
    pub max_lon: f32,

    /// Number of layers in the file.
    pub layer_count: u8,

    /// Total feature count across all layers.
    pub total_poi_count: u64,

    /// Byte offset to the zstd dictionary.
    pub dict_offset: u64,

    /// Dictionary size in bytes.
    pub dict_length: u32,

    /// Byte offset to the layer directory.
    pub layer_dir_offset: u64,

    /// Layer directory size in bytes.
    pub layer_dir_length: u32,

    /// Byte offset to the spatial index.
    pub index_offset: u64,

    /// Spatial index size in bytes.
    pub index_length: u32,

    /// Byte offset to the first data block.
    pub blocks_offset: u64,
}

impl Header {
    /// Parse a header from a reader.
    ///
    /// Reads exactly 512 bytes from the current position.
    pub fn read<R: Read>(reader: &mut R) -> Result<Self, HeaderError> {
        let mut magic = [0u8; 8];
        reader.read_exact(&mut magic)?;

        if &magic != MAGIC {
            return Err(HeaderError::InvalidMagic(magic));
        }

        let version = reader.read_u8()?;
        if version != VERSION {
            return Err(HeaderError::UnsupportedVersion(version));
        }

        // Skip 3 reserved bytes
        let mut reserved = [0u8; 3];
        reader.read_exact(&mut reserved)?;

        let min_lat = reader.read_f32::<LittleEndian>()?;
        let min_lon = reader.read_f32::<LittleEndian>()?;
        let max_lat = reader.read_f32::<LittleEndian>()?;
        let max_lon = reader.read_f32::<LittleEndian>()?;

        let layer_count = reader.read_u8()?;

        // Skip 3 reserved bytes
        let mut reserved2 = [0u8; 3];
        reader.read_exact(&mut reserved2)?;

        let total_poi_count = reader.read_u64::<LittleEndian>()?;
        let dict_offset = reader.read_u64::<LittleEndian>()?;
        let dict_length = reader.read_u32::<LittleEndian>()?;
        let layer_dir_offset = reader.read_u64::<LittleEndian>()?;
        let layer_dir_length = reader.read_u32::<LittleEndian>()?;
        let index_offset = reader.read_u64::<LittleEndian>()?;
        let index_length = reader.read_u32::<LittleEndian>()?;
        let blocks_offset = reader.read_u64::<LittleEndian>()?;

        // Skip remaining reserved bytes (428 bytes of padding to reach 512)
        // We've read: 8+1+3 + 4*4 + 1+3 + 8 + 8+4 + 8+4 + 8+4 + 8 = 84 bytes so far
        // 512 - 84 = 428 bytes remaining
        let mut reserved_tail = vec![0u8; HEADER_SIZE - 84];
        reader.read_exact(&mut reserved_tail)?;

        Ok(Header {
            version,
            min_lat,
            min_lon,
            max_lat,
            max_lon,
            layer_count,
            total_poi_count,
            dict_offset,
            dict_length,
            layer_dir_offset,
            layer_dir_length,
            index_offset,
            index_length,
            blocks_offset,
        })
    }

    /// Check if the given coordinates fall within the file's bounding box.
    pub fn contains_point(&self, lat: f32, lon: f32) -> bool {
        lat >= self.min_lat && lat <= self.max_lat && lon >= self.min_lon && lon <= self.max_lon
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::io::Cursor;

    #[test]
    fn test_read_header() {
        let mut buf = vec![0u8; HEADER_SIZE];
        buf[0..8].copy_from_slice(MAGIC);
        buf[8] = VERSION;
        // min_lat = 25.0, min_lon = -125.0, max_lat = 50.0, max_lon = -65.0
        buf[12..16].copy_from_slice(&25.0f32.to_le_bytes());
        buf[16..20].copy_from_slice(&(-125.0f32).to_le_bytes());
        buf[20..24].copy_from_slice(&50.0f32.to_le_bytes());
        buf[24..28].copy_from_slice(&(-65.0f32).to_le_bytes());
        buf[28] = 7; // layer_count
                       // total_poi_count
        buf[32..40].copy_from_slice(&100_000_000u64.to_le_bytes());
        // dict_offset @ 512
        buf[40..48].copy_from_slice(&512u64.to_le_bytes());
        // dict_length = 524288
        buf[48..52].copy_from_slice(&524288u32.to_le_bytes());
        // layer_dir_offset @ 524800
        buf[52..60].copy_from_slice(&524800u64.to_le_bytes());
        // layer_dir_length = 224
        buf[60..64].copy_from_slice(&224u32.to_le_bytes());
        // index_offset @ 525024
        buf[64..72].copy_from_slice(&525024u64.to_le_bytes());
        // index_length = 1000000
        buf[72..76].copy_from_slice(&1000000u32.to_le_bytes());
        // blocks_offset @ 1525024
        buf[76..84].copy_from_slice(&1525024u64.to_le_bytes());

        let mut cursor = Cursor::new(&buf);
        let header = Header::read(&mut cursor).unwrap();

        assert_eq!(header.version, 7);
        assert_eq!(header.min_lat, 25.0);
        assert_eq!(header.max_lat, 50.0);
        assert_eq!(header.layer_count, 7);
        assert_eq!(header.total_poi_count, 100_000_000);
        assert!(header.contains_point(40.0, -100.0));
        assert!(!header.contains_point(60.0, -100.0));
    }

    #[test]
    fn test_bad_magic() {
        let mut buf = vec![0u8; HEADER_SIZE];
        buf[0..8].copy_from_slice(b"BADMAGIC");
        let mut cursor = Cursor::new(&buf);
        let err = Header::read(&mut cursor).unwrap_err();
        assert!(matches!(err, HeaderError::InvalidMagic(_)));
    }

    #[test]
    fn test_bad_version() {
        let mut buf = vec![0u8; HEADER_SIZE];
        buf[0..8].copy_from_slice(MAGIC);
        buf[8] = 99;
        let mut cursor = Cursor::new(&buf);
        let err = Header::read(&mut cursor).unwrap_err();
        assert!(matches!(err, HeaderError::UnsupportedVersion(99)));
    }
}

//! Layer types and layer directory for PTiles v7.
//!
//! The layer directory maps each of the 7 layers to its geometry type,
//! feature count, and name.

use byteorder::{LittleEndian, ReadBytesExt};
use std::io::{self, Read};
use thiserror::Error;

/// Number of layers in the v7 format.
pub const LAYER_COUNT: usize = 7;

/// Size of a single layer directory entry in bytes.
pub const LAYER_DIR_ENTRY_SIZE: usize = 32;

/// Total size of the layer directory.
pub const LAYER_DIR_SIZE: usize = LAYER_COUNT * LAYER_DIR_ENTRY_SIZE;

/// The 7 layer types supported by PTiles v7.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash)]
#[repr(u8)]
pub enum LayerType {
    /// Building footprints (polygon)
    Buildings = 0,
    /// Road network (linestring)
    Roads = 1,
    /// Places / points of interest (point)
    Places = 2,
    /// Administrative boundaries (polygon)
    Admin = 3,
    /// Water bodies (polygon)
    Water = 4,
    /// Railway network (linestring)
    Rail = 5,
    /// Parks, forests, protected areas (polygon)
    Parks = 6,
}

impl LayerType {
    /// Create a LayerType from a u8.
    ///
    /// Returns `None` if the value is not in 0..=6.
    pub fn from_u8(v: u8) -> Option<Self> {
        match v {
            0 => Some(Self::Buildings),
            1 => Some(Self::Roads),
            2 => Some(Self::Places),
            3 => Some(Self::Admin),
            4 => Some(Self::Water),
            5 => Some(Self::Rail),
            6 => Some(Self::Parks),
            _ => None,
        }
    }

    /// Display name for the layer.
    pub fn name(&self) -> &'static str {
        match self {
            Self::Buildings => "buildings",
            Self::Roads => "roads",
            Self::Places => "places",
            Self::Admin => "admin",
            Self::Water => "water",
            Self::Rail => "rail",
            Self::Parks => "parks",
        }
    }
}

/// Geometry type for a layer.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
#[repr(u8)]
pub enum GeometryType {
    Point = 0,
    Linestring = 1,
    Polygon = 2,
}

impl GeometryType {
    pub fn from_u8(v: u8) -> Option<Self> {
        match v {
            0 => Some(Self::Point),
            1 => Some(Self::Linestring),
            2 => Some(Self::Polygon),
            _ => None,
        }
    }
}

/// A single entry in the layer directory.
#[derive(Debug, Clone)]
pub struct LayerDirEntry {
    pub layer_type: LayerType,
    pub geometry_type: GeometryType,
    pub index_count: u32,
    pub poi_count: u64,
    pub name: String,
}

/// The complete layer directory (7 entries).
#[derive(Debug, Clone)]
pub struct LayerDir {
    pub entries: Vec<LayerDirEntry>,
}

/// Error type for layer directory parsing.
#[derive(Error, Debug)]
pub enum LayerDirError {
    #[error("invalid layer type: {0}")]
    InvalidLayerType(u8),

    #[error("invalid geometry type: {0}")]
    InvalidGeometryType(u8),

    #[error("not enough entries: expected {LAYER_COUNT}, got {0}")]
    NotEnoughEntries(usize),

    #[error("I/O error: {0}")]
    Io(#[from] io::Error),
}

impl LayerDir {
    /// Parse a layer directory from a reader.
    ///
    /// Reads exactly `LAYER_DIR_SIZE` bytes (7 × 32 = 224 bytes).
    pub fn read<R: Read>(reader: &mut R) -> Result<Self, LayerDirError> {
        let mut entries = Vec::with_capacity(LAYER_COUNT);

        for _ in 0..LAYER_COUNT {
            let layer_type_byte = reader.read_u8()?;
            let geometry_type_byte = reader.read_u8()?;

            // Skip 2 reserved bytes
            let mut reserved = [0u8; 2];
            reader.read_exact(&mut reserved)?;

            let index_count = reader.read_u32::<LittleEndian>()?;
            let poi_count = reader.read_u64::<LittleEndian>()?;

            // Read 16-byte name field
            let mut name_bytes = [0u8; 16];
            reader.read_exact(&mut name_bytes)?;
            let name_len = name_bytes
                .iter()
                .position(|&b| b == 0)
                .unwrap_or(16);
            let name = String::from_utf8_lossy(&name_bytes[..name_len]).into_owned();

            let layer_type =
                LayerType::from_u8(layer_type_byte).ok_or(LayerDirError::InvalidLayerType(layer_type_byte))?;
            let geometry_type = GeometryType::from_u8(geometry_type_byte)
                .ok_or(LayerDirError::InvalidGeometryType(geometry_type_byte))?;

            entries.push(LayerDirEntry {
                layer_type,
                geometry_type,
                index_count,
                poi_count,
                name,
            });
        }

        Ok(LayerDir { entries })
    }

    /// Get the entry for a specific layer type.
    pub fn get(&self, layer: LayerType) -> Option<&LayerDirEntry> {
        self.entries.get(layer as usize)
    }

    /// Compute the byte offset into the concatenated spatial index for a given layer.
    ///
    /// Returns `(start_byte, end_byte)` where start is the byte offset of the first
    /// entry for this layer and end is one past the last entry.
    pub fn index_range(&self, layer: LayerType) -> Option<(u64, u64)> {
        let mut offset: u64 = 0;
        for entry in &self.entries {
            if entry.layer_type == layer {
                let size = entry.index_count as u64 * 20;
                return Some((offset, offset + size));
            }
            offset += entry.index_count as u64 * 20;
        }
        None
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::io::Cursor;

    #[test]
    fn test_layer_type_roundtrip() {
        for i in 0..=6u8 {
            let lt = LayerType::from_u8(i).unwrap();
            assert_eq!(lt as u8, i);
        }
        assert!(LayerType::from_u8(7).is_none());
        assert!(LayerType::from_u8(255).is_none());
    }

    #[test]
    fn test_read_layer_dir() {
        // Build a minimal 224-byte layer directory
        let mut buf = Vec::with_capacity(LAYER_DIR_SIZE);
        let layer_names = [
            "buildings\0\0\0\0\0\0\0",
            "roads\0\0\0\0\0\0\0\0\0\0\0",
            "places\0\0\0\0\0\0\0\0\0\0",
            "admin\0\0\0\0\0\0\0\0\0\0\0",
            "water\0\0\0\0\0\0\0\0\0\0\0",
            "rail\0\0\0\0\0\0\0\0\0\0\0\0",
            "parks\0\0\0\0\0\0\0\0\0\0\0",
        ];
        let geometry_types: [u8; 7] = [2, 1, 0, 2, 2, 1, 2];
        let index_counts: [u32; 7] = [100, 200, 50, 10, 30, 20, 40];

        for i in 0..LAYER_COUNT {
            buf.push(i as u8); // layer_type
            buf.push(geometry_types[i]); // geometry_type
            buf.extend_from_slice(&[0u8; 2]); // reserved
            buf.extend_from_slice(&index_counts[i].to_le_bytes());
            buf.extend_from_slice(&1000u64.to_le_bytes()); // poi_count
            let name = layer_names[i].as_bytes();
            let mut name_field = [0u8; 16];
            name_field[..name.len()].copy_from_slice(name);
            buf.extend_from_slice(&name_field);
        }

        let mut cursor = Cursor::new(&buf);
        let dir = LayerDir::read(&mut cursor).unwrap();

        assert_eq!(dir.entries.len(), 7);
        assert_eq!(dir.entries[0].layer_type, LayerType::Buildings);
        assert_eq!(dir.entries[0].geometry_type, GeometryType::Polygon);
        assert_eq!(dir.entries[0].index_count, 100);
        assert_eq!(dir.entries[0].name, "buildings");

        assert_eq!(dir.entries[1].layer_type, LayerType::Roads);
        assert_eq!(dir.entries[1].geometry_type, GeometryType::Linestring);
    }
}

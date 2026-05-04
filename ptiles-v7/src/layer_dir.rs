/// Layer Directory
///
/// Lists all layers present in a v7 file, each with their own spatial index
/// and data block ranges.
///
/// Binary layout:
///   layer_count: uint8 (1 byte)
///   reserved: 3 bytes
///   For each layer:
///     layer_type: uint8 (enum)
///     name_len: uint8
///     name: UTF-8 (variable)
///     index_offset: uint64 (8 bytes)
///     index_length: uint32 (4 bytes)
///     blocks_offset: uint64 (8 bytes)
///     poi_count: uint64 (8 bytes)

use byteorder::{LittleEndian, ReadBytesExt};
use std::io::{Cursor, Read};

/// Layer type enum (matching layer order in the file).
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
#[repr(u8)]
pub enum LayerType {
    Buildings = 0,
    Roads = 1,
    Places = 2,
    Admin = 3,
    Water = 4,
    Rail = 5,
    Parks = 6,
    // 7-255 reserved
}

impl LayerType {
    pub fn from_u8(v: u8) -> Option<Self> {
        match v {
            0 => Some(LayerType::Buildings),
            1 => Some(LayerType::Roads),
            2 => Some(LayerType::Places),
            3 => Some(LayerType::Admin),
            4 => Some(LayerType::Water),
            5 => Some(LayerType::Rail),
            6 => Some(LayerType::Parks),
            _ => None,
        }
    }
}

/// A single layer entry in the directory.
#[derive(Debug, Clone)]
pub struct LayerEntry {
    pub layer_type: LayerType,
    pub name: String,
    pub index_offset: u64,
    pub index_length: u32,
    pub blocks_offset: u64,
    pub poi_count: u64,
}

/// The full layer directory.
#[derive(Debug, Clone)]
pub struct LayerDirectory {
    pub layer_count: u8,
    pub layers: Vec<LayerEntry>,
}

impl LayerDirectory {
    /// Decode layer directory from raw bytes.
    pub fn decode(data: &[u8]) -> Self {
        let mut cursor = Cursor::new(data);
        let layer_count = cursor.read_u8().unwrap_or(0);

        // Skip 3 reserved bytes
        let mut reserved = [0u8; 3];
        let _ = cursor.read(&mut reserved);

        let mut layers = Vec::with_capacity(layer_count as usize);

        for _ in 0..layer_count {
            let type_byte = cursor.read_u8().unwrap_or(0);
            let name_len = cursor.read_u8().unwrap_or(0) as usize;

            let mut name_bytes = vec![0u8; name_len];
            let _ = cursor.read(&mut name_bytes);
            let name = String::from_utf8_lossy(&name_bytes).to_string();

            let index_offset = cursor.read_u64::<LittleEndian>().unwrap_or(0);
            let index_length = cursor.read_u32::<LittleEndian>().unwrap_or(0);
            let blocks_offset = cursor.read_u64::<LittleEndian>().unwrap_or(0);
            let poi_count = cursor.read_u64::<LittleEndian>().unwrap_or(0);

            if let Some(layer_type) = LayerType::from_u8(type_byte) {
                layers.push(LayerEntry {
                    layer_type,
                    name,
                    index_offset,
                    index_length,
                    blocks_offset,
                    poi_count,
                });
            }
        }

        LayerDirectory {
            layer_count,
            layers,
        }
    }

    /// Encode layer directory to bytes.
    pub fn encode(&self) -> Vec<u8> {
        let mut buf = Vec::new();
        buf.push(self.layer_count);
        buf.extend_from_slice(&[0u8; 3]); // reserved

        for layer in &self.layers {
            buf.push(layer.layer_type as u8);
            let name = layer.name.as_bytes();
            buf.push(name.len() as u8);
            buf.extend_from_slice(name);
            buf.extend_from_slice(&layer.index_offset.to_le_bytes());
            buf.extend_from_slice(&layer.index_length.to_le_bytes());
            buf.extend_from_slice(&layer.blocks_offset.to_le_bytes());
            buf.extend_from_slice(&layer.poi_count.to_le_bytes());
        }

        buf
    }

    /// Find a layer by type.
    pub fn find(&self, layer_type: LayerType) -> Option<&LayerEntry> {
        self.layers.iter().find(|l| l.layer_type == layer_type)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_layer_dir_roundtrip() {
        let dir = LayerDirectory {
            layer_count: 2,
            layers: vec![
                LayerEntry {
                    layer_type: LayerType::Buildings,
                    name: "buildings".to_string(),
                    index_offset: 1000,
                    index_length: 500,
                    blocks_offset: 2000,
                    poi_count: 1000000,
                },
                LayerEntry {
                    layer_type: LayerType::Roads,
                    name: "roads".to_string(),
                    index_offset: 2500,
                    index_length: 300,
                    blocks_offset: 3000,
                    poi_count: 500000,
                },
            ],
        };

        let encoded = dir.encode();
        let decoded = LayerDirectory::decode(&encoded);

        assert_eq!(decoded.layer_count, 2);
        assert_eq!(decoded.layers.len(), 2);
        assert_eq!(decoded.layers[0].layer_type, LayerType::Buildings);
        assert_eq!(decoded.layers[0].name, "buildings");
        assert_eq!(decoded.layers[0].index_offset, 1000);
        assert_eq!(decoded.layers[1].layer_type, LayerType::Roads);
        assert_eq!(decoded.layers[1].poi_count, 500000);
    }
}

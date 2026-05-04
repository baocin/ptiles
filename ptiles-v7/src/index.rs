//! Spatial index for PTiles v7.
//!
//! The spatial index maps H3 cell IDs (resolution 7) to compressed data block
//! offsets. Each entry is 20 bytes: layer_type (1) + h3_cell (8) + block_offset (6)
//! + block_length (3) + poi_count (2).
//!
//! All layer indices are concatenated into a single linear index. The layer directory
//! provides the entry count per layer so readers know where each layer's entries
//! begin and end.

use byteorder::{LittleEndian, ReadBytesExt};
use std::io::{self, Read};
use thiserror::Error;

use crate::layer::LayerType;

/// Size of a single index entry in bytes.
pub const INDEX_ENTRY_SIZE: usize = 20;

/// A single spatial index entry.
#[derive(Debug, Clone, Copy)]
pub struct IndexEntry {
    /// Layer type this entry belongs to.
    pub layer_type: LayerType,

    /// H3 cell index (resolution 7, as integer).
    pub h3_cell: u64,

    /// Absolute byte offset to the compressed data block.
    pub block_offset: u64,

    /// Compressed block size in bytes.
    pub block_length: u32,

    /// Number of features in this block.
    pub poi_count: u16,
}

/// Error type for index parsing.
#[derive(Error, Debug)]
pub enum IndexError {
    #[error("invalid layer type in index entry: {0}")]
    InvalidLayerType(u8),

    #[error("I/O error: {0}")]
    Io(#[from] io::Error),
}

/// The spatial index: a sorted list of entries.
///
/// Entries are sorted by (layer_type, h3_cell). Binary search is used for lookups.
#[derive(Debug, Clone)]
pub struct SpatialIndex {
    pub entries: Vec<IndexEntry>,
}

impl SpatialIndex {
    /// Parse the spatial index from a reader.
    ///
    /// `entry_count` is the total number of entries across all layers.
    pub fn read<R: Read>(reader: &mut R, entry_count: usize) -> Result<Self, IndexError> {
        let mut entries = Vec::with_capacity(entry_count);

        for _ in 0..entry_count {
            let layer_byte = reader.read_u8()?;
            let h3_cell = reader.read_u64::<LittleEndian>()?;

            // Read 6-byte block offset (little-endian)
            let mut off_bytes = [0u8; 8];
            reader.read_exact(&mut off_bytes[..6])?;
            let block_offset =
                off_bytes[0] as u64
                    | (off_bytes[1] as u64) << 8
                    | (off_bytes[2] as u64) << 16
                    | (off_bytes[3] as u64) << 24
                    | (off_bytes[4] as u64) << 32
                    | (off_bytes[5] as u64) << 40;

            // Read 3-byte block length (little-endian)
            let mut len_bytes = [0u8; 4];
            reader.read_exact(&mut len_bytes[..3])?;
            let block_length = len_bytes[0] as u32
                | (len_bytes[1] as u32) << 8
                | (len_bytes[2] as u32) << 16;

            let poi_count = reader.read_u16::<LittleEndian>()?;

            let layer_type =
                LayerType::from_u8(layer_byte).ok_or(IndexError::InvalidLayerType(layer_byte))?;

            entries.push(IndexEntry {
                layer_type,
                h3_cell,
                block_offset,
                block_length,
                poi_count,
            });
        }

        Ok(SpatialIndex { entries })
    }

    /// Build a combined key for binary search: `(layer_type << 64) | h3_cell`.
    fn key(layer: LayerType, h3: u64) -> u128 {
        ((layer as u128) << 64) | (h3 as u128)
    }

    fn entry_key(e: &IndexEntry) -> u128 {
        Self::key(e.layer_type, e.h3_cell)
    }

    /// Find an index entry for a given layer and H3 cell via binary search.
    pub fn find(&self, layer: LayerType, h3_cell: u64) -> Option<IndexEntry> {
        let target = Self::key(layer, h3_cell);

        let mut left: usize = 0;
        let mut right: isize = self.entries.len() as isize - 1;

        while left as isize <= right {
            let mid = (left + right as usize) / 2;
            let mid_key = Self::entry_key(&self.entries[mid]);

            #[allow(clippy::comparison_chain)]
            if mid_key == target {
                return Some(self.entries[mid]);
            } else if mid_key < target {
                left = mid + 1;
            } else {
                right = mid as isize - 1;
            }
        }

        None
    }

    /// Get all index entries for a single layer, in order.
    pub fn entries_for_layer(&self, layer: LayerType) -> Vec<&IndexEntry> {
        self.entries
            .iter()
            .filter(|e| e.layer_type == layer)
            .collect()
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::io::Cursor;

    #[test]
    fn test_read_index() {
        // Build a small index with 2 entries
        let mut buf = Vec::new();

        // Entry 0: layer 0 (buildings), h3=0x0870c00051ffffff, off=1000, len=255, pois=10
        buf.push(0u8); // layer_type
        buf.extend_from_slice(&0x0870c00051ffffffu64.to_le_bytes());
        buf.extend_from_slice(&1000u64.to_le_bytes()[..6]); // 6-byte offset
        buf.extend_from_slice(&255u32.to_le_bytes()[..3]); // 3-byte length
        buf.extend_from_slice(&10u16.to_le_bytes());

        // Entry 1: layer 0 (buildings), h3=0x0870c00055ffffff, off=1255, len=120, pois=5
        buf.push(0u8);
        buf.extend_from_slice(&0x0870c00055ffffffu64.to_le_bytes());
        buf.extend_from_slice(&1255u64.to_le_bytes()[..6]);
        buf.extend_from_slice(&120u32.to_le_bytes()[..3]);
        buf.extend_from_slice(&5u16.to_le_bytes());

        let mut cursor = Cursor::new(&buf);
        let index = SpatialIndex::read(&mut cursor, 2).unwrap();

        assert_eq!(index.entries.len(), 2);

        // Binary search
        let found = index.find(LayerType::Buildings, 0x0870c00051ffffff);
        assert!(found.is_some());
        let entry = found.unwrap();
        assert_eq!(entry.block_offset, 1000);
        assert_eq!(entry.block_length, 255);
        assert_eq!(entry.poi_count, 10);

        // Not found
        assert!(index.find(LayerType::Buildings, 0x999999).is_none());
        assert!(index.find(LayerType::Roads, 0x0870c00051ffffff).is_none());
    }

    #[test]
    fn test_binary_search_midpoint() {
        // Test binary search correctness with sequential entries
        let mut buf = Vec::new();
        for i in 0u64..10u64 {
            buf.push(0u8); // buildings layer
            buf.extend_from_slice(&(i * 1000).to_le_bytes());
            buf.extend_from_slice(&1000u64.to_le_bytes()[..6]);
            buf.extend_from_slice(&50u32.to_le_bytes()[..3]);
            buf.extend_from_slice(&10u16.to_le_bytes());
        }

        let mut cursor = Cursor::new(&buf);
        let index = SpatialIndex::read(&mut cursor, 10).unwrap();

        // Find each entry
        for i in 0..10 {
            let h3 = i * 1000;
            let entry = index.find(LayerType::Buildings, h3);
            assert!(
                entry.is_some(),
                "entry {i} with h3={h3:016x} not found"
            );
            assert_eq!(entry.unwrap().h3_cell, h3);
        }
    }
}

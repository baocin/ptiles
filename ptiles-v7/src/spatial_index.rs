/// Spatial Index (per layer)
///
/// H3 resolution 7 cells sorted by H3 cell ID for binary search.
/// Shared binary format with v6 (19 bytes per entry).
///
/// Layout:
///   entry_count (4 bytes, uint32)
///   For each entry:
///     h3_cell (8 bytes, uint64) - H3 index as integer
///     block_offset (6 bytes) - Absolute byte offset to data block
///     block_length (3 bytes) - Compressed block size
///     poi_count (2 bytes, uint16) - Features in this cell

use byteorder::{LittleEndian, ReadBytesExt};

pub const ENTRY_SIZE: usize = 19; // 8 + 6 + 3 + 2

#[derive(Debug, Clone)]
pub struct IndexEntry {
    pub h3_cell: u64,
    pub block_offset: u64,
    pub block_length: u32,
    pub poi_count: u16,
}

#[derive(Debug, Clone)]
pub struct SpatialIndex {
    pub entry_count: u32,
    pub entries: Vec<IndexEntry>,
}

impl SpatialIndex {
    /// Read spatial index from a reader at its current position.
    pub fn read<R: std::io::Read>(reader: &mut R) -> std::io::Result<Self> {
        let entry_count = reader.read_u32::<LittleEndian>()?;
        let mut entries = Vec::with_capacity(entry_count as usize);

        for _ in 0..entry_count {
            let h3_cell = reader.read_u64::<LittleEndian>()?;

            // Read 6-byte block_offset
            let mut offset_bytes = [0u8; 8];
            reader.read_exact(&mut offset_bytes[..6])?;
            let block_offset = u64::from_le_bytes(offset_bytes);

            // Read 3-byte block_length
            let mut len_bytes = [0u8; 4];
            reader.read_exact(&mut len_bytes[..3])?;
            let block_length = u32::from_le_bytes(len_bytes);

            let poi_count = reader.read_u16::<LittleEndian>()?;

            entries.push(IndexEntry {
                h3_cell,
                block_offset,
                block_length,
                poi_count,
            });
        }

        Ok(SpatialIndex {
            entry_count,
            entries,
        })
    }

    /// Binary search for an H3 cell in the index.
    pub fn find_cell(&self, h3_cell: u64) -> Option<&IndexEntry> {
        self.entries
            .binary_search_by_key(&h3_cell, |e| e.h3_cell)
            .ok()
            .map(|idx| &self.entries[idx])
    }

    /// Find all cells that intersect a bounding box (brute-force scan).
    /// For production use, a proper H3 polygon fill should be used instead.
    pub fn find_in_bounds(
        &self,
    ) -> impl Iterator<Item = &IndexEntry> {
        self.entries.iter()
    }
}

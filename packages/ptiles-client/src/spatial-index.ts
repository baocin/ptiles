// src/spatial-index.ts — spatial index parsing

import { BinaryReader } from './binary-reader.js';

export interface IndexEntry {
  h3_cell: bigint;
  block_offset: number;
  block_length: number;
  feature_count: number;
}

/**
 * Parse the spatial index from a buffer.
 * Format:
 *   u32 entry_count
 *   for each entry:
 *     u64  h3_cell
 *     u48  block_offset
 *     u24  block_length
 *     u16  feature_count
 */
export function parseIndex(buffer: Uint8Array): IndexEntry[] {
  const reader = new BinaryReader(buffer);
  const count = reader.readU32();
  const entries: IndexEntry[] = [];

  for (let i = 0; i < count; i++) {
    const h3Low = reader.readU32();
    const h3High = reader.readU32();
    const h3_cell = (BigInt(h3High) << 32n) | BigInt(h3Low);
    const block_offset = reader.readU48();
    const block_length = reader.readU24();
    const feature_count = reader.readU16();

    entries.push({
      h3_cell,
      block_offset,
      block_length,
      feature_count,
    });
  }

  return entries;
}

/**
 * Detect whether block offsets are relative or absolute.
 * Rule: relative = (first_entry.block_offset < header.blocks_offset) or entries is empty
 */
export function detectRelativeOffsets(
  entries: IndexEntry[],
  blocksOffset: number
): boolean {
  if (entries.length === 0) return true;
  return entries[0].block_offset < blocksOffset;
}

/**
 * Look up an H3 cell in the sorted index via binary search.
 * Returns the entry or null.
 */
export function lookupCell(
  entries: IndexEntry[],
  cell: bigint
): IndexEntry | null {
  let lo = 0;
  let hi = entries.length - 1;

  while (lo <= hi) {
    const mid = (lo + hi) >>> 1;
    const midCell = entries[mid].h3_cell;
    if (midCell === cell) {
      return entries[mid];
    } else if (midCell < cell) {
      lo = mid + 1;
    } else {
      hi = mid - 1;
    }
  }

  return null;
}

/**
 * Resolve the absolute file position for a block entry.
 */
export function resolveBlockOffset(
  entry: IndexEntry,
  blocksOffset: number,
  relative: boolean
): number {
  if (relative) {
    return blocksOffset + entry.block_offset;
  }
  return entry.block_offset;
}

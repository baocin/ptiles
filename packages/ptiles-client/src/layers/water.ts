// src/layers/water.ts — WaterReader stub

import { readFileSync } from 'fs';
import { parseHeader } from '../header.js';
import { parseIndex, detectRelativeOffsets } from '../spatial-index.js';
import type { WaterFeature, LargeWaterBody, Header } from '../types.js';

export class WaterReader {
  readonly header: Header;
  readonly index: ReturnType<typeof parseIndex>;
  readonly relativeOffsets: boolean;
  private data: Uint8Array;

  constructor(data: Uint8Array) {
    this.data = data;
    this.header = parseHeader(data);

    const indexBuf = data.slice(this.header.index_offset, this.header.index_offset + this.header.index_length);
    this.index = parseIndex(indexBuf);
    this.relativeOffsets = detectRelativeOffsets(this.index, this.header.blocks_offset);
  }

  static open(path: string): WaterReader {
    const data = readFileSync(path);
    return new WaterReader(new Uint8Array(data.buffer, data.byteOffset, data.byteLength));
  }

  /** Get large water body features from the aux section. */
  largeWaterBodies(): LargeWaterBody[] {
    // TODO: parse aux section
    return [];
  }

  /** Get water features within bounds (stub). */
  getInBounds(minLat: number, minLon: number, maxLat: number, maxLon: number, limit?: number): WaterFeature[] {
    // TODO: decompress blocks, decode records
    return [];
  }

  close(): void {}
}

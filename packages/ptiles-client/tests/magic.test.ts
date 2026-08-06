// tests/magic.test.ts — layer identification, including the Admin/Address
// magic collision in files published before the PTILESD fix.
//
// Headers are synthesized rather than read from disk: the behaviour under test
// is entirely determined by the first 8 bytes plus the filename, and building
// them here keeps the test runnable without a tile set.

import { describe, test, expect } from 'vitest';
import { parseHeader, MAGIC_TO_FORMAT, FORMAT_TO_MAGIC, FORMAT_SUFFIX } from '../src/header.js';

/** A minimal but structurally valid 256-byte header carrying `magic`. */
function header(magic: string, version = 1): Uint8Array {
  const buf = new Uint8Array(256);
  for (let i = 0; i < 7; i++) buf[i] = magic.charCodeAt(i);
  buf[7] = 0;
  buf[8] = version;
  return buf;
}

describe('every published layer is identifiable', () => {
  // Read back from the v4-20260711 tile set. A magic missing from the map
  // makes parseHeader throw, i.e. the client cannot open that layer at all.
  const shipped: [string, string][] = [
    ['PTILESF', 'Buildings'],
    ['PTILESB', 'Business'],
    ['PTILESX', 'BusinessNameIndex'],
    ['PTILESR', 'Roads'],
    ['PTILESW', 'Water'],
    ['PTILESP', 'Places'],
    ['PTILESN', 'Parks'],
    ['PTILEST', 'Rail'],
    ['PTILESA', 'Admin'],
    ['PTILESD', 'Address'],
    ['PTILESC', 'Camera'],
    ['PTILESS', 'Signals'],
  ];

  test.each(shipped)('%s parses as %s', (magic, format) => {
    expect(MAGIC_TO_FORMAT[magic]).toBe(format);
    expect(parseHeader(header(magic)).format).toBe(format);
  });

  test('magic and format maps agree in both directions', () => {
    for (const [magic, format] of Object.entries(MAGIC_TO_FORMAT)) {
      expect(FORMAT_TO_MAGIC[format]).toBe(magic);
    }
  });

  test('every format has at least one filename suffix', () => {
    for (const format of Object.values(MAGIC_TO_FORMAT)) {
      expect(FORMAT_SUFFIX[format]?.length ?? 0).toBeGreaterThan(0);
    }
  });

  test('an unrecognised magic still throws, with the bytes shown', () => {
    expect(() => parseHeader(header('PTILESZ'))).toThrow(/Unknown PTILES magic: PTILESZ/);
  });
});

describe('Address files built after the PTILESD fix', () => {
  test('identify from magic alone, no filename needed', () => {
    const h = parseHeader(header('PTILESD'));
    expect(h.format).toBe('Address');
    expect(h.magic).toBe('PTILESD');
    expect(h.legacy_magic).toBe(false);
    expect(h.format_ambiguous).toBe(false);
  });

  test('are not mistaken for Admin', () => {
    expect(parseHeader(header('PTILESD'), 'TN.address_v1.ptiles').format).toBe('Address');
  });
});

describe('Address files built before the fix (malformed, carrying PTILESA)', () => {
  // These shipped through v4-20260711 and are byte-identical to an Admin file
  // in their magic, because a nine-byte PTILESA2 was truncated to seven.
  const legacy = header('PTILESA');

  test.each([
    'TN.address_v1.ptiles',
    'CA.address.ptiles',
    '/mnt/core/kino/ptiles/data/v4/states/WY.address_v1.ptiles',
    'https://maps.mydatatimeline.com/maps/v4-20260711/TX.address_v1.ptiles',
  ])('recovered as Address from the filename: %s', (source) => {
    const h = parseHeader(legacy, source);
    expect(h.format).toBe('Address');
    expect(h.magic).toBe('PTILESA');
    expect(h.legacy_magic).toBe(true);
    expect(h.format_ambiguous).toBe(false);
  });

  test('a real Admin file is left alone', () => {
    const h = parseHeader(legacy, 'US.admin.ptiles');
    expect(h.format).toBe('Admin');
    expect(h.legacy_magic).toBe(false);
  });

  test('without a filename it reports Admin but flags the ambiguity', () => {
    const h = parseHeader(legacy);
    expect(h.format).toBe('Admin');
    expect(h.format_ambiguous).toBe(true);
  });

  test('the filename never promotes a non-PTILESA file to Address', () => {
    // A misnamed buildings file must stay Buildings; the override only ever
    // narrows the one genuinely ambiguous magic.
    const h = parseHeader(header('PTILESF'), 'TN.address_v1.ptiles');
    expect(h.format).toBe('Buildings');
    expect(h.legacy_magic).toBe(false);
  });
});

describe('header fields still decode', () => {
  test('version is read for both address spellings', () => {
    expect(parseHeader(header('PTILESD', 1)).version).toBe(1);
    expect(parseHeader(header('PTILESA', 1), 'TN.address_v1.ptiles').version).toBe(1);
  });
});

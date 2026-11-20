#!/usr/bin/env python3
"""Write LORIS EEG browser chunks for BrainVision (.vhdr) and BDF recordings, which LORIS-MRI's chunker lacks.

Usage: ephys_chunks.py FILE --destination DIR [--chunk-size N]; writes DIR/<stem>.chunks like edf-to-chunks.
Channels are read and written one at a time (index.json is merged per channel) so memory stays at one channel.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import mne
from loris_ephys_chunker.chunking import chunk_dir_path, write_chunk_directory

READERS = {'.vhdr': mne.io.read_raw_brainvision, '.bdf': mne.io.read_raw_bdf}


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument('file', type=Path)
    parser.add_argument('--destination', '-d', type=Path, required=True)
    parser.add_argument('--chunk-size', '-s', type=int, default=5000)
    args = parser.parse_args(argv)
    reader = READERS.get(args.file.suffix.lower())
    if reader is None:
        parser.error(f'unsupported recording {args.file.name}; expected {", ".join(READERS)}')
    mne.set_log_level('ERROR')
    names = reader(args.file, preload=False).ch_names
    for index, name in enumerate(names):
        print(f'channel {index + 1}/{len(names)}: {name}', flush=True)
        write_chunk_directory(path=args.file, chunk_size=args.chunk_size,
                              loader=lambda path, name=name: reader(path, preload=False).pick([name]),
                              from_channel_index=index, from_channel_name=name, channel_count=1,
                              destination=args.destination)
    # Per-channel writes leave a channel count of 1 in the shapes; record the full count like a single pass would.
    index_path = chunk_dir_path(args.file, destination=args.destination) / 'index.json'
    index = json.loads(index_path.read_text())
    index['shapes'] = [[len(names), *shape[1:]] for shape in index['shapes']]
    index_path.write_text(json.dumps(index, indent=2))
    return 0


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))

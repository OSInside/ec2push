"""
Information about the local disk image file to upload
"""
import errno
import lzma
import math
import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO, Iterator, NamedTuple, Optional, Tuple

from ec2push.exceptions import EC2PushImageError

GiB = 1024 ** 3

# Formats which look like a disk image but can't be dumped as is
UNSUPPORTED_SUFFIXES = (
    '.tar', '.tgz', '.tbz', '.gz', '.bz2', '.zst', '.zip',
    '.qcow2', '.vmdk', '.vhd', '.vhdx', '.vhdfixed', '.iso',
)


class ImageBlock(NamedTuple):
    """
    A block of the raw disk image

    index is the number of the block, data its content padded with
    zeros to the block size and position the number of bytes of the
    image file read so far, the compressed bytes for xz compressed
    images.
    """
    index: int
    data: bytes
    position: int


@dataclass
class ImageFile:
    """
    A raw disk image, optionally xz compressed

    upload_size is the size of the image file, raw_size the number
    of bytes written to the root volume of the AMI. The raw_size of
    an xz compressed image is None if it can't be determined from
    the xz index.
    """
    path: Path
    upload_size: int
    raw_size: Optional[int]
    xz_compressed: bool

    @property
    def raw_size_gib(self) -> Optional[int]:
        """
        Size of the raw disk image in GiB, rounded up
        """
        if self.raw_size is None:
            return None
        return max(1, math.ceil(self.raw_size / GiB))

    def read_blocks(
        self, block_size: int, max_size: int
    ) -> Iterator[ImageBlock]:
        """
        Read the raw disk image in blocks of the given size

        xz compressed images are decompressed on the fly. Holes of
        sparse raw images are skipped, they read as zeros anyway.
        Raises EC2PushImageError if the raw disk image is larger
        than max_size bytes.
        """
        if self.xz_compressed:
            blocks = self._read_xz_blocks(block_size)
        else:
            blocks = self._read_raw_blocks(block_size)
        for index, data, position in blocks:
            if index * block_size + len(data) > max_size:
                raise EC2PushImageError(
                    f'The raw disk image {self.path} is larger than '
                    f'{max_size} bytes'
                )
            if len(data) < block_size:
                data += bytes(block_size - len(data))
            yield ImageBlock(index, data, position)

    def _read_raw_blocks(self, block_size: int) -> Iterator[ImageBlock]:
        with open(self.path, 'rb') as raw:
            next_index = 0
            for start, end in data_ranges(raw, self.upload_size):
                first = max(start // block_size, next_index)
                next_index = math.ceil(end / block_size)
                for index in range(first, next_index):
                    raw.seek(index * block_size)
                    data = raw.read(block_size)
                    if not data:
                        return
                    yield ImageBlock(index, data, raw.tell())

    def _read_xz_blocks(self, block_size: int) -> Iterator[ImageBlock]:
        with open(self.path, 'rb') as compressed:
            try:
                with lzma.LZMAFile(compressed) as raw:
                    index = 0
                    while True:
                        data = raw.read(block_size)
                        if not data:
                            return
                        yield ImageBlock(index, data, compressed.tell())
                        index += 1
            except (lzma.LZMAError, EOFError) as issue:
                raise EC2PushImageError(
                    f'Unable to decompress {self.path}: {issue}'
                )


def read_image_file(path: Path) -> ImageFile:
    """
    Provide the ImageFile information of the given path
    """
    if not path.is_file():
        raise EC2PushImageError(f'Image file {path} not found')
    if path.suffix.lower() in UNSUPPORTED_SUFFIXES:
        raise EC2PushImageError(
            f'Unsupported image format {path.suffix}, expected a raw '
            'disk image, optionally xz compressed'
        )
    upload_size = path.stat().st_size
    if upload_size == 0:
        raise EC2PushImageError(f'Image file {path} is empty')
    if path.suffix.lower() == '.xz':
        return ImageFile(
            path=path,
            upload_size=upload_size,
            raw_size=read_xz_uncompressed_size(path),
            xz_compressed=True
        )
    return ImageFile(
        path=path,
        upload_size=upload_size,
        raw_size=upload_size,
        xz_compressed=False
    )


def data_ranges(file: BinaryIO, size: int) -> Iterator[Tuple[int, int]]:
    """
    Provide the (start, end) offsets of the ranges of the given file
    which hold data

    Holes of sparse files are skipped. If the platform or filesystem
    can't tell where the holes are, the whole file is one range.
    """
    if not hasattr(os, 'SEEK_DATA'):
        yield 0, size
        return
    descriptor = file.fileno()
    offset = 0
    while offset < size:
        try:
            start = os.lseek(descriptor, offset, os.SEEK_DATA)
        except OSError as issue:
            if issue.errno == errno.ENXIO:
                # Nothing but a hole up to the end of the file
                return
            yield offset, size
            return
        end = min(os.lseek(descriptor, start, os.SEEK_HOLE), size)
        if start >= end:
            return
        yield start, end
        offset = end


def read_xz_uncompressed_size(path: Path) -> Optional[int]:
    """
    Read the uncompressed size of the given xz file from its index

    Requires the xz tool. Returns None if xz is not available or
    the index can't be read.
    """
    xz = shutil.which('xz')
    if not xz:
        return None
    result = subprocess.run(
        [xz, '--robot', '--list', str(path)],
        capture_output=True, text=True
    )
    if result.returncode != 0:
        return None
    for line in result.stdout.splitlines():
        # totals <streams> <blocks> <compressed> <uncompressed> ...
        fields = line.split('\t')
        if fields[0] == 'totals' and len(fields) > 4:
            try:
                return int(fields[4])
            except ValueError:
                return None
    return None

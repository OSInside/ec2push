"""
Information about the local disk image file to upload
"""
import math
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from ec2push.exceptions import EC2PushImageError

GiB = 1024 ** 3

# Formats which look like a disk image but can't be dumped as is
UNSUPPORTED_SUFFIXES = (
    '.tar', '.tgz', '.tbz', '.gz', '.bz2', '.zst', '.zip',
    '.qcow2', '.vmdk', '.vhd', '.vhdx', '.vhdfixed', '.iso',
)


@dataclass
class ImageFile:
    """
    A raw disk image, optionally xz compressed

    upload_size is the number of bytes transferred to the helper
    instance, raw_size the number of bytes dumped to the target
    volume. The raw_size of an xz compressed image is None if it
    can't be determined from the xz index.
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

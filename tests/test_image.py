import lzma
import os
import shutil

import pytest

from ec2push.exceptions import EC2PushImageError
from ec2push.image import read_image_file


def test_raw_image(raw_image):
    image = read_image_file(raw_image)
    assert not image.xz_compressed
    assert image.upload_size == image.raw_size == 3 * 1024 ** 3 + 1
    assert image.raw_size_gib == 4


@pytest.mark.skipif(not shutil.which('xz'), reason='requires xz')
def test_xz_image(tmp_path):
    path = tmp_path / 'image.raw.xz'
    with lzma.open(path, 'wb') as xz:
        xz.write(b'\0' * 4096)
    image = read_image_file(path)
    assert image.xz_compressed
    assert image.upload_size == path.stat().st_size
    assert image.raw_size == 4096
    assert image.raw_size_gib == 1


def test_xz_image_without_xz_tool(tmp_path, monkeypatch):
    path = tmp_path / 'image.raw.xz'
    with lzma.open(path, 'wb') as xz:
        xz.write(b'\0' * 4096)
    monkeypatch.setattr(shutil, 'which', lambda name: None)
    image = read_image_file(path)
    assert image.raw_size is None
    assert image.raw_size_gib is None


@pytest.mark.parametrize('name', ['image.tar.gz', 'image.qcow2'])
def test_unsupported_format(tmp_path, name):
    path = tmp_path / name
    path.write_bytes(b'data')
    with pytest.raises(EC2PushImageError, match='Unsupported'):
        read_image_file(path)


def test_missing_image(tmp_path):
    with pytest.raises(EC2PushImageError, match='not found'):
        read_image_file(tmp_path / 'missing.raw')


def test_empty_image(tmp_path):
    path = tmp_path / 'empty.raw'
    path.touch()
    with pytest.raises(EC2PushImageError, match='empty'):
        read_image_file(path)


BLOCK = 4096


def has_holes(path):
    with open(path, 'rb') as file:
        return os.lseek(file.fileno(), 0, os.SEEK_HOLE) < path.stat().st_size


def write_sparse_image(path):
    """
    Write an image of 10 blocks and 10 bytes with data in the first
    and the last block and a hole in between
    """
    with open(path, 'wb') as raw:
        raw.write(b'\x01' * 100)
        raw.seek(10 * BLOCK + 5)
        raw.write(b'x')
        raw.truncate(10 * BLOCK + 10)
    return {
        0: b'\x01' * 100 + bytes(BLOCK - 100),
        10: bytes(5) + b'x' + bytes(BLOCK - 6),
    }


def test_read_blocks_raw(tmp_path):
    path = tmp_path / 'image.raw'
    expected = write_sparse_image(path)
    blocks = list(read_image_file(path).read_blocks(BLOCK, 11 * BLOCK))
    assert all(len(block.data) == BLOCK for block in blocks)
    assert {
        block.index: block.data for block in blocks if any(block.data)
    } == expected
    if has_holes(path):
        assert [block.index for block in blocks] == [0, 10]
    else:
        assert [block.index for block in blocks] == list(range(11))
    assert blocks[-1].position == path.stat().st_size


def test_read_blocks_raw_without_hole_detection(tmp_path, monkeypatch):
    path = tmp_path / 'image.raw'
    write_sparse_image(path)
    monkeypatch.delattr(os, 'SEEK_DATA')
    blocks = list(read_image_file(path).read_blocks(BLOCK, 11 * BLOCK))
    assert [block.index for block in blocks] == list(range(11))


def test_read_blocks_raw_only_hole(tmp_path):
    path = tmp_path / 'image.raw'
    with open(path, 'wb') as raw:
        raw.truncate(1024 ** 2)
    if not has_holes(path):
        pytest.skip('filesystem does not report holes')
    assert list(read_image_file(path).read_blocks(BLOCK, 1024 ** 2)) == []


def test_read_blocks_xz(tmp_path):
    raw = tmp_path / 'image.raw'
    expected = write_sparse_image(raw)
    path = tmp_path / 'image.raw.xz'
    with lzma.open(path, 'wb') as xz:
        xz.write(raw.read_bytes())
    blocks = list(read_image_file(path).read_blocks(BLOCK, 11 * BLOCK))
    # All blocks are read, also the zero ones
    assert [block.index for block in blocks] == list(range(11))
    assert all(len(block.data) == BLOCK for block in blocks)
    assert {
        block.index: block.data for block in blocks if any(block.data)
    } == expected
    positions = [block.position for block in blocks]
    assert positions == sorted(positions)
    assert positions[-1] == path.stat().st_size


@pytest.mark.parametrize('compress', [False, True])
def test_read_blocks_larger_than_max_size(tmp_path, compress):
    raw = tmp_path / 'image.raw'
    write_sparse_image(raw)
    path = raw
    if compress:
        path = tmp_path / 'image.raw.xz'
        with lzma.open(path, 'wb') as xz:
            xz.write(raw.read_bytes())
    image = read_image_file(path)
    with pytest.raises(EC2PushImageError, match='larger than'):
        list(image.read_blocks(BLOCK, 10 * BLOCK + 9))
    # Exactly the size of the image is fine
    assert list(image.read_blocks(BLOCK, 10 * BLOCK + 10))


def test_read_blocks_xz_corrupt(tmp_path):
    path = tmp_path / 'image.raw.xz'
    path.write_bytes(b'not really xz')
    with pytest.raises(EC2PushImageError, match='Unable to decompress'):
        list(read_image_file(path).read_blocks(BLOCK, 1024 ** 2))


def test_read_blocks_xz_truncated(tmp_path):
    path = tmp_path / 'image.raw.xz'
    with lzma.open(path, 'wb') as xz:
        xz.write(os.urandom(64 * 1024))
    path.write_bytes(path.read_bytes()[:32 * 1024])
    with pytest.raises(EC2PushImageError, match='Unable to decompress'):
        list(read_image_file(path).read_blocks(BLOCK, 1024 ** 2))

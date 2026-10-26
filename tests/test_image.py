import lzma
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

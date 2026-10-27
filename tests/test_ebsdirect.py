import base64
import hashlib
import threading

import pytest
from botocore.exceptions import ClientError

from ec2push.ebsdirect import (
    BLOCK_SIZE, BLOCKS_PER_THREAD, MAX_ATTEMPTS, client_config,
    write_snapshot_blocks
)
from ec2push.exceptions import EC2PushError
from ec2push.image import ImageBlock

SNAPSHOT_ID = 'snap-0123456789abcdef0'


class FakeEBS:
    """
    Records the blocks written with put_snapshot_block, verifies the
    arguments of each call
    """
    def __init__(self, fail_on=None) -> None:
        self.fail_on = fail_on
        self.blocks = {}
        self.lock = threading.Lock()

    def put_snapshot_block(self, **arguments):
        data = arguments['BlockData']
        assert arguments['SnapshotId'] == SNAPSHOT_ID
        assert arguments['DataLength'] == len(data) == BLOCK_SIZE
        assert arguments['ChecksumAlgorithm'] == 'SHA256'
        assert arguments['Checksum'] == base64.b64encode(
            hashlib.sha256(data).digest()
        ).decode()
        if arguments['BlockIndex'] == self.fail_on:
            raise ClientError(
                {'Error': {'Code': 'ValidationException', 'Message': 'bad'}},
                'PutSnapshotBlock'
            )
        with self.lock:
            assert arguments['BlockIndex'] not in self.blocks
            self.blocks[arguments['BlockIndex']] = data
        return {}


def linear_checksum(blocks):
    """
    The aggregated checksum of the given written blocks, a mapping of
    block index to data
    """
    digests = b''.join(
        hashlib.sha256(blocks[index]).digest() for index in sorted(blocks)
    )
    return base64.b64encode(hashlib.sha256(digests).digest()).decode()


def data_block(index, position=0):
    return ImageBlock(
        index, index.to_bytes(4, 'big') * (BLOCK_SIZE // 4), position
    )


def test_write_skips_zero_blocks():
    ebs = FakeEBS()
    blocks = [
        data_block(1, 10),
        ImageBlock(2, bytes(BLOCK_SIZE), 20),
        data_block(3, 30),
    ]
    progress = []
    data = write_snapshot_blocks(
        ebs, SNAPSHOT_ID, blocks, threads=2, progress=progress.append
    )
    assert data.blocks == 2
    assert ebs.blocks == {1: blocks[0].data, 3: blocks[2].data}
    # The zero block is not part of the aggregated checksum
    assert data.checksum == linear_checksum(ebs.blocks)
    assert progress == [10, 20, 30]


def test_write_all_zero():
    ebs = FakeEBS()
    blocks = [ImageBlock(index, bytes(BLOCK_SIZE), 0) for index in range(3)]
    data = write_snapshot_blocks(ebs, SNAPSHOT_ID, blocks)
    assert data.blocks == 0
    assert data.checksum == linear_checksum({})
    assert ebs.blocks == {}


def test_write_parallel():
    ebs = FakeEBS()
    blocks = [data_block(index) for index in range(1, 201)]
    data = write_snapshot_blocks(ebs, SNAPSHOT_ID, iter(blocks), threads=8)
    assert data.blocks == 200
    assert ebs.blocks == {block.index: block.data for block in blocks}
    # The blocks are written in any order, the aggregated checksum
    # follows the order of the block index
    assert data.checksum == linear_checksum(ebs.blocks)


def test_checksum_differs_for_changed_order():
    first = data_block(1)
    second = data_block(2)
    swapped = [
        ImageBlock(1, second.data, 0), ImageBlock(2, first.data, 0)
    ]
    assert write_snapshot_blocks(
        FakeEBS(), SNAPSHOT_ID, [first, second]
    ).checksum != write_snapshot_blocks(
        FakeEBS(), SNAPSHOT_ID, swapped
    ).checksum


@pytest.mark.parametrize('indexes', [[2, 1], [1, 1]])
def test_write_requires_ascending_blocks(indexes):
    blocks = [data_block(index) for index in indexes]
    with pytest.raises(EC2PushError, match='ascending order'):
        write_snapshot_blocks(FakeEBS(), SNAPSHOT_ID, blocks)


def test_write_failure_stops_reading():
    ebs = FakeEBS(fail_on=1)
    read = []

    def blocks():
        for index in range(1, 1001):
            read.append(index)
            yield data_block(index)
    with pytest.raises(ClientError, match='bad'):
        write_snapshot_blocks(ebs, SNAPSHOT_ID, blocks(), threads=1)
    # Reading stops as soon as the failure is noticed, at the latest
    # when the read ahead blocks are written
    assert len(read) <= BLOCKS_PER_THREAD + 1


def test_client_config():
    config = client_config(8)
    assert config.max_pool_connections == 8
    assert config.retries == {'mode': 'standard', 'max_attempts': MAX_ATTEMPTS}

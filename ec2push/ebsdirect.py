"""
Write a raw disk image into an EBS snapshot with the EBS direct APIs

The EBS direct APIs write the blocks of a snapshot without an EC2
instance and volume in between. A snapshot is started with
StartSnapshot, its blocks are written with PutSnapshotBlock and it
is finished with CompleteSnapshot. Blocks which are never written
read as zeros, all-zero blocks of the image are therefore skipped.

Each block is sent with its SHA256 checksum which EBS verifies on
PutSnapshotBlock. In addition, an aggregated checksum of all written
blocks can be passed to CompleteSnapshot to let EBS verify the
complete snapshot.
"""
import base64
import hashlib
from concurrent.futures import (
    FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
)
from typing import Any, Callable, Iterable, NamedTuple, Optional, Set

from botocore.config import Config

from ec2push.exceptions import EC2PushError
from ec2push.image import ImageBlock

# Size of a snapshot block, the only size PutSnapshotBlock accepts
BLOCK_SIZE = 512 * 1024
DEFAULT_THREADS = 16
# Blocks read ahead per upload thread, bounds the memory in use
BLOCKS_PER_THREAD = 2
# Attempts of the botocore retry handler per request, covers the
# throttling of PutSnapshotBlock
MAX_ATTEMPTS = 10
# Checksum settings of PutSnapshotBlock and CompleteSnapshot
CHECKSUM_ALGORITHM = 'SHA256'
CHECKSUM_AGGREGATION_METHOD = 'LINEAR'


class SnapshotData(NamedTuple):
    """
    The data written into a snapshot

    blocks is the number of written blocks, checksum the aggregated
    checksum of these blocks as expected by CompleteSnapshot with
    the LINEAR aggregation method: the Base64 encoded SHA256 of the
    concatenated SHA256 digests of all written blocks in ascending
    order of their block index.
    """
    blocks: int
    checksum: str


def client_config(threads: int) -> Config:
    """
    Configuration of the ebs client to serve the given number of
    upload threads
    """
    return Config(
        max_pool_connections=threads,
        retries={'mode': 'standard', 'max_attempts': MAX_ATTEMPTS}
    )


def write_snapshot_blocks(
    ebs: Any, snapshot_id: str, blocks: Iterable[ImageBlock],
    threads: int = DEFAULT_THREADS,
    progress: Optional[Callable[[int], None]] = None
) -> SnapshotData:
    """
    Write the given blocks into the started snapshot

    The blocks must be given in ascending order of their index,
    all-zero blocks are skipped. The blocks are written by the given
    number of threads, progress is called with the position of the
    last block handed to them.
    """
    zero_block = bytes(BLOCK_SIZE)
    written = 0
    last_index = -1
    linear_checksum = hashlib.sha256()
    pending: Set[Future] = set()

    def collect(done: Set[Future]) -> None:
        nonlocal written
        for future in done:
            # Raises the error of a failed block
            future.result()
        written += len(done)

    executor = ThreadPoolExecutor(
        max_workers=threads, thread_name_prefix='ec2push-block'
    )
    try:
        for block in blocks:
            if block.data != zero_block:
                if block.index <= last_index:
                    raise EC2PushError(
                        f'Block {block.index} given after block '
                        f'{last_index}, blocks must be in ascending order'
                    )
                last_index = block.index
                digest = hashlib.sha256(block.data).digest()
                linear_checksum.update(digest)
                if len(pending) >= threads * BLOCKS_PER_THREAD:
                    done, pending = wait(pending, return_when=FIRST_COMPLETED)
                    collect(done)
                pending.add(executor.submit(
                    put_snapshot_block, ebs, snapshot_id, block, digest
                ))
            if progress:
                progress(block.position)
        done, pending = wait(pending)
        collect(done)
    finally:
        executor.shutdown(wait=True, cancel_futures=True)
    return SnapshotData(written, encode_checksum(linear_checksum.digest()))


def put_snapshot_block(
    ebs: Any, snapshot_id: str, block: ImageBlock, digest: bytes
) -> None:
    """
    Write the given block, digest is the SHA256 digest of its data
    """
    ebs.put_snapshot_block(
        SnapshotId=snapshot_id,
        BlockIndex=block.index,
        BlockData=block.data,
        DataLength=len(block.data),
        Checksum=encode_checksum(digest),
        ChecksumAlgorithm=CHECKSUM_ALGORITHM
    )


def encode_checksum(digest: bytes) -> str:
    return base64.b64encode(digest).decode()

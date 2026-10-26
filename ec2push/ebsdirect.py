"""
Write a raw disk image into an EBS snapshot with the EBS direct APIs

The EBS direct APIs write the blocks of a snapshot without an EC2
instance and volume in between. A snapshot is started with
StartSnapshot, its blocks are written with PutSnapshotBlock and it
is finished with CompleteSnapshot. Blocks which are never written
read as zeros, all-zero blocks of the image are therefore skipped.
"""
import base64
import hashlib
from concurrent.futures import (
    FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
)
from typing import Any, Callable, Iterable, Optional, Set

from botocore.config import Config

from ec2push.image import ImageBlock

# Size of a snapshot block, the only size PutSnapshotBlock accepts
BLOCK_SIZE = 512 * 1024
DEFAULT_THREADS = 16
# Blocks read ahead per upload thread, bounds the memory in use
BLOCKS_PER_THREAD = 2
# Attempts of the botocore retry handler per request, covers the
# throttling of PutSnapshotBlock
MAX_ATTEMPTS = 10


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
) -> int:
    """
    Write the given blocks into the started snapshot

    All-zero blocks are skipped. The blocks are written by the given
    number of threads, progress is called with the position of the
    last block handed to them. Returns the number of written blocks.
    """
    zero_block = bytes(BLOCK_SIZE)
    written = 0
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
                if len(pending) >= threads * BLOCKS_PER_THREAD:
                    done, pending = wait(pending, return_when=FIRST_COMPLETED)
                    collect(done)
                pending.add(executor.submit(
                    put_snapshot_block, ebs, snapshot_id, block
                ))
            if progress:
                progress(block.position)
        done, pending = wait(pending)
        collect(done)
    finally:
        executor.shutdown(wait=True, cancel_futures=True)
    return written


def put_snapshot_block(ebs: Any, snapshot_id: str, block: ImageBlock) -> None:
    checksum = base64.b64encode(hashlib.sha256(block.data).digest()).decode()
    ebs.put_snapshot_block(
        SnapshotId=snapshot_id,
        BlockIndex=block.index,
        BlockData=block.data,
        DataLength=len(block.data),
        Checksum=checksum,
        ChecksumAlgorithm='SHA256'
    )

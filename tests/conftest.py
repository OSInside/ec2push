import os
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

import boto3
import pytest
from moto import mock_aws

import ec2push.uploader as uploader

REGION = 'eu-central-1'


class FakeRemoteHost:
    """
    Stands in for the ssh connection to the helper instance, records
    the commands and uploads instead of running them
    """
    instances: List['FakeRemoteHost'] = []
    fail_on: Optional[str] = None

    def __init__(self, hostname, user, pkey, timeout=300) -> None:
        self.hostname = hostname
        self.user = user
        self.pkey = pkey
        self.timeout = timeout
        self.connected = False
        self.commands: List[str] = []
        self.uploads: List[Tuple[Path, str]] = []
        FakeRemoteHost.instances.append(self)

    def connect(self) -> None:
        self.connected = True

    def run(self, command: str) -> str:
        self.commands.append(command)
        if self.fail_on and self.fail_on in command:
            raise uploader.EC2PushError(f'{command} failed')
        return ''

    def upload(
        self, source: Path, target: str,
        callback: Optional[Callable[[int, int], None]] = None
    ) -> None:
        self.uploads.append((source, target))
        if callback:
            size = source.stat().st_size
            callback(size, size)

    def find_volume_device(self, volume_id: str, device_name: str) -> str:
        return device_name.replace('/dev/sd', '/dev/xvd')

    def close(self) -> None:
        self.connected = False


@pytest.fixture(autouse=True)
def aws_credentials(monkeypatch):
    for variable in ('AWS_PROFILE', 'AWS_DEFAULT_PROFILE'):
        monkeypatch.delenv(variable, raising=False)
    monkeypatch.setenv('AWS_ACCESS_KEY_ID', 'testing')
    monkeypatch.setenv('AWS_SECRET_ACCESS_KEY', 'testing')
    monkeypatch.setenv('AWS_SESSION_TOKEN', 'testing')
    monkeypatch.setenv('AWS_DEFAULT_REGION', REGION)
    monkeypatch.setenv('AWS_CONFIG_FILE', os.devnull)
    monkeypatch.setenv('AWS_SHARED_CREDENTIALS_FILE', os.devnull)


@pytest.fixture
def aws():
    with mock_aws():
        yield boto3.session.Session(region_name=REGION)


@pytest.fixture
def remote(monkeypatch):
    FakeRemoteHost.instances = []
    FakeRemoteHost.fail_on = None
    monkeypatch.setattr(uploader, 'RemoteHost', FakeRemoteHost)
    monkeypatch.setattr(uploader.time, 'sleep', lambda seconds: None)
    fast: Dict[str, int] = {'Delay': 1, 'MaxAttempts': 3}
    monkeypatch.setattr(uploader, 'WAIT_DEFAULT', fast)
    monkeypatch.setattr(uploader, 'WAIT_SNAPSHOT', fast)
    monkeypatch.setattr(uploader, 'WAIT_IMAGE', fast)
    return FakeRemoteHost


@pytest.fixture
def raw_image(tmp_path) -> Path:
    image = tmp_path / 'test-image.x86_64-1.0.0.raw'
    with open(image, 'wb') as raw:
        raw.truncate(3 * 1024 ** 3 + 1)
    return image

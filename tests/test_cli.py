import pytest
from typer.testing import CliRunner

import ec2push.cli as cli
from ec2push import __version__
from ec2push.exceptions import EC2PushError
from ec2push.uploader import DEFAULT_HELPER_AMI

runner = CliRunner()


class FakeEC2Push:
    calls = []
    error = None

    def __init__(self, session, config, upload_progress=None):
        self.session = session
        self.config = config
        self.upload_progress = upload_progress

    def push(self, image):
        FakeEC2Push.calls.append((self.session, self.config, image))
        if self.error:
            raise self.error
        self.upload_progress(1, 2)
        self.upload_progress(2, 2)
        return 'ami-0123456789abcdef0'


@pytest.fixture
def pusher(monkeypatch):
    FakeEC2Push.calls = []
    FakeEC2Push.error = None
    monkeypatch.setattr(cli, 'EC2Push', FakeEC2Push)
    return FakeEC2Push


def test_push(pusher, raw_image):
    result = runner.invoke(cli.app, [
        str(raw_image), '--name', 'test-image', '--region', 'us-east-1',
        '--boot-mode', 'uefi', '--tpm', '--imds-v2', '--no-sriov',
        '--tag', 'project=demo', '-t', 'empty=',
    ])
    assert result.exit_code == 0, result.output
    assert result.stdout == 'ami-0123456789abcdef0\n'
    session, config, image = pusher.calls[0]
    assert session.region_name == 'us-east-1'
    assert image.path == raw_image
    assert config.image_name == 'test-image'
    assert config.boot_mode == 'uefi'
    assert config.tpm_support
    assert config.imds_v2
    assert not config.sriov_support
    assert config.ena_support
    assert config.tags == {'project': 'demo', 'empty': ''}
    assert config.method == 'direct'
    assert config.upload_threads == 16
    assert config.helper_ami == DEFAULT_HELPER_AMI
    assert config.instance_type == 't2.micro'
    assert config.ssh_user == 'ec2-user'


def test_push_with_helper(pusher, raw_image):
    result = runner.invoke(cli.app, [
        str(raw_image), '--name', 'test-image', '--method', 'helper',
        '--instance-type', 't3.micro', '--upload-threads', '4',
    ])
    assert result.exit_code == 0, result.output
    config = pusher.calls[0][1]
    assert config.method == 'helper'
    assert config.instance_type == 't3.micro'
    assert config.upload_threads == 4


@pytest.mark.parametrize('arguments', [
    ['--method', 'other'], ['--upload-threads', '0'],
])
def test_invalid_upload_options(pusher, raw_image, arguments):
    result = runner.invoke(
        cli.app, [str(raw_image), '--name', 'x'] + arguments
    )
    assert result.exit_code == 2
    assert pusher.calls == []


def test_push_failure(pusher, raw_image):
    pusher.error = EC2PushError('helper instance failed')
    result = runner.invoke(cli.app, [str(raw_image), '--name', 'x'])
    assert result.exit_code == 1
    assert result.stdout == ''
    assert 'helper instance failed' in result.stderr


@pytest.mark.parametrize('arguments, message', [
    (['--tpm'], '--tpm requires --boot-mode uefi'),
    (['--tag', 'novalue'], 'Invalid tag'),
    (['--ssh-key-name', 'my-key'], 'must be used together'),
])
def test_invalid_arguments(pusher, raw_image, arguments, message):
    result = runner.invoke(
        cli.app, [str(raw_image), '--name', 'x'] + arguments
    )
    assert result.exit_code == 1
    assert message in result.stderr
    assert pusher.calls == []


def test_unsupported_image(pusher, tmp_path):
    image = tmp_path / 'image.qcow2'
    image.write_bytes(b'data')
    result = runner.invoke(cli.app, [str(image), '--name', 'x'])
    assert result.exit_code == 1
    assert 'Unsupported image format' in result.stderr


def test_version():
    result = runner.invoke(cli.app, ['--version'])
    assert result.exit_code == 0
    assert result.stdout == f'ec2push {__version__}\n'

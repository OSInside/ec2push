import json
from unittest.mock import MagicMock, patch

import paramiko
import pytest

import ec2push.remote as remote_module
from ec2push.exceptions import EC2PushRemoteError
from ec2push.remote import RemoteHost


def connected_host(user='ec2-user', stdout=b'', stderr=b'', exit_code=0):
    host = RemoteHost('192.0.2.1', user, MagicMock())
    client = MagicMock()
    out = MagicMock()
    out.read.return_value = stdout
    out.channel.recv_exit_status.return_value = exit_code
    err = MagicMock()
    err.read.return_value = stderr
    client.exec_command.return_value = (MagicMock(), out, err)
    host.client = client
    return host


def test_run_with_sudo():
    host = connected_host(stdout=b' output \n')
    assert host.run("echo 'a b' | cat") == 'output'
    host.client.exec_command.assert_called_once_with(
        'sudo -n bash -o pipefail -c \'echo \'"\'"\'a b\'"\'"\' | cat\''
    )


def test_run_as_root():
    host = connected_host(user='root')
    host.run('true')
    host.client.exec_command.assert_called_once_with(
        'bash -o pipefail -c true'
    )


def test_run_failure():
    host = connected_host(stderr=b'no such device\n', exit_code=1)
    with pytest.raises(EC2PushRemoteError, match='exit code 1: no such'):
        host.run('mkfs.ext4 /dev/missing')


def test_run_not_connected():
    with pytest.raises(EC2PushRemoteError, match='Not connected'):
        RemoteHost('192.0.2.1', 'ec2-user', MagicMock()).run('true')


@pytest.mark.parametrize('devices, expected', [
    # Nitro: NVMe device with the volume id as serial
    ([
        {'name': 'nvme0n1', 'serial': 'vol0aaaaaaaaaaaaaaaa'},
        {'name': 'nvme1n1', 'serial': 'vol0123456789abcdef0'},
    ], '/dev/nvme1n1'),
    # Xen: /dev/sdf shows up as xvdf
    ([
        {'name': 'xvda', 'serial': None},
        {'name': 'xvdf', 'serial': None},
    ], '/dev/xvdf'),
])
def test_find_volume_device(devices, expected):
    host = connected_host(
        stdout=json.dumps({'blockdevices': devices}).encode()
    )
    assert host.find_volume_device(
        'vol-0123456789abcdef0', '/dev/sdf'
    ) == expected


def test_find_volume_device_not_found():
    host = connected_host(stdout=b'{"blockdevices": []}')
    with pytest.raises(EC2PushRemoteError, match='No block device'):
        host.find_volume_device('vol-0123456789abcdef0', '/dev/sdf')


def test_upload(tmp_path):
    host = connected_host()
    sftp = host.client.open_sftp.return_value
    callback = MagicMock()
    host.upload(tmp_path / 'image.raw', '/mnt/image.raw', callback)
    sftp.put.assert_called_once_with(
        str(tmp_path / 'image.raw'), '/mnt/image.raw',
        callback=callback, confirm=True
    )
    sftp.close.assert_called_once_with()


def test_upload_failure(tmp_path):
    host = connected_host()
    sftp = host.client.open_sftp.return_value
    sftp.put.side_effect = OSError('disk full')
    with pytest.raises(EC2PushRemoteError, match='disk full'):
        host.upload(tmp_path / 'image.raw', '/mnt/image.raw')
    sftp.close.assert_called_once_with()


@patch.object(remote_module.time, 'sleep')
@patch.object(remote_module.paramiko, 'SSHClient')
def test_connect_retries(mock_client, mock_sleep):
    first, second = MagicMock(), MagicMock()
    first.connect.side_effect = paramiko.SSHException('not ready')
    mock_client.side_effect = [first, second]
    host = RemoteHost('192.0.2.1', 'ec2-user', MagicMock())
    host.connect()
    assert host.client is second
    first.close.assert_called_once_with()
    second.get_transport.return_value.set_keepalive.assert_called_once_with(
        remote_module.KEEPALIVE_INTERVAL
    )


@patch.object(remote_module.time, 'sleep')
@patch.object(remote_module.paramiko, 'SSHClient')
def test_connect_timeout(mock_client, mock_sleep):
    mock_client.return_value.connect.side_effect = OSError('refused')
    host = RemoteHost('192.0.2.1', 'ec2-user', MagicMock(), timeout=0)
    with pytest.raises(EC2PushRemoteError, match='Unable to connect'):
        host.connect()
    assert host.client is None


def test_close():
    host = connected_host()
    client = host.client
    host.close()
    client.close.assert_called_once_with()
    assert host.client is None

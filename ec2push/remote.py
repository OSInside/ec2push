"""
SSH access to the helper instance
"""
import json
import logging
import shlex
import time
from pathlib import Path
from typing import Callable, Optional

import paramiko

from ec2push.exceptions import EC2PushRemoteError

log = logging.getLogger('ec2push')

# Seconds to wait between two connection attempts
CONNECT_RETRY_DELAY = 10
# Seconds between two keepalive packets on an established connection,
# keeps the connection open during the long running dump
KEEPALIVE_INTERVAL = 30


class RemoteHost:
    """
    Run commands on and copy files to the helper instance

    All commands are run through bash with pipefail set. If the
    user is not root, the commands are run through sudo.
    """
    def __init__(
        self, hostname: str, user: str, pkey: paramiko.PKey,
        timeout: int = 300
    ) -> None:
        self.hostname = hostname
        self.user = user
        self.pkey = pkey
        self.timeout = timeout
        self.client: Optional[paramiko.SSHClient] = None

    def connect(self) -> None:
        """
        Connect to the host, retry until the timeout is reached

        A freshly launched instance needs some time until sshd
        accepts connections and the key of the user is deployed.
        """
        deadline = time.monotonic() + self.timeout
        while True:
            client = paramiko.SSHClient()
            # The helper instance is created for this upload only, its
            # host key can't be known in advance
            client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
            try:
                client.connect(
                    hostname=self.hostname,
                    username=self.user,
                    pkey=self.pkey,
                    timeout=CONNECT_RETRY_DELAY,
                    banner_timeout=CONNECT_RETRY_DELAY,
                    auth_timeout=CONNECT_RETRY_DELAY,
                    allow_agent=False,
                    look_for_keys=False
                )
            except (paramiko.SSHException, OSError) as issue:
                client.close()
                if time.monotonic() >= deadline:
                    raise EC2PushRemoteError(
                        f'Unable to connect to {self.user}@{self.hostname} '
                        f'within {self.timeout}s: {issue}'
                    )
                log.debug(f'SSH not ready yet: {issue}')
                time.sleep(CONNECT_RETRY_DELAY)
                continue
            transport = client.get_transport()
            if transport:
                transport.set_keepalive(KEEPALIVE_INTERVAL)
            self.client = client
            log.debug(f'Connected to {self.user}@{self.hostname}')
            return

    def run(self, command: str) -> str:
        """
        Run the given command and return its stdout

        Raises EC2PushRemoteError if the command exits non zero
        """
        shell = f'bash -o pipefail -c {shlex.quote(command)}'
        if self.user != 'root':
            shell = f'sudo -n {shell}'
        log.debug(f'Remote: {command}')
        stdin, stdout, stderr = self._get_client().exec_command(shell)
        stdin.close()
        output = stdout.read().decode('utf-8', errors='replace')
        error = stderr.read().decode('utf-8', errors='replace')
        exit_code = stdout.channel.recv_exit_status()
        if exit_code != 0:
            raise EC2PushRemoteError(
                f'Remote command "{command}" failed with exit code '
                f'{exit_code}: {error.strip()}'
            )
        return output.strip()

    def upload(
        self, source: Path, target: str,
        callback: Optional[Callable[[int, int], None]] = None
    ) -> None:
        """
        Copy the given local file to the target path on the host
        """
        sftp = self._get_client().open_sftp()
        try:
            sftp.put(str(source), target, callback=callback, confirm=True)
        except (paramiko.SSHException, OSError) as issue:
            raise EC2PushRemoteError(
                f'Upload of {source} to {self.hostname}:{target} '
                f'failed: {issue}'
            )
        finally:
            sftp.close()

    def find_volume_device(self, volume_id: str, device_name: str) -> str:
        """
        Find the block device of an attached EBS volume

        On Nitro based instances EBS volumes show up as NVMe devices
        whose serial is the volume id without the dash. On Xen based
        instances the device requested at attach time, e.g /dev/sdf,
        shows up as /dev/xvdf.
        """
        serial = volume_id.replace('-', '')
        xen_name = device_name.replace('/dev/sd', 'xvd')
        devices = json.loads(
            self.run('lsblk --json --nodeps --output NAME,SERIAL')
        )
        for device in devices.get('blockdevices', []):
            if device.get('serial') == serial:
                return f'/dev/{device["name"]}'
        for device in devices.get('blockdevices', []):
            if device.get('name') == xen_name:
                return f'/dev/{device["name"]}'
        raise EC2PushRemoteError(
            f'No block device found for volume {volume_id} '
            f'attached as {device_name}'
        )

    def close(self) -> None:
        if self.client:
            self.client.close()
            self.client = None

    def _get_client(self) -> paramiko.SSHClient:
        if not self.client:
            raise EC2PushRemoteError(f'Not connected to {self.hostname}')
        return self.client

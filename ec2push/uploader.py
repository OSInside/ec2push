"""
Upload a raw disk image to AWS EC2 and register it as AMI

The procedure follows the one of ec2uploadimg from ec2imgutils:

1. Launch a helper instance and attach two EBS volumes to it, a
   storage volume to receive the image file and the target volume
   which becomes the root volume of the AMI
2. Wait for ssh access to the helper instance
3. Create an ext4 filesystem on the storage volume and mount it
4. Upload the image file to the storage volume
5. Dump the raw disk image to the target volume
6. Detach the target volume and create a snapshot from it
7. Register an AMI from the snapshot

All temporary resources (helper instance, volumes, security group
and key pair) are removed when the procedure ends, also on failure.
"""
import io
import logging
import math
import secrets
import shlex
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

import boto3
import paramiko
from botocore.exceptions import BotoCoreError, ClientError, WaiterError
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey
)

from ec2push.exceptions import EC2PushAWSError, EC2PushError
from ec2push.image import GiB, ImageFile
from ec2push.remote import RemoteHost

log = logging.getLogger('ec2push')

# Public SSM parameters of the latest Amazon Linux 2023 AMI. These
# parameters exist in every region and resolve to the AMI id of that
# region. The login user of Amazon Linux is ec2-user.
HELPER_AMI_PARAMETERS = {
    'x86_64': '/aws/service/ami-amazon-linux-latest/'
              'al2023-ami-kernel-default-x86_64',
    'arm64': '/aws/service/ami-amazon-linux-latest/'
             'al2023-ami-kernel-default-arm64',
}
DEFAULT_HELPER_AMI = f'resolve:ssm:{HELPER_AMI_PARAMETERS["x86_64"]}'
DEFAULT_INSTANCE_TYPE = 't2.micro'
DEFAULT_SSH_USER = 'ec2-user'

# Device names used to attach the volumes to the helper instance
STORAGE_DEVICE = '/dev/sdf'
TARGET_DEVICE = '/dev/sdg'
# Root device name of the registered AMI
ROOT_DEVICE = '/dev/sda1'
# Mount point of the storage volume on the helper instance
MOUNT_POINT = '/mnt/ec2push'
# Additional space on the storage volume for the filesystem metadata
STORAGE_OVERHEAD_GIB = 1
# Name and tag of all temporary resources, eases finding leftovers
RESOURCE_NAME = 'ec2push-helper'
CREATED_BY_TAG = {'Key': 'created-by', 'Value': 'ec2push'}

# Waiter settings: seconds between checks and number of checks
WAIT_DEFAULT = {'Delay': 5, 'MaxAttempts': 120}
WAIT_SNAPSHOT = {'Delay': 15, 'MaxAttempts': 480}
WAIT_IMAGE = {'Delay': 15, 'MaxAttempts': 240}
# Attempts to find the block device of an attached volume
DEVICE_LOOKUP_ATTEMPTS = 30
DEVICE_LOOKUP_DELAY = 2
# Attempts to delete the security group, deletion fails until
# EC2 has released the network interface of the helper instance
SG_DELETE_ATTEMPTS = 24
SG_DELETE_DELAY = 5


@dataclass
class UploadConfig:
    """
    Settings of an image upload
    """
    image_name: str
    description: Optional[str] = None
    arch: str = 'x86_64'
    boot_mode: Optional[str] = None
    ena_support: bool = True
    sriov_support: bool = True
    tpm_support: bool = False
    imds_v2: bool = False
    root_volume_size: Optional[int] = None
    volume_type: str = 'gp3'
    tags: Dict[str, str] = field(default_factory=dict)
    helper_ami: str = DEFAULT_HELPER_AMI
    instance_type: str = DEFAULT_INSTANCE_TYPE
    ssh_user: str = DEFAULT_SSH_USER
    ssh_key_name: Optional[str] = None
    ssh_private_key: Optional[Path] = None
    ssh_timeout: int = 300
    ssh_cidr: str = '0.0.0.0/0'
    subnet_id: Optional[str] = None
    security_group_id: Optional[str] = None
    use_private_ip: bool = False


UploadProgress = Callable[[int, int], None]


class EC2Push:
    """
    Upload a raw disk image to AWS EC2 and register it as AMI
    """
    def __init__(
        self, session: boto3.session.Session, config: UploadConfig,
        upload_progress: Optional[UploadProgress] = None
    ) -> None:
        if not session.region_name:
            raise EC2PushError(
                'No AWS region configured, use --region or set the '
                'region of the profile'
            )
        if bool(config.ssh_key_name) != bool(config.ssh_private_key):
            raise EC2PushError(
                'An existing key pair requires both, the key pair name '
                'and the private key file'
            )
        self.session = session
        self.config = config
        self.upload_progress = upload_progress
        self.region = session.region_name
        self.ec2 = session.client('ec2')
        self.ssm = session.client('ssm')

        self.remote: Optional[RemoteHost] = None
        self.pkey: Optional[paramiko.PKey] = None
        self.key_pair_name: Optional[str] = config.ssh_key_name
        self.temporary_key_pair: Optional[str] = None
        self.security_group_id: Optional[str] = config.security_group_id
        self.subnet_id: Optional[str] = config.subnet_id
        self.temporary_security_group: Optional[str] = None
        self.instance: Optional[Dict[str, Any]] = None
        self.instance_terminating = False
        self.volumes: List[str] = []
        # Snapshot of the target volume until an image got registered
        # from it
        self.temporary_snapshot: Optional[str] = None

    def push(self, image: ImageFile) -> str:
        """
        Upload the given image and register it as AMI

        Returns the id of the registered AMI
        """
        target_size = self._get_target_volume_size(image)
        self._check_image_name_available()
        try:
            helper_ami = self._resolve_helper_ami()
            self.subnet_id = self._resolve_subnet()
            self._setup_key_pair()
            self._setup_security_group()
            self._launch_helper_instance(helper_ami)
            zone = self.instance['Placement']['AvailabilityZone']
            storage_volume = self._create_volume(
                math.ceil(image.upload_size / GiB) + STORAGE_OVERHEAD_GIB,
                zone, 'storage'
            )
            target_volume = self._create_volume(target_size, zone, 'target')
            self._attach_volume(storage_volume, STORAGE_DEVICE)
            self._attach_volume(target_volume, TARGET_DEVICE)
            self._connect_helper_instance()
            storage_device = self._find_volume_device(
                storage_volume, STORAGE_DEVICE
            )
            target_device = self._find_volume_device(
                target_volume, TARGET_DEVICE
            )
            self._create_storage_filesystem(storage_device)
            remote_image = self._upload_image(image)
            self._dump_image(image, remote_image, target_device)
            self._disconnect_helper_instance()
            self._detach_volume(target_volume)
            # The helper instance is no longer needed, start its
            # termination while the snapshot is created
            self._terminate_helper_instance()
            snapshot_id = self._create_snapshot(target_volume)
            return self._register_image(snapshot_id, target_size)
        finally:
            self.cleanup()

    def cleanup(self) -> None:
        """
        Remove all temporary resources

        A failure to remove one resource is logged and does not
        prevent the removal of the others.
        """
        self._disconnect_helper_instance()
        steps: List[Callable[[], None]] = [
            self._cleanup_instance,
            self._cleanup_volumes,
            self._cleanup_snapshot,
            self._cleanup_security_group,
            self._cleanup_key_pair,
        ]
        for step in steps:
            try:
                step()
            except Exception as issue:
                log.warning(f'Cleanup: {issue}')

    def _get_target_volume_size(self, image: ImageFile) -> int:
        raw_size_gib = image.raw_size_gib
        size = self.config.root_volume_size
        if size is None:
            if raw_size_gib is None:
                raise EC2PushError(
                    f'Unable to read the uncompressed size of {image.path}, '
                    'use --root-volume-size to specify the volume size'
                )
            return raw_size_gib
        if raw_size_gib is not None and size < raw_size_gib:
            raise EC2PushError(
                f'Root volume size of {size}GiB is too small for the '
                f'image of {raw_size_gib}GiB'
            )
        return size

    def _check_image_name_available(self) -> None:
        images = self.ec2.describe_images(
            Owners=['self'],
            Filters=[{'Name': 'name', 'Values': [self.config.image_name]}]
        )['Images']
        if images:
            raise EC2PushError(
                f'An image named "{self.config.image_name}" already '
                f'exists in {self.region}: {images[0]["ImageId"]}'
            )

    def _resolve_helper_ami(self) -> str:
        """
        Provide the AMI id of the helper instance

        The helper AMI is either an AMI id or the name of an SSM
        parameter holding the AMI id, optionally prefixed with
        'resolve:ssm:'. If the default helper AMI is used, the
        Amazon Linux variant matching the architecture of the
        instance type is selected.
        """
        helper_ami = self.config.helper_ami
        if helper_ami.startswith('ami-'):
            return helper_ami
        if helper_ami == DEFAULT_HELPER_AMI:
            parameter = HELPER_AMI_PARAMETERS[
                self._get_instance_type_arch()
            ]
        else:
            parameter = helper_ami.removeprefix('resolve:ssm:')
        try:
            ami = self.ssm.get_parameter(Name=parameter)['Parameter']['Value']
        except ClientError as issue:
            raise EC2PushAWSError(
                f'Unable to resolve helper AMI from SSM parameter '
                f'{parameter}: {issue}'
            )
        log.info(f'Using helper AMI {ami} from {parameter}')
        return ami

    def _get_instance_type_arch(self) -> str:
        instance_type = self.ec2.describe_instance_types(
            InstanceTypes=[self.config.instance_type]
        )['InstanceTypes'][0]
        architectures = instance_type['ProcessorInfo'][
            'SupportedArchitectures'
        ]
        for arch in HELPER_AMI_PARAMETERS:
            if arch in architectures:
                return arch
        raise EC2PushError(
            f'No helper AMI for instance type {self.config.instance_type} '
            f'with architectures {architectures}, use --helper-ami'
        )

    def _setup_key_pair(self) -> None:
        """
        Load the private key of the given key pair or create a
        temporary key pair

        The temporary key is generated locally, only the public key
        is imported to EC2 and the private key is never written to
        disk.
        """
        if self.config.ssh_private_key:
            self.pkey = load_private_key(self.config.ssh_private_key)
            return
        private_key = Ed25519PrivateKey.generate()
        public_key = private_key.public_key().public_bytes(
            serialization.Encoding.OpenSSH,
            serialization.PublicFormat.OpenSSH
        )
        private_key_text = private_key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.OpenSSH,
            serialization.NoEncryption()
        ).decode()
        self.pkey = paramiko.Ed25519Key.from_private_key(
            io.StringIO(private_key_text)
        )
        name = f'{RESOURCE_NAME}-{secrets.token_hex(4)}'
        self.ec2.import_key_pair(
            KeyName=name,
            PublicKeyMaterial=public_key,
            TagSpecifications=[self._tag_spec('key-pair')]
        )
        self.temporary_key_pair = self.key_pair_name = name
        log.info(f'Created temporary key pair {name}')

    def _setup_security_group(self) -> None:
        """
        Create a temporary security group allowing ssh access unless
        a security group was given
        """
        if self.security_group_id:
            return
        vpc_id = self._get_vpc_id()
        group_id = self.ec2.create_security_group(
            GroupName=f'{RESOURCE_NAME}-{secrets.token_hex(4)}',
            Description='Temporary ssh access to the ec2push helper instance',
            VpcId=vpc_id,
            TagSpecifications=[self._tag_spec('security-group')]
        )['GroupId']
        self.temporary_security_group = self.security_group_id = group_id
        log.info(f'Created temporary security group {group_id} in {vpc_id}')
        self.ec2.authorize_security_group_ingress(
            GroupId=group_id,
            IpPermissions=[
                {
                    'IpProtocol': 'tcp',
                    'FromPort': 22,
                    'ToPort': 22,
                    'IpRanges': [{'CidrIp': self.config.ssh_cidr}]
                }
            ]
        )

    def _resolve_subnet(self) -> str:
        """
        Provide the subnet of the helper instance

        Without a given subnet, the default subnet of the default VPC
        in the first availability zone which offers the instance type
        is used.
        """
        if self.config.subnet_id:
            return self.config.subnet_id
        vpcs = self.ec2.describe_vpcs(
            Filters=[{'Name': 'is-default', 'Values': ['true']}]
        )['Vpcs']
        if not vpcs:
            raise EC2PushError(
                f'No default VPC in {self.region}, use --subnet-id to '
                'specify the subnet of the helper instance'
            )
        offered_zones = {
            offering['Location'] for offering in
            self.ec2.describe_instance_type_offerings(
                LocationType='availability-zone',
                Filters=[{
                    'Name': 'instance-type',
                    'Values': [self.config.instance_type]
                }]
            )['InstanceTypeOfferings']
        }
        subnets = self.ec2.describe_subnets(
            Filters=[
                {'Name': 'vpc-id', 'Values': [vpcs[0]['VpcId']]},
                {'Name': 'default-for-az', 'Values': ['true']},
            ]
        )['Subnets']
        for subnet in sorted(subnets, key=lambda s: s['AvailabilityZone']):
            if subnet['AvailabilityZone'] in offered_zones:
                return subnet['SubnetId']
        raise EC2PushError(
            f'No default subnet in {self.region} offers instance type '
            f'{self.config.instance_type}, use --subnet-id or '
            '--instance-type'
        )

    def _get_vpc_id(self) -> str:
        return self.ec2.describe_subnets(
            SubnetIds=[self.subnet_id]
        )['Subnets'][0]['VpcId']

    def _launch_helper_instance(self, helper_ami: str) -> None:
        network_interface: Dict[str, Any] = {
            'DeviceIndex': 0,
            'AssociatePublicIpAddress': not self.config.use_private_ip,
            'Groups': [self.security_group_id],
            'SubnetId': self.subnet_id,
            'DeleteOnTermination': True,
        }
        instance = self.ec2.run_instances(
            ImageId=helper_ami,
            InstanceType=self.config.instance_type,
            KeyName=self.key_pair_name,
            MinCount=1,
            MaxCount=1,
            NetworkInterfaces=[network_interface],
            InstanceInitiatedShutdownBehavior='terminate',
            TagSpecifications=[self._tag_spec('instance')]
        )['Instances'][0]
        instance_id = instance['InstanceId']
        self.instance = instance
        log.info(f'Launched helper instance {instance_id}, waiting for it')
        self._wait('instance_running', InstanceIds=[instance_id])
        self.instance = self.ec2.describe_instances(
            InstanceIds=[instance_id]
        )['Reservations'][0]['Instances'][0]

    def _create_volume(self, size: int, zone: str, purpose: str) -> str:
        volume_id = self.ec2.create_volume(
            AvailabilityZone=zone,
            Size=size,
            VolumeType='gp3',
            TagSpecifications=[self._tag_spec('volume', purpose)]
        )['VolumeId']
        self.volumes.append(volume_id)
        log.info(f'Created {purpose} volume {volume_id} of {size}GiB')
        self._wait('volume_available', VolumeIds=[volume_id])
        return volume_id

    def _attach_volume(self, volume_id: str, device: str) -> None:
        self.ec2.attach_volume(
            VolumeId=volume_id,
            InstanceId=self._instance_id(),
            Device=device
        )
        self._wait('volume_in_use', VolumeIds=[volume_id], Filters=[
            {'Name': 'attachment.status', 'Values': ['attached']}
        ])
        log.debug(f'Attached volume {volume_id} as {device}')

    def _detach_volume(self, volume_id: str) -> None:
        self.ec2.detach_volume(
            VolumeId=volume_id, InstanceId=self._instance_id()
        )
        self._wait('volume_available', VolumeIds=[volume_id])
        log.debug(f'Detached volume {volume_id}')

    def _connect_helper_instance(self) -> None:
        if self.config.use_private_ip:
            address = self.instance.get('PrivateIpAddress')
        else:
            address = self.instance.get('PublicIpAddress')
        if not address:
            raise EC2PushAWSError(
                f'Helper instance {self._instance_id()} has no '
                f'{"private" if self.config.use_private_ip else "public"} '
                'IP address'
            )
        log.info(f'Waiting for ssh access to {address}')
        self.remote = RemoteHost(
            address, self.config.ssh_user, self.pkey, self.config.ssh_timeout
        )
        self.remote.connect()

    def _disconnect_helper_instance(self) -> None:
        if self.remote:
            self.remote.close()
            self.remote = None

    def _find_volume_device(self, volume_id: str, device: str) -> str:
        """
        Find the block device of the given volume on the helper
        instance, it might take a moment until the device shows up
        after the volume got attached
        """
        attempt = 1
        while True:
            try:
                block_device = self._remote().find_volume_device(
                    volume_id, device
                )
            except EC2PushError:
                if attempt >= DEVICE_LOOKUP_ATTEMPTS:
                    raise
                attempt += 1
                time.sleep(DEVICE_LOOKUP_DELAY)
                continue
            log.debug(f'Volume {volume_id} is {block_device}')
            return block_device

    def _create_storage_filesystem(self, device: str) -> None:
        log.info('Creating ext4 filesystem on the storage volume')
        remote = self._remote()
        remote.run(f'mkfs.ext4 -q -F -m 0 {device}')
        remote.run(f'mkdir -p {MOUNT_POINT}')
        remote.run(f'mount {device} {MOUNT_POINT}')
        remote.run(f'chown {shlex.quote(self.config.ssh_user)} {MOUNT_POINT}')

    def _upload_image(self, image: ImageFile) -> str:
        remote_image = f'{MOUNT_POINT}/image.raw'
        if image.xz_compressed:
            remote_image += '.xz'
        log.info(f'Uploading {image.path}')
        self._remote().upload(image.path, remote_image, self.upload_progress)
        return remote_image

    def _dump_image(
        self, image: ImageFile, remote_image: str, device: str
    ) -> None:
        log.info(f'Dumping the image to the target volume {device}')
        dd = f'dd of={device} bs=4M iflag=fullblock conv=fsync status=none'
        if image.xz_compressed:
            command = f'xz -dc {remote_image} | {dd}'
        else:
            command = f'{dd} if={remote_image}'
        remote = self._remote()
        remote.run(command)
        remote.run(f'umount {MOUNT_POINT}')

    def _terminate_helper_instance(self) -> None:
        if self.instance and not self.instance_terminating:
            self.ec2.terminate_instances(InstanceIds=[self._instance_id()])
            self.instance_terminating = True
            log.info(f'Terminating helper instance {self._instance_id()}')

    def _create_snapshot(self, volume_id: str) -> str:
        snapshot_id = self.ec2.create_snapshot(
            VolumeId=volume_id,
            Description=self.config.description or self.config.image_name,
            TagSpecifications=[self._image_tag_spec('snapshot')]
        )['SnapshotId']
        self.temporary_snapshot = snapshot_id
        log.info(f'Creating snapshot {snapshot_id}, waiting for it')
        self._wait(
            'snapshot_completed', WAIT_SNAPSHOT, SnapshotIds=[snapshot_id]
        )
        return snapshot_id

    def _register_image(self, snapshot_id: str, volume_size: int) -> str:
        config = self.config
        arguments: Dict[str, Any] = {
            'Name': config.image_name,
            'Architecture': config.arch,
            'VirtualizationType': 'hvm',
            'RootDeviceName': ROOT_DEVICE,
            'BlockDeviceMappings': [
                {
                    'DeviceName': ROOT_DEVICE,
                    'Ebs': {
                        'SnapshotId': snapshot_id,
                        'VolumeSize': volume_size,
                        'VolumeType': config.volume_type,
                        'DeleteOnTermination': True,
                    }
                }
            ],
            'EnaSupport': config.ena_support,
            'TagSpecifications': [self._image_tag_spec('image')],
        }
        if config.description:
            arguments['Description'] = config.description
        if config.sriov_support:
            arguments['SriovNetSupport'] = 'simple'
        if config.boot_mode:
            arguments['BootMode'] = config.boot_mode
        if config.tpm_support:
            arguments['TpmSupport'] = 'v2.0'
        if config.imds_v2:
            arguments['ImdsSupport'] = 'v2.0'
        image_id = self.ec2.register_image(**arguments)['ImageId']
        # The snapshot belongs to the image from now on
        self.temporary_snapshot = None
        log.info(f'Registered image {image_id}, waiting for it')
        try:
            self._wait('image_available', WAIT_IMAGE, ImageIds=[image_id])
        except EC2PushAWSError as issue:
            raise EC2PushAWSError(
                f'Image {image_id} got registered but did not become '
                f'available: {issue}'
            )
        return image_id

    def _cleanup_instance(self) -> None:
        if not self.instance:
            return
        instance_id = self._instance_id()
        self._terminate_helper_instance()
        self._wait('instance_terminated', InstanceIds=[instance_id])
        log.info(f'Terminated helper instance {instance_id}')
        self.instance = None

    def _cleanup_volumes(self) -> None:
        for volume_id in list(self.volumes):
            try:
                # Volumes get detached by the termination of the
                # helper instance
                self._wait('volume_available', VolumeIds=[volume_id])
                self.ec2.delete_volume(VolumeId=volume_id)
            except (BotoCoreError, ClientError, EC2PushError) as issue:
                log.warning(f'Cleanup: failed to delete {volume_id}: {issue}')
                continue
            log.info(f'Deleted volume {volume_id}')
            self.volumes.remove(volume_id)

    def _cleanup_security_group(self) -> None:
        group_id = self.temporary_security_group
        if not group_id:
            return
        attempt = 1
        while True:
            try:
                self.ec2.delete_security_group(GroupId=group_id)
            except ClientError as issue:
                code = issue.response['Error']['Code']
                if code != 'DependencyViolation' or \
                   attempt >= SG_DELETE_ATTEMPTS:
                    raise
                attempt += 1
                time.sleep(SG_DELETE_DELAY)
                continue
            log.info(f'Deleted security group {group_id}')
            self.temporary_security_group = None
            return

    def _cleanup_snapshot(self) -> None:
        if not self.temporary_snapshot:
            return
        self.ec2.delete_snapshot(SnapshotId=self.temporary_snapshot)
        log.info(f'Deleted snapshot {self.temporary_snapshot}')
        self.temporary_snapshot = None

    def _cleanup_key_pair(self) -> None:
        if not self.temporary_key_pair:
            return
        self.ec2.delete_key_pair(KeyName=self.temporary_key_pair)
        log.info(f'Deleted key pair {self.temporary_key_pair}')
        self.temporary_key_pair = None

    def _wait(
        self, name: str, config: Optional[Dict[str, int]] = None,
        **arguments: Any
    ) -> None:
        try:
            self.ec2.get_waiter(name).wait(
                WaiterConfig=config or WAIT_DEFAULT, **arguments
            )
        except WaiterError as issue:
            raise EC2PushAWSError(f'Waiting for {name} failed: {issue}')

    def _tag_spec(
        self, resource_type: str, purpose: Optional[str] = None
    ) -> Dict[str, Any]:
        name = f'{RESOURCE_NAME}-{purpose}' if purpose else RESOURCE_NAME
        return {
            'ResourceType': resource_type,
            'Tags': [{'Key': 'Name', 'Value': name}, CREATED_BY_TAG]
        }

    def _image_tag_spec(self, resource_type: str) -> Dict[str, Any]:
        tags = {'Name': self.config.image_name, **self.config.tags}
        return {
            'ResourceType': resource_type,
            'Tags': [
                {'Key': key, 'Value': value} for key, value in tags.items()
            ]
        }

    def _instance_id(self) -> str:
        if not self.instance:
            raise EC2PushError('No helper instance launched')
        return self.instance['InstanceId']

    def _remote(self) -> RemoteHost:
        if not self.remote:
            raise EC2PushError('Not connected to the helper instance')
        return self.remote


def load_private_key(path: Path) -> paramiko.PKey:
    """
    Load the ssh private key from the given file, supports all
    key types of paramiko
    """
    try:
        return paramiko.PKey.from_path(path)
    except (OSError, paramiko.SSHException, ValueError) as issue:
        raise EC2PushError(f'Unable to load private key {path}: {issue}')

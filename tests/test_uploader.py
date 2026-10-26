import pytest
from botocore.exceptions import ClientError

from ec2push.exceptions import EC2PushAWSError, EC2PushError
from ec2push.image import read_image_file
from ec2push.uploader import (
    HELPER_AMI_PARAMETERS, MOUNT_POINT, ROOT_DEVICE, EC2Push, UploadConfig,
    load_private_key
)


def spy_register_image(pusher):
    """
    Record the arguments of register_image, moto does not keep all
    of them in the image
    """
    calls = []
    register_image = pusher.ec2.register_image

    def spy(**arguments):
        calls.append(arguments)
        return register_image(**arguments)
    pusher.ec2.register_image = spy
    return calls


def image_snapshots(session, name='test-image'):
    return session.client('ec2').describe_snapshots(
        Filters=[{'Name': 'tag:Name', 'Values': [name]}]
    )['Snapshots']


def leftovers(session):
    """
    Temporary resources created by ec2push which still exist
    """
    ec2 = session.client('ec2')
    created_by = [{'Name': 'tag:created-by', 'Values': ['ec2push']}]
    instances = [
        instance['InstanceId']
        for reservation in ec2.describe_instances(
            Filters=created_by + [{
                'Name': 'instance-state-name',
                'Values': ['pending', 'running', 'stopping', 'stopped']
            }]
        )['Reservations']
        for instance in reservation['Instances']
    ]
    volumes = [
        volume['VolumeId']
        for volume in ec2.describe_volumes(Filters=created_by)['Volumes']
    ]
    groups = [
        group['GroupId'] for group in
        ec2.describe_security_groups(Filters=created_by)['SecurityGroups']
    ]
    keys = [
        key['KeyName']
        for key in ec2.describe_key_pairs(Filters=created_by)['KeyPairs']
    ]
    return instances + volumes + groups + keys


def test_push(aws, remote, raw_image):
    config = UploadConfig(
        image_name='test-image',
        description='Test image',
        boot_mode='uefi',
        tpm_support=True,
        imds_v2=True,
        tags={'project': 'demo'}
    )
    progress = []
    pusher = EC2Push(aws, config, lambda done, total: progress.append(done))
    register_calls = spy_register_image(pusher)
    image_id = pusher.push(read_image_file(raw_image))

    ec2 = aws.client('ec2')
    image = ec2.describe_images(ImageIds=[image_id])['Images'][0]
    assert image['Name'] == 'test-image'
    arguments = register_calls[0]
    snapshot_id = arguments['BlockDeviceMappings'][0]['Ebs']['SnapshotId']
    assert arguments == {
        'Name': 'test-image',
        'Description': 'Test image',
        'Architecture': 'x86_64',
        'VirtualizationType': 'hvm',
        'RootDeviceName': ROOT_DEVICE,
        'BlockDeviceMappings': [{
            'DeviceName': ROOT_DEVICE,
            'Ebs': {
                'SnapshotId': snapshot_id,
                'VolumeSize': 4,
                'VolumeType': 'gp3',
                'DeleteOnTermination': True,
            }
        }],
        'EnaSupport': True,
        'SriovNetSupport': 'simple',
        'BootMode': 'uefi',
        'TpmSupport': 'v2.0',
        'ImdsSupport': 'v2.0',
        'TagSpecifications': [{
            'ResourceType': 'image',
            'Tags': [
                {'Key': 'Name', 'Value': 'test-image'},
                {'Key': 'project', 'Value': 'demo'},
            ]
        }],
    }
    snapshot = ec2.describe_snapshots(
        SnapshotIds=[snapshot_id]
    )['Snapshots'][0]
    assert {'Key': 'project', 'Value': 'demo'} in snapshot['Tags']

    host = remote.instances[0]
    assert host.user == 'ec2-user'
    assert host.uploads == [(raw_image, f'{MOUNT_POINT}/image.raw')]
    assert host.commands == [
        'mkfs.ext4 -q -F -m 0 /dev/xvdf',
        f'mkdir -p {MOUNT_POINT}',
        f'mount /dev/xvdf {MOUNT_POINT}',
        f'chown ec2-user {MOUNT_POINT}',
        'dd of=/dev/xvdg bs=4M iflag=fullblock conv=fsync status=none '
        f'if={MOUNT_POINT}/image.raw',
        f'umount {MOUNT_POINT}',
    ]
    assert progress == [raw_image.stat().st_size]
    assert leftovers(aws) == []


def test_push_cleanup_on_failure(aws, remote, raw_image):
    remote.fail_on = 'dd '
    with pytest.raises(EC2PushError, match='failed'):
        EC2Push(aws, UploadConfig(image_name='test-image')).push(
            read_image_file(raw_image)
        )
    images = aws.client('ec2').describe_images(Owners=['self'])['Images']
    assert images == []
    assert leftovers(aws) == []


def test_push_existing_image_name(aws, remote, raw_image):
    EC2Push(aws, UploadConfig(image_name='test-image')).push(
        read_image_file(raw_image)
    )
    remote.instances = []
    with pytest.raises(EC2PushError, match='already exists'):
        EC2Push(aws, UploadConfig(image_name='test-image')).push(
            read_image_file(raw_image)
        )
    # Nothing was launched for the second upload
    assert remote.instances == []


def test_push_root_volume_too_small(aws, remote, raw_image):
    config = UploadConfig(image_name='test-image', root_volume_size=2)
    with pytest.raises(EC2PushError, match='too small'):
        EC2Push(aws, config).push(read_image_file(raw_image))


def test_push_xz_image(aws, remote, tmp_path):
    import lzma
    path = tmp_path / 'image.raw.xz'
    with lzma.open(path, 'wb') as xz:
        xz.write(b'\0' * 4096)
    image = read_image_file(path)
    image.raw_size = None
    config = UploadConfig(image_name='test-image', root_volume_size=10)
    pusher = EC2Push(aws, config)
    register_calls = spy_register_image(pusher)
    pusher.push(image)
    mapping = register_calls[0]['BlockDeviceMappings'][0]
    assert mapping['Ebs']['VolumeSize'] == 10
    assert remote.instances[0].commands[4] == (
        f'xz -dc {MOUNT_POINT}/image.raw.xz | dd of=/dev/xvdg bs=4M '
        'iflag=fullblock conv=fsync status=none'
    )


def test_push_xz_image_unknown_size(aws, remote, tmp_path):
    path = tmp_path / 'image.raw.xz'
    path.write_bytes(b'not really xz')
    image = read_image_file(path)
    image.raw_size = None
    with pytest.raises(EC2PushError, match='--root-volume-size'):
        EC2Push(aws, UploadConfig(image_name='test-image')).push(image)


def test_push_with_existing_key_and_group(aws, remote, raw_image, tmp_path):
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import (
        Ed25519PrivateKey
    )
    key = Ed25519PrivateKey.generate()
    key_file = tmp_path / 'id_ed25519'
    key_file.write_bytes(key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.OpenSSH,
        serialization.NoEncryption()
    ))
    ec2 = aws.client('ec2')
    ec2.import_key_pair(
        KeyName='my-key',
        PublicKeyMaterial=key.public_key().public_bytes(
            serialization.Encoding.OpenSSH,
            serialization.PublicFormat.OpenSSH
        )
    )
    vpc_id = ec2.describe_vpcs()['Vpcs'][0]['VpcId']
    group_id = ec2.create_security_group(
        GroupName='my-group', Description='ssh', VpcId=vpc_id
    )['GroupId']
    config = UploadConfig(
        image_name='test-image',
        ssh_key_name='my-key',
        ssh_private_key=key_file,
        security_group_id=group_id
    )
    EC2Push(aws, config).push(read_image_file(raw_image))
    # Given resources are used and not removed
    assert ec2.describe_key_pairs(KeyNames=['my-key'])['KeyPairs']
    assert ec2.describe_security_groups(GroupIds=[group_id])
    assert leftovers(aws) == []


def test_key_pair_name_requires_private_key(aws):
    with pytest.raises(EC2PushError, match='both'):
        EC2Push(aws, UploadConfig(image_name='x', ssh_key_name='my-key'))


def test_missing_region(aws, monkeypatch):
    import boto3
    monkeypatch.delenv('AWS_DEFAULT_REGION')
    session = boto3.session.Session()
    with pytest.raises(EC2PushError, match='region'):
        EC2Push(session, UploadConfig(image_name='x'))


def test_helper_ami_default_x86_64(aws):
    pusher = EC2Push(aws, UploadConfig(image_name='x'))
    expected = aws.client('ssm').get_parameter(
        Name=HELPER_AMI_PARAMETERS['x86_64']
    )['Parameter']['Value']
    assert pusher._resolve_helper_ami() == expected


def test_helper_ami_default_follows_instance_type(aws):
    config = UploadConfig(image_name='x', instance_type='t4g.micro')
    expected = aws.client('ssm').get_parameter(
        Name=HELPER_AMI_PARAMETERS['arm64']
    )['Parameter']['Value']
    assert EC2Push(aws, config)._resolve_helper_ami() == expected


def test_helper_ami_id(aws):
    config = UploadConfig(image_name='x', helper_ami='ami-0123456789abcdef0')
    assert EC2Push(aws, config)._resolve_helper_ami() == (
        'ami-0123456789abcdef0'
    )


def test_helper_ami_ssm_parameter(aws):
    aws.client('ssm').put_parameter(
        Name='/custom/helper', Value='ami-0aaaaaaaaaaaaaaaa', Type='String'
    )
    for helper_ami in ('/custom/helper', 'resolve:ssm:/custom/helper'):
        config = UploadConfig(image_name='x', helper_ami=helper_ami)
        assert EC2Push(aws, config)._resolve_helper_ami() == (
            'ami-0aaaaaaaaaaaaaaaa'
        )


def test_helper_ami_ssm_parameter_missing(aws):
    config = UploadConfig(image_name='x', helper_ami='/missing')
    with pytest.raises(EC2PushError, match='Unable to resolve'):
        EC2Push(aws, config)._resolve_helper_ami()


def test_subnet_default(aws):
    subnet_id = EC2Push(aws, UploadConfig(image_name='x'))._resolve_subnet()
    subnet = aws.client('ec2').describe_subnets(
        SubnetIds=[subnet_id]
    )['Subnets'][0]
    assert subnet['DefaultForAz']


def test_subnet_without_default_vpc(aws):
    ec2 = aws.client('ec2')
    vpc_id = ec2.describe_vpcs(
        Filters=[{'Name': 'is-default', 'Values': ['true']}]
    )['Vpcs'][0]['VpcId']
    for subnet in ec2.describe_subnets(
        Filters=[{'Name': 'vpc-id', 'Values': [vpc_id]}]
    )['Subnets']:
        ec2.delete_subnet(SubnetId=subnet['SubnetId'])
    ec2.delete_vpc(VpcId=vpc_id)
    with pytest.raises(EC2PushError, match='No default VPC'):
        EC2Push(aws, UploadConfig(image_name='x'))._resolve_subnet()


def test_subnet_instance_type_not_offered(aws, monkeypatch):
    pusher = EC2Push(aws, UploadConfig(image_name='x'))
    monkeypatch.setattr(
        pusher.ec2, 'describe_instance_type_offerings',
        lambda **arguments: {'InstanceTypeOfferings': []}
    )
    with pytest.raises(EC2PushError, match='offers instance type'):
        pusher._resolve_subnet()


def test_push_in_subnet_with_private_ip(aws, remote, raw_image):
    ec2 = aws.client('ec2')
    vpc_id = ec2.create_vpc(CidrBlock='10.0.0.0/16')['Vpc']['VpcId']
    subnet_id = ec2.create_subnet(
        VpcId=vpc_id, CidrBlock='10.0.1.0/24'
    )['Subnet']['SubnetId']
    config = UploadConfig(
        image_name='test-image', subnet_id=subnet_id, use_private_ip=True
    )
    pusher = EC2Push(aws, config)
    vpcs = []
    setup_security_group = pusher._setup_security_group

    def record_vpc():
        setup_security_group()
        vpcs.append(ec2.describe_security_groups(
            GroupIds=[pusher.security_group_id]
        )['SecurityGroups'][0]['VpcId'])
    pusher._setup_security_group = record_vpc
    pusher.push(read_image_file(raw_image))
    assert vpcs == [vpc_id]
    assert remote.instances[0].hostname.startswith('10.0.1.')
    assert leftovers(aws) == []


def test_find_volume_device_retry(aws, remote, raw_image, monkeypatch):
    lookups = []
    find_volume_device = remote.find_volume_device

    def appears_late(self, volume_id, device_name):
        lookups.append(device_name)
        if lookups.count(device_name) < 3:
            raise EC2PushError('not yet')
        return find_volume_device(self, volume_id, device_name)
    monkeypatch.setattr(remote, 'find_volume_device', appears_late)
    EC2Push(aws, UploadConfig(image_name='test-image')).push(
        read_image_file(raw_image)
    )
    assert lookups == ['/dev/sdf'] * 3 + ['/dev/sdg'] * 3


def test_find_volume_device_gives_up(aws, remote, raw_image, monkeypatch):
    def never(self, volume_id, device_name):
        raise EC2PushError('no device')
    monkeypatch.setattr(remote, 'find_volume_device', never)
    with pytest.raises(EC2PushError, match='no device'):
        EC2Push(aws, UploadConfig(image_name='test-image')).push(
            read_image_file(raw_image)
        )
    assert leftovers(aws) == []


def test_cleanup_security_group_retry(aws, remote, raw_image):
    pusher = EC2Push(aws, UploadConfig(image_name='test-image'))
    delete_security_group = pusher.ec2.delete_security_group
    attempts = []

    def in_use_once(**arguments):
        attempts.append(arguments['GroupId'])
        if len(attempts) == 1:
            raise ClientError(
                {'Error': {'Code': 'DependencyViolation', 'Message': 'used'}},
                'DeleteSecurityGroup'
            )
        return delete_security_group(**arguments)
    pusher.ec2.delete_security_group = in_use_once
    pusher.push(read_image_file(raw_image))
    assert len(attempts) == 2
    assert leftovers(aws) == []


def test_cleanup_continues_after_failure(aws, remote, raw_image, caplog):
    pusher = EC2Push(aws, UploadConfig(image_name='test-image'))

    def volume_in_use(**arguments):
        raise ClientError(
            {'Error': {'Code': 'VolumeInUse', 'Message': 'in use'}},
            'DeleteVolume'
        )
    pusher.ec2.delete_volume = volume_in_use
    pusher.push(read_image_file(raw_image))
    assert 'failed to delete vol-' in caplog.text
    # Security group and key pair are removed nevertheless
    ec2 = aws.client('ec2')
    created_by = [{'Name': 'tag:created-by', 'Values': ['ec2push']}]
    assert ec2.describe_security_groups(
        Filters=created_by
    )['SecurityGroups'] == []
    assert ec2.describe_key_pairs(Filters=created_by)['KeyPairs'] == []
    assert len(pusher.volumes) == 2


def test_wait_failure(aws, remote):
    pusher = EC2Push(aws, UploadConfig(image_name='x'))
    volume_id = pusher.ec2.create_volume(
        AvailabilityZone=f'{aws.region_name}a', Size=1
    )['VolumeId']
    with pytest.raises(EC2PushAWSError, match='volume_in_use'):
        pusher._wait('volume_in_use', VolumeIds=[volume_id])


def test_load_private_key_invalid(tmp_path):
    key_file = tmp_path / 'id_broken'
    key_file.write_text('not a key')
    with pytest.raises(EC2PushError, match='Unable to load private key'):
        load_private_key(key_file)


def test_snapshot_removed_if_registration_fails(aws, remote, raw_image):
    pusher = EC2Push(aws, UploadConfig(image_name='test-image'))

    def register_fails(**arguments):
        raise ClientError(
            {'Error': {'Code': 'InvalidParameter', 'Message': 'bad'}},
            'RegisterImage'
        )
    pusher.ec2.register_image = register_fails
    with pytest.raises(ClientError, match='bad'):
        pusher.push(read_image_file(raw_image))
    assert image_snapshots(aws) == []
    assert leftovers(aws) == []


def test_image_not_available(aws, remote, raw_image, monkeypatch):
    pusher = EC2Push(aws, UploadConfig(image_name='test-image'))
    wait = pusher._wait

    def image_never_available(name, config=None, **arguments):
        if name == 'image_available':
            raise EC2PushAWSError('timeout')
        return wait(name, config, **arguments)
    monkeypatch.setattr(pusher, '_wait', image_never_available)
    with pytest.raises(EC2PushAWSError, match='Image ami-.* got registered'):
        pusher.push(read_image_file(raw_image))
    # The snapshot belongs to the registered image and is kept
    assert len(image_snapshots(aws)) == 1

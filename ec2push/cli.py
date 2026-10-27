"""
Command line interface of ec2push

ec2push IMAGE_FILE --name NAME [OPTIONS]

  Upload the given raw disk image, optionally xz compressed, to
  AWS EC2 and register it as AMI named NAME. The id of the
  registered AMI is printed to stdout, all other messages are
  printed to stderr.

  By default the image is written into a new EBS snapshot with the
  EBS direct APIs, no EC2 instance is involved. With --method helper
  the image is uploaded through a helper instance which is launched
  from a public Amazon Linux 2023 AMI by default. The AMI is
  resolved from a public SSM parameter which exists in every
  region. The AWS credentials are taken from the given profile as
  configured in ~/.aws/config, without a profile the default
  credentials of boto3 are used.

  The data written to EBS is verified against the checksum of the
  raw disk image unless --no-verify is given.
"""
import logging
from enum import Enum
from pathlib import Path
from typing import Dict, List, NoReturn, Optional

import boto3
import typer
from botocore.exceptions import BotoCoreError, ClientError
from rich.console import Console
from rich.logging import RichHandler
from rich.progress import (
    BarColumn, DownloadColumn, Progress, TimeRemainingColumn,
    TransferSpeedColumn
)

from ec2push import __version__
from ec2push.ebsdirect import DEFAULT_THREADS
from ec2push.exceptions import EC2PushError
from ec2push.image import read_image_file
from ec2push.uploader import (
    DEFAULT_HELPER_AMI, DEFAULT_INSTANCE_TYPE, DEFAULT_SSH_USER,
    METHOD_DIRECT, METHOD_HELPER, EC2Push, UploadConfig
)

log = logging.getLogger('ec2push')
console = Console(stderr=True)

app = typer.Typer(
    help='Upload a raw disk image to AWS EC2 and register it as AMI',
    add_completion=False,
    no_args_is_help=True
)

# Help section of the options which only apply to --method helper
HELPER_PANEL = 'Helper instance options (--method helper)'


class UploadMethod(str, Enum):
    direct = METHOD_DIRECT
    helper = METHOD_HELPER


class Architecture(str, Enum):
    x86_64 = 'x86_64'
    arm64 = 'arm64'


class BootMode(str, Enum):
    legacy_bios = 'legacy-bios'
    uefi = 'uefi'
    uefi_preferred = 'uefi-preferred'


class VolumeType(str, Enum):
    gp3 = 'gp3'
    gp2 = 'gp2'
    io1 = 'io1'
    io2 = 'io2'
    standard = 'standard'


def fail(message: str) -> NoReturn:
    console.print(f'[red]Error: {message}[/red]', highlight=False)
    raise typer.Exit(1)


def parse_tags(tags: List[str]) -> Dict[str, str]:
    """
    Turn the given KEY=VALUE entries into a mapping
    """
    result = {}
    for tag in tags:
        key, separator, value = tag.partition('=')
        if not separator or not key:
            fail(f'Invalid tag "{tag}", expected KEY=VALUE')
        result[key] = value
    return result


def version_callback(value: bool) -> None:
    if value:
        typer.echo(f'ec2push {__version__}')
        raise typer.Exit()


@app.command()
def main(
    image_file: Path = typer.Argument(
        ..., help='Raw disk image file, optionally xz compressed'
    ),
    name: str = typer.Option(
        ..., '--name', '-n', help='Name of the AMI'
    ),
    description: Optional[str] = typer.Option(
        None, '--description', '-d', help='Description of the AMI'
    ),
    profile: Optional[str] = typer.Option(
        None, '--profile', '-p', envvar='AWS_PROFILE',
        help='AWS profile of the credentials'
    ),
    region: Optional[str] = typer.Option(
        None, '--region', '-r',
        help='AWS region, defaults to the region of the profile'
    ),
    arch: Architecture = typer.Option(
        Architecture.x86_64, '--arch', help='Architecture of the image'
    ),
    boot_mode: Optional[BootMode] = typer.Option(
        None, '--boot-mode', help='Boot mode of the AMI'
    ),
    ena: bool = typer.Option(
        True, '--ena/--no-ena', help='Enable ENA networking'
    ),
    sriov: bool = typer.Option(
        True, '--sriov/--no-sriov',
        help='Enable enhanced networking with the Intel 82599 VF'
    ),
    tpm: bool = typer.Option(
        False, '--tpm', help='Enable NitroTPM 2.0, requires uefi boot mode'
    ),
    imds_v2: bool = typer.Option(
        False, '--imds-v2', help='Require IMDSv2 for instances of the AMI'
    ),
    root_volume_size: Optional[int] = typer.Option(
        None, '--root-volume-size', min=1,
        help='Root volume size in GiB, defaults to the image size'
    ),
    volume_type: VolumeType = typer.Option(
        VolumeType.gp3, '--volume-type', help='Root volume type of the AMI'
    ),
    tag: List[str] = typer.Option(
        [], '--tag', '-t', metavar='KEY=VALUE',
        help='Tag of the AMI and its snapshot, can be repeated'
    ),
    method: UploadMethod = typer.Option(
        UploadMethod.direct, '--method',
        help='Write the snapshot with the EBS direct APIs or through a '
             'helper instance'
    ),
    upload_threads: int = typer.Option(
        DEFAULT_THREADS, '--upload-threads', min=1, max=64,
        help='Parallel block uploads of the direct method'
    ),
    verify: bool = typer.Option(
        True, '--verify/--no-verify',
        help='Verify the checksum of the data written to EBS'
    ),
    helper_ami: str = typer.Option(
        DEFAULT_HELPER_AMI, '--helper-ami',
        help='AMI id or SSM parameter name of the helper instance AMI',
        rich_help_panel=HELPER_PANEL
    ),
    instance_type: str = typer.Option(
        DEFAULT_INSTANCE_TYPE, '--instance-type',
        help='Instance type of the helper instance',
        rich_help_panel=HELPER_PANEL
    ),
    ssh_user: str = typer.Option(
        DEFAULT_SSH_USER, '--ssh-user',
        help='Login user of the helper instance',
        rich_help_panel=HELPER_PANEL
    ),
    ssh_key_name: Optional[str] = typer.Option(
        None, '--ssh-key-name',
        help='Existing EC2 key pair, a temporary one is created by default',
        rich_help_panel=HELPER_PANEL
    ),
    ssh_private_key: Optional[Path] = typer.Option(
        None, '--ssh-private-key', exists=True, dir_okay=False,
        help='Private key file of --ssh-key-name',
        rich_help_panel=HELPER_PANEL
    ),
    ssh_timeout: int = typer.Option(
        300, '--ssh-timeout', min=1,
        help='Seconds to wait for ssh access to the helper instance',
        rich_help_panel=HELPER_PANEL
    ),
    ssh_cidr: str = typer.Option(
        '0.0.0.0/0', '--ssh-cidr',
        help='Source address range of ssh access in the temporary '
             'security group',
        rich_help_panel=HELPER_PANEL
    ),
    subnet_id: Optional[str] = typer.Option(
        None, '--subnet-id',
        help='Subnet of the helper instance, defaults to a default subnet',
        rich_help_panel=HELPER_PANEL
    ),
    security_group_id: Optional[str] = typer.Option(
        None, '--security-group-id',
        help='Security group of the helper instance allowing ssh access, '
             'a temporary one is created by default',
        rich_help_panel=HELPER_PANEL
    ),
    private_ip: bool = typer.Option(
        False, '--private-ip',
        help='Connect to the private IP address of the helper instance',
        rich_help_panel=HELPER_PANEL
    ),
    verbose: bool = typer.Option(
        False, '--verbose', '-v', help='Show debug messages'
    ),
    version: bool = typer.Option(
        False, '--version', callback=version_callback, is_eager=True,
        help='Show the version and exit'
    ),
) -> None:
    """
    Upload a raw disk image to AWS EC2 and register it as AMI

    The id of the registered AMI is printed to stdout.
    """
    logging.basicConfig(
        level=logging.WARNING,
        format='%(message)s',
        handlers=[RichHandler(console=console, show_path=False)]
    )
    log.setLevel(logging.DEBUG if verbose else logging.INFO)

    if tpm and boot_mode != BootMode.uefi:
        fail('--tpm requires --boot-mode uefi')
    if bool(ssh_key_name) != bool(ssh_private_key):
        fail('--ssh-key-name and --ssh-private-key must be used together')

    config = UploadConfig(
        image_name=name,
        method=method.value,
        upload_threads=upload_threads,
        verify=verify,
        description=description,
        arch=arch.value,
        boot_mode=boot_mode.value if boot_mode else None,
        ena_support=ena,
        sriov_support=sriov,
        tpm_support=tpm,
        imds_v2=imds_v2,
        root_volume_size=root_volume_size,
        volume_type=volume_type.value,
        tags=parse_tags(tag),
        helper_ami=helper_ami,
        instance_type=instance_type,
        ssh_user=ssh_user,
        ssh_key_name=ssh_key_name,
        ssh_private_key=ssh_private_key,
        ssh_timeout=ssh_timeout,
        ssh_cidr=ssh_cidr,
        subnet_id=subnet_id,
        security_group_id=security_group_id,
        use_private_ip=private_ip
    )
    progress = Progress(
        '[progress.description]{task.description}', BarColumn(),
        DownloadColumn(), TransferSpeedColumn(), TimeRemainingColumn(),
        console=console, transient=True
    )
    upload_task = progress.add_task('Uploading', start=False, visible=False)

    def upload_progress(transferred: int, total: int) -> None:
        progress.start_task(upload_task)
        progress.update(
            upload_task, completed=transferred, total=total,
            visible=transferred < total
        )

    try:
        image = read_image_file(image_file)
        session = boto3.session.Session(
            profile_name=profile, region_name=region
        )
        uploader = EC2Push(session, config, upload_progress)
        with progress:
            image_id = uploader.push(image)
    except (EC2PushError, BotoCoreError, ClientError) as issue:
        fail(str(issue))
    except KeyboardInterrupt:
        fail('Interrupted')
    typer.echo(image_id)

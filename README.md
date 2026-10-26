# ec2push

Upload a raw disk image to AWS EC2 and register it as AMI.

ec2push is a rewrite of the upload procedure of `ec2uploadimg` from
[ec2imgutils](https://github.com/SUSE-Enceladus/ec2imgutils). It
needs no configuration file: everything except the AWS credentials
has a default or is passed on the command line.

## How it works

1. Launch a helper instance from a public Amazon Linux 2023 AMI
2. Create and attach two EBS volumes: a *storage* volume which
   receives the image file and the *target* volume which becomes the
   root volume of the AMI
3. Wait for ssh access to the helper instance
4. Create an ext4 filesystem on the storage volume and mount it
5. Upload the image file to the storage volume
6. Dump the raw disk image to the target volume, xz compressed images
   are decompressed on the fly
7. Detach the target volume and create a snapshot from it
8. Register an AMI from the snapshot

The helper instance, the volumes and, unless given on the command line,
a temporary key pair and security group are removed when ec2push ends,
also on failure or interruption. All temporary resources are tagged
`created-by=ec2push` to ease finding leftovers.

## Helper instance

By default the helper instance is a `t2.micro` launched from the
latest Amazon Linux 2023 AMI with the login user `ec2-user`. AMI ids
differ per region, therefore the AMI is looked up from the public SSM
parameter

    /aws/service/ami-amazon-linux-latest/al2023-ami-kernel-default-x86_64

which exists in every region. If a different `--instance-type` is
used, the Amazon Linux variant matching its architecture (`x86_64` or
`arm64`) is selected. The architecture of the helper instance is
independent of the architecture of the uploaded image.

Use `--helper-ami` to launch a different AMI, either by AMI id or by
SSM parameter name, and `--ssh-user` for its login user. The helper
AMI needs `bash`, `sudo`, `lsblk`, `mkfs.ext4`, `dd` and, for xz
compressed images, `xz`.

## Installation

    poetry install

## Usage

    ec2push IMAGE_FILE --name NAME [OPTIONS]

For example:

    ec2push --profile my-profile --region eu-central-1 \
        --name my-image-v1 --boot-mode uefi --imds-v2 \
        --tag project=demo \
        my-image.x86_64-1.0.0.raw.xz

The id of the registered AMI is printed to stdout, all other messages
go to stderr:

    AMI=$(ec2push -p my-profile -n my-image-v1 my-image.raw)

The AWS credentials are read from the given profile as configured in
`~/.aws/config` (or `AWS_PROFILE`). Without a profile the default
credential chain of boto3 is used. The region defaults to the region
of the profile.

Without `--subnet-id` the helper instance is launched in a default
subnet of the default VPC whose availability zone offers the instance
type. Without `--security-group-id` a temporary security group allowing
ssh from `--ssh-cidr` (default `0.0.0.0/0`) is created. Without
`--ssh-key-name`/`--ssh-private-key` a temporary ed25519 key pair is
created; its private key is generated locally and never written to
disk.

The root volume size of the AMI defaults to the size of the raw image
rounded up to the next GiB. For xz compressed images the size is read
from the xz index, which requires the `xz` tool locally; without it
use `--root-volume-size`.

Run `ec2push --help` for all options.

## Development

    poetry install
    poetry run pytest
    poetry run flake8 ec2push tests

The tests mock AWS with [moto](https://github.com/getmoto/moto) and
replace the ssh connection to the helper instance, they don't access
AWS.

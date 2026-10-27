#!/bin/bash

mkdir -p image

podman run \
    --privileged \
    --pull=newer \
    -v $HOME/.kiwi_boxes:/root/.kiwi_boxes \
    -v $PWD:/ec2push.kiwi \
    -v $PWD/image:/ec2push.oci \
    --rm \
    -it public.ecr.aws/b9k1j9y6/kiwi:latest \
    system boxbuild \
    --box tumbleweed \
    kiwi \
    --description /ec2push.kiwi \
    --target-dir /ec2push.oci

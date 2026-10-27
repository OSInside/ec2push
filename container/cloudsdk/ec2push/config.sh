#!/bin/bash

set -ex

git clone https://github.com/OSInside/ec2push.git

pushd ec2push

poetry install

cat > /usr/bin/ec2push <<'EOF'
#!/bin/bash
pushd /ec2push
poetry run ec2push $*
EOF

chmod u+x /usr/bin/ec2push

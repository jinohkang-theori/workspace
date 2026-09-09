#!/bin/sh
[ "$#" -gt 0 ] || set -- /bin/bash -i

cmd=
cmd=$cmd'[ -e /home/ubuntu ] || sudo useradd -u 1000 -g 1000 ubuntu || exit; '
cmd=$cmd'echo "ubuntu ALL=(ALL) NOPASSWD:ALL" > /etc/sudoers.d/99-ubuntu; '
cmd=$cmd'mountpoint -q /home/ubuntu || mount --bind /var/lib/docker/codespacemount/workspace/ubuntu /home/ubuntu; '
cmd=$cmd'exec systemd-run --quiet --pty --wait --collect --service-type=exec --setenv=TERM="$TERM" --uid=ubuntu --gid=ubuntu --working-directory=/home/ubuntu "$@"'

export DOCKER_HOST=unix:///var/run/docker-host.sock

image=$(docker container ls --filter=volume=/var/run/docker-host.sock --format='{{.Image}}')

exec docker \
    run --rm -it --name="guest2host-$$" --privileged \
    --pid=host --userns=host --uts=host \
    --ipc=host --cgroupns=host --network=host \
    -v /:/host "$image" \
    nsenter -FZ -t1 -a env TERM="$TERM" sh -ec "$cmd" - "$@"

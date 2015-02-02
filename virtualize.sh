#!/bin/bash
#
# Copyright (C) 2015 eNovance SAS <licensing@enovance.com>
#
# Author: Frederic Lepied <frederic.lepied@enovance.com>
#
# Licensed under the Apache License, Version 2.0 (the "License"); you may
# not use this file except in compliance with the License. You may obtain
# a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
# WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
# License for the specific language governing permissions and limitations
# under the License.

set -e
set -x

ORIG=$(cd $(dirname $0); pwd)
PREFIX=$USER

if [ $# != 1 ]; then
    echo "Usage: $0 <libvirt host>"
    echo
    echo "ex: $0 192.168.100.12"
    exit 1
fi

virthost=$1

# Default values if not set by user env
if [ -z "$TIMEOUT_ITERATION" ]; then
    TIMEOUT_ITERATION=150
fi

SSHOPTS="-oBatchMode=yes -oCheckHostIP=no -oHashKnownHosts=no  -oStrictHostKeyChecking=no -oPreferredAuthentications=publickey  -oChallengeResponseAuthentication=no -oKbdInteractiveDevices=no -oUserKnownHostsFile=/dev/null"

test_connectivity() {
    set +x
    local i=0
    local host_ip=$1
    while true; do
        echo -n "."
        ssh $SSHOPTS jenkins@$host_ip uname -a > /dev/null 2>&1 && break
        sleep 4
        i=$[i+1]
        if [[ $i -ge $TIMEOUT_ITERATION ]]; then
            echo "uname timeout on ${host_ip}..."
            return 1
        fi
    done
    echo "Node $host_name is alive !"
    return 0
}

if [ -n "$SSH_AUTH_SOCK" ]; then
    ssh-add -L > pubfile
    pubfile=pubfile
else
    pubfile=~/.ssh/id_rsa.pub
fi

$ORIG/virtualization/virtualizor.py virt_platform.yml $virthost --replace --prefix ${PREFIX} --public_network nat --replace --pub-key-file $pubfile
installserverip=$(ssh $SSHOPTS root@$virthost "awk '/ ${PREFIX}_/ {print \$3}' /var/lib/libvirt/dnsmasq/nat.leases")

retry=0
while ! rsync -e "ssh $SSHOPTS" --quiet -av --no-owner top/ root@$installserverip:/; do
    if [ $((retry++)) -gt 300 ]; then
        echo "reached max retries"
    else
        echo "install-server ($installserverip) not ready yet. waiting..."
    fi
    sleep 10
    echo -n .
done

set -x
scp $SSHOPTS extract-archive.sh functions root@$installserverip:/tmp

ssh $SSHOPTS root@$installserverip "echo -e 'RSERV=localhost\nRSERV_PORT=873' >> /var/lib/edeploy/conf"

ssh $SSHOPTS root@$installserverip /tmp/extract-archive.sh
ssh $SSHOPTS root@$installserverip rm /tmp/extract-archive.sh /tmp/functions
ssh $SSHOPTS root@$installserverip service dnsmasq restart
ssh $SSHOPTS root@$installserverip service httpd restart
ssh $SSHOPTS root@$installserverip service rsyncd restart

. top/etc/config-tools/config

JOBS=
declare -a assoc

for node in $HOSTS; do
    (
        echo "Testing $hostname"
        ip=$(${ORIG}/extract.py hosts.${node}.ip top/etc/config-tools/global.yml)
        test_connectivity $ip $node || exit 1
    ) &
    JOBS="$JOBS $!"
    assoc[$!]=$hostname
done

set -x
set +e

rc=0
for job in $JOBS; do
    wait $job
    ret=$?
    if [ $ret -eq 127 ]; then
        echo "$job doesn't exist anymore"
    elif [ $ret -ne 0 ]; then
        echo "${assoc[$job]} wasn't installed"
        rc=1
    fi
done

set -e

while curl --silent http://$installserverip:8282/job/puppet/build|\
        grep "Your browser will reload automatically when Jenkins is read"; do
    sleep 1;
done
#ssh $SSHOPTS -A root@$installserverip configure.sh

# virtualize.sh ends here

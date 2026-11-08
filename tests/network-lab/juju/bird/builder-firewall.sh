#!/bin/sh
set -eu
# Package downloads leave the nested builder through the VM management NIC.
# Keep Docker's default forwarding policy and permit only web egress plus replies.
test "$(hostname)" = fabric
test -f /etc/network-lab/versions.json
iptables -C DOCKER-USER -i build0 -o mgmt0 -s 10.203.251.0/24 ! -d 10.0.0.0/8 \
    -p tcp -m multiport --dports 80,443 -j ACCEPT 2>/dev/null ||
    iptables -I DOCKER-USER -i build0 -o mgmt0 -s 10.203.251.0/24 ! -d 10.0.0.0/8 \
        -p tcp -m multiport --dports 80,443 -j ACCEPT
iptables -C DOCKER-USER -i mgmt0 -o build0 -d 10.203.251.0/24 \
    -m conntrack --ctstate RELATED,ESTABLISHED -j ACCEPT 2>/dev/null ||
    iptables -I DOCKER-USER -i mgmt0 -o build0 -d 10.203.251.0/24 \
        -m conntrack --ctstate RELATED,ESTABLISHED -j ACCEPT

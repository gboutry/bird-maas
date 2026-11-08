#!/bin/sh
set -eu
ip route replace unreachable 10.205.0.0/16 metric 42760
if [ -n "${LAB_HOST_MAC:-}" ]; then
    # Containerlab sets the parent MAC/MTU while attaching the dataplane port.
    attempts=0
    until [ -e /sys/class/net/eth3/address ] \
        && [ "$(cat /sys/class/net/eth3/address)" = "$LAB_HOST_MAC" ] \
        && [ "$(cat /sys/class/net/eth3/mtu)" = "1500" ]; do
        attempts=$((attempts + 1))
        [ "$attempts" -le 100 ] || exit 1
        sleep 0.2
    done
    ip link set dev eth3 up
    for vid in 3400 3401 3402 3403 3404; do
        ip link add link eth3 name "eth3.$vid" type vlan id "$vid"
        ip link set dev "eth3.$vid" addrgenmode none
        ip -6 address add fe80::1/64 dev "eth3.$vid"
        ip link set dev "eth3.$vid" up
    done
    lldpd -I eth3 -C eth3 -k -x
fi
exec /usr/lib/frr/docker-start

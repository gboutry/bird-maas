FROM quay.io/frrouting/frr@sha256:e995beaa50fdc9edb35eadcfefa29b7f062cc06f2b812613789b68fa541554d2

COPY entrypoint.sh /usr/local/sbin/network-lab-frr
RUN apk add --no-cache lldpd=1.0.19-r0 \
    && chmod 0755 /usr/local/sbin/network-lab-frr \
    && mkdir -p /var/log/frr && chown frr:frr /var/log/frr \
    && touch /etc/frr/vtysh.conf
CMD ["/usr/local/sbin/network-lab-frr"]

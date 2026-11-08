FROM ubuntu@sha256:fe81560990fae86dc4743202b2cb4d3df98d0bcf949173cecef15ce1127ef852

ARG FRR_VERSION=8.4.4-1.1ubuntu6.8
RUN apt-get update && DEBIAN_FRONTEND=noninteractive apt-get install --yes --no-install-recommends \
    frr=${FRR_VERSION} iproute2 iputils-ping tcpdump ca-certificates \
    && dpkg-query -W > /usr/share/network-lab-packages.txt \
    && rm -rf /var/lib/apt/lists/*
COPY entrypoint.sh /usr/local/sbin/network-lab-frr
RUN chmod 0755 /usr/local/sbin/network-lab-frr
ENTRYPOINT ["/usr/local/sbin/network-lab-frr"]

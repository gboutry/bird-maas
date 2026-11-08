"""Pin the seven Terraform-owned, capped VM identities before deployment."""

from bootstrap import owned
from commission import HOSTS


def owned_infrastructure():
    specifications = {
        "maas": (4, "8GiB"),
        "fabric": (2, "8GiB"),
        "juju": (2, "4GiB"),
        **{name: (2, "4GiB") for name in HOSTS},
    }
    return {name: owned(name, cpu, memory) for name, (cpu, memory) in specifications.items()}

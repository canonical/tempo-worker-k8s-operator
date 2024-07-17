#!/usr/bin/env python3
# Copyright 2023 Canonical
# See LICENSE file for licensing details.

"""Tempo worker charm.
This charm deploys a Tempo worker application on k8s Juju models.

Integrate it with a `tempo-k8s` coordinator unit to start.
"""

import logging
from pathlib import Path
from typing import Optional

from charms.tempo_k8s.v1.charm_tracing import trace_charm
from ops.charm import CharmBase
from ops.main import main
from cosl.coordinated_workers.worker import CONFIG_FILE, Worker, CLIENT_CA_FILE
from ops.pebble import Layer


# Log messages can be retrieved using juju debug-log
logger = logging.getLogger(__name__)


@trace_charm(
    tracing_endpoint="tempo_endpoint",
    server_cert="ca_cert_path",
)
class TempoWorkerK8SOperatorCharm(CharmBase):
    """A Juju Charmed Operator for Tempo."""

    _name = "tempo"
    _instance_addr = "127.0.0.1"

    def __init__(self, *args):
        super().__init__(*args)

        # TODO take ports from tempo instead of using hardcoded ports set
        self.unit.set_ports(3200, 4317, 4318, 9411, 14268, 7946, 9096)

        self.worker = Worker(
            charm=self,
            name="tempo",
            pebble_layer=self.pebble_layer,
            endpoints={"cluster": "tempo-cluster"},
        )

    @property
    def tempo_endpoint(self) -> Optional[str]:
        """Tempo endpoint for charm tracing."""
        if endpoints := self.worker.cluster.get_tracing_receivers():
            return endpoints.get("otlp_http", None)

    @property
    def ca_cert_path(self) -> Optional[str]:
        """CA certificate path for tls tracing."""
        return CLIENT_CA_FILE if Path(CLIENT_CA_FILE).exists() else None

    def pebble_layer(self, worker: Worker) -> Layer:
        """Return a dictionary representing a Pebble layer.

        Caller is responsible for checking whether the tempo role is valid before
        calling this method.
        """
        role = "".join(worker.roles)
        if role == "all":
            role = "scalable-single-binary"
        return Layer(
            {
                "summary": "tempo worker layer",
                "description": "pebble config layer for tempo worker",
                "services": {
                    "tempo": {
                        "override": "replace",
                        "summary": "tempo worker process",
                        "command": f"/bin/tempo -config.file={CONFIG_FILE} -target {role}",
                        "startup": "enabled",
                    }
                },
            }
        )


if __name__ == "__main__":  # pragma: nocover
    main(TempoWorkerK8SOperatorCharm)

#!/usr/bin/env python3
# Copyright 2023 Canonical
# See LICENSE file for licensing details.

"""Tempo worker charm.
This charm deploys a Tempo worker application on k8s Juju models.

Integrate it with a `tempo-k8s` coordinator unit to start.
"""
import logging
from typing import Optional

from cosl.coordinated_workers.worker import CONFIG_FILE, Worker
from ops import CollectStatusEvent
from ops.charm import CharmBase
from ops.main import main
from ops.model import BlockedStatus
from ops.pebble import Layer

from charms.tempo_k8s.v1.charm_tracing import trace_charm

# Log messages can be retrieved using juju debug-log
logger = logging.getLogger(__name__)

CA_PATH = "/usr/local/share/ca-certificates/ca.crt"


class RolesConfigurationError(Exception):
    """Raised when the worker has an invalid role(s) set in its config."""


@trace_charm(
    tracing_endpoint="tempo_endpoint",
    server_cert="ca_cert_path",
    extra_types=[Worker],
)
class TempoWorkerK8SOperatorCharm(CharmBase):
    """A Juju Charmed Operator for Tempo."""

    _valid_roles = [
        "all",
        "querier",
        "query-frontend",
        "ingester",
        "distributor",
        "compactor",
        "metrics-generator",
    ]

    def __init__(self, *args):
        super().__init__(*args)

        # TODO take ports from tempo instead of using hardcoded ports set
        self.unit.set_ports(3200, 4317, 4318, 9411, 14268, 7946, 9096, 14250)

        self.worker = Worker(
            charm=self,
            name="tempo",
            pebble_layer=self.generate_worker_layer,
            endpoints={"cluster": "tempo-cluster"},  # type: ignore
            readiness_check_endpoint=self.readiness_check_endpoint,
        )
        self.framework.observe(self.on.collect_unit_status, self._on_collect_status)

    @property
    def tempo_endpoint(self) -> Optional[str]:
        """Tempo endpoint for charm tracing."""
        if endpoints := self.worker.cluster.get_tracing_receivers():
            return endpoints.get("otlp_http")

    @property
    def ca_cert_path(self) -> Optional[str]:
        """CA certificate path for tls tracing."""
        return CA_PATH

    @staticmethod
    def readiness_check_endpoint(worker: Worker) -> str:
        """Endpoint for worker readiness checks."""
        scheme = "https" if worker.cluster.get_tls_data() else "http"
        return f"{scheme}://localhost:3200/ready"

    def generate_worker_layer(self, worker: Worker) -> Layer:
        """Return the Pebble layer for the Worker.

        This method assumes that worker.roles is valid.
        """
        roles = worker.roles
        if not len(roles) == 1:
            raise RolesConfigurationError(
                f"Worker can only have 1 role configured. {len(roles)} found."
            )
        role = roles[0]
        if role not in self._valid_roles:
            raise RolesConfigurationError(
                f"Invalid role {role}. Should be one of {self._valid_roles}"
            )
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

    def _on_collect_status(self, e: CollectStatusEvent):
        # add Tempo worker custom blocking conditions
        if self.worker.roles and self.worker.roles[0] not in self._valid_roles:
            e.add_status(
                BlockedStatus(
                    f"Invalid `role` config value: {self.config.get('role')!r}. Should be one of {self._valid_roles}"
                )
            )


if __name__ == "__main__":  # pragma: nocover
    main(TempoWorkerK8SOperatorCharm)

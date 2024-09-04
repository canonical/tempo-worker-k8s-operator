#!/usr/bin/env python3
# Copyright 2023 Canonical
# See LICENSE file for licensing details.

"""Tempo worker charm.
This charm deploys a Tempo worker application on k8s Juju models.

Integrate it with a `tempo-k8s` coordinator unit to start.
"""
import socket
import logging
from typing import Optional, Dict, Any

from cosl.coordinated_workers.worker import CONFIG_FILE, Worker
from ops import CollectStatusEvent
from ops.charm import CharmBase
from ops.main import main
from ops.model import BlockedStatus, ActiveStatus
from ops.pebble import Layer

from charms.tempo_k8s.v1.charm_tracing import trace_charm


# Log messages can be retrieved using juju debug-log
logger = logging.getLogger(__name__)

CA_PATH = "/usr/local/share/ca-certificates/ca.crt"


class RolesConfigurationError(Exception):
    """Raised when the worker has an invalid role(s) set in its config."""


class TempoWorker(Worker):
    """A Tempo worker class that inherits from the Worker class."""

    @property
    def _worker_config(self) -> Dict[str, Any]:
        """Override property to add unit-specific configurations, like juju topology."""
        config = super()._worker_config
        if "all" in self.roles or "metrics-generator" in self.roles:
            config = self._add_juju_topolgy(config)

        return config

    def _add_juju_topolgy(self, config: Dict[str, Any]):
        """Modify the worker config to add juju topology for `metrics-generator`'s generated metrics."""
        # if `metrics_generator` doesn't exist in config,
        # then it is not enabled, so no point of adding juju topology.
        if "metrics_generator" in config:
            if "registry" not in config["metrics_generator"]:
                config["metrics_generator"]["registry"] = {}
            config["metrics_generator"]["registry"]["external_labels"] = dict(
                self.cluster.juju_topology.as_dict()
            )

        return config


@trace_charm(
    tracing_endpoint="tempo_endpoint",
    server_cert="ca_cert_path",
    extra_types=[Worker],
)
class TempoWorkerK8SOperatorCharm(CharmBase):
    """A Juju Charmed Operator for Tempo."""

    def __init__(self, *args):
        super().__init__(*args)

        # TODO take ports from tempo instead of using hardcoded ports set
        self.unit.set_ports(3200, 4317, 4318, 9411, 14268, 7946, 9096, 14250)
        self.framework.observe(self.on.collect_unit_status, self._on_collect_status)
        self.worker = TempoWorker(
            charm=self,
            name="tempo",
            pebble_layer=self.generate_worker_layer,
            endpoints={"cluster": "tempo-cluster"},  # type: ignore
            readiness_check_endpoint=self.readiness_check_endpoint,
            resources_requests=self.get_resources_requests,
            container_name="tempo",
        )

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
        return f"{scheme}://{socket.getfqdn()}:3200/ready"

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

    def get_resources_requests(self, _) -> Dict[str, str]:
        """Returns a dictionary for the "requests" portion of the resources requirements."""
        return {"cpu": "50m", "memory": "200Mi"}

    def _on_collect_status(self, e: CollectStatusEvent):
        # add Tempo worker-specific statuses
        roles = self.worker.roles
        if roles and len(roles) > 1:
            e.add_status(BlockedStatus(f"cannot have more than 1 enabled role: {roles}"))
        if (
            roles
            and self.worker.cluster.relation
            and not self.worker.cluster.get_remote_write_endpoints()
        ):
            if "all" in roles:
                e.add_status(
                    ActiveStatus(
                        "metrics-generator disabled. No prometheus remote-write relation configured on the coordinator"
                    )
                )
            elif "metrics-generator" in roles:
                e.add_status(
                    BlockedStatus(
                        "No prometheus remote-write relation configured on the coordinator"
                    )
                )


if __name__ == "__main__":  # pragma: nocover
    main(TempoWorkerK8SOperatorCharm)

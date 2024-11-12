#!/usr/bin/env python3
# Copyright 2023 Canonical
# See LICENSE file for licensing details.

"""Tempo worker charm.
This charm deploys a Tempo worker application on k8s Juju models.

Integrate it with a `tempo-k8s` coordinator unit to start.
"""
import socket
import logging
from typing import Dict, Any

import tenacity
from cosl.coordinated_workers.worker import CONFIG_FILE, Worker
from ops import CollectStatusEvent
from ops.charm import CharmBase
from ops.main import main
from ops.model import BlockedStatus, ActiveStatus
from ops.pebble import Layer

from charms.tempo_coordinator_k8s.v0.charm_tracing import trace_charm


# Log messages can be retrieved using juju debug-log
logger = logging.getLogger(__name__)

CA_PATH = "/usr/local/share/ca-certificates/ca.crt"


class RolesConfigurationError(Exception):
    """Raised when the worker has an invalid role(s) set in its config."""


class TempoWorker(Worker):
    """A Tempo worker class that inherits from the Worker class."""

    SERVICE_START_RETRY_STOP = tenacity.stop_after_delay(60)
    SERVICE_START_RETRY_WAIT = tenacity.wait_fixed(5)

    @property
    def _worker_config(self) -> Dict[str, Any]:
        """Override property to add unit-specific configurations, like juju topology."""
        config = super()._worker_config
        if "all" in self.roles or "metrics-generator" in self.roles:
            config = self._add_juju_topology(config)

        return config

    def _add_juju_topology(self, config: Dict[str, Any]):
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


class MetricsGeneratorStoragePathMissing(RuntimeError):
    """Raised if the worker node is a metrics-generator and we are missing some config."""


@trace_charm(
    tracing_endpoint="_charm_tracing_endpoint",
    server_cert="_charm_tracing_cert",
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
        self._charm_tracing_endpoint, self._charm_tracing_cert = self.worker.charm_tracing_config()

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
        elif role == "metrics-generator":
            # verify we have a metrics storage path configured, else
            # Tempo will fail to start with a bad error.
            if not worker.cluster.get_remote_write_endpoints():
                logger.error(
                    "cannot start this metrics-generator node without remote-write endpoints."
                    "Please relate the coordinator with a prometheus instance."
                )
                # this will tell the Worker that something is wrong and this node can't be started
                # update-status will inform the user of what's going on
                raise MetricsGeneratorStoragePathMissing()
        tempo_endpoint = worker.cluster.get_workload_tracing_receivers().get("jaeger_thrift_http", None)
        topology = worker.cluster.juju_topology
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
                        # Configure Tempo workload traces
                        "environment": {
                            # TODO: Future Tempo versions would be using otlp, so use these env variables instead.
                            # "OTEL_TRACES_EXPORTER": "otlp",
                            # "OTEL_EXPORTER_OTLP_TRACES_ENDPOINT": (
                            #     f"{tempo_endpoint}/v1/traces" if tempo_endpoint else ""
                            # ),
                            "OTEL_EXPORTER_JAEGER_ENDPOINT": (
                                f"{tempo_endpoint}/api/traces?format=jaeger.thrift"
                                if tempo_endpoint
                                else ""
                            ),
                            "OTEL_RESOURCE_ATTRIBUTES": f"juju_application={topology.application},juju_model={topology.model}"
                            + f",juju_model_uuid={topology.model_uuid},juju_unit={topology.unit},juju_charm={topology.charm_name}",
                        },
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

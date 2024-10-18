#!/usr/bin/env python3
# Copyright 2023 Canonical
# See LICENSE file for licensing details.

"""Tempo worker charm.
This charm deploys a Tempo worker application on k8s Juju models.

Integrate it with a `tempo-k8s` coordinator unit to start.
"""
import socket
import logging
import ssl
from typing import Optional, Dict, Any
from urllib.error import HTTPError
from urllib.request import urlopen

import tenacity
from cosl.coordinated_workers.worker import CONFIG_FILE, Worker, ServiceEndpointStatus, WorkerError
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

    def check_readiness(self) -> ServiceEndpointStatus:
        """If the user has configured a readiness check endpoint, GET it and check the workload status."""
        check_endpoint = self._readiness_check_endpoint
        if not check_endpoint:
            raise WorkerError(
                "cannot check readiness without a readiness_check_endpoint configured. "
                "Pass one to Worker on __init__."
            )

        try:
            ctx = ssl.create_default_context()
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE

            with urlopen(check_endpoint(self), context=ctx) as response:
                html: bytes = response.read()

            # ready response should simply be a string:
            #   "ready"
            raw_out = html.decode("utf-8").strip()
            if raw_out == "ready":
                return ServiceEndpointStatus.up

            # depending on the workload, we get something like:
            #   Some services are not Running:
            #   Starting: 1
            #   Running: 16
            # (tempo)
            #   Ingester not ready: waiting for 15s after being ready
            # (mimir)

            # anything that isn't 'ready' but also is a 2xx response will be interpreted as:
            # we're not ready yet, but we're working on it.
            logger.debug(f"GET {check_endpoint} returned: {raw_out!r}.")
            return ServiceEndpointStatus.starting

        except HTTPError:
            logger.debug("Error getting readiness endpoint: server not up (yet)")
        except Exception:
            logger.exception("Unexpected exception getting readiness endpoint")
        return ServiceEndpointStatus.down


class MetricsGeneratorStoragePathMissing(RuntimeError):
    """Raised if the worker node is a metrics-generator and we are missing some config."""


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
        elif role == "metrics-generator":
            # verify we have a metrics storage path configured, else
            # Tempo will fail to start with a bad error.
            if not self.worker.cluster.get_remote_write_endpoints():
                logger.error(
                    "cannot start this metrics-generator node without remote-write endpoints."
                    "Please relate the coordinator with a prometheus instance."
                )
                # this will tell the Worker that something is wrong and this node can't be started
                # update-status will inform the user of what's going on
                raise MetricsGeneratorStoragePathMissing()

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

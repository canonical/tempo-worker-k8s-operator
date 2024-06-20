#!/usr/bin/env python3
# Copyright 2024 Canonical
# See LICENSE file for licensing details.

"""This module contains an endpoint wrapper class for the requirer side of the ``tempo-cluster`` relation.

As this relation is cluster-internal and not intended for third-party charms to interact with
`tempo-coordinator-k8s`, its only user will be the tempo-worker-k8s charm. As such,
it does not live in a charm lib as most other relation endpoint wrappers do.
"""

import json
import logging
from enum import Enum, unique
from typing import Any, Dict, MutableMapping, Optional, Tuple, cast
from urllib.parse import urlparse

import ops
import pydantic

# The only reason we need the tracing lib is this enum. Not super nice.
from charms.tempo_k8s.v2.tracing import ReceiverProtocol
from ops import EventSource, Object, ObjectEvents, RelationCreatedEvent
from pydantic import BaseModel, ConfigDict

log = logging.getLogger("tempo_cluster")

DEFAULT_ENDPOINT_NAME = "tempo-cluster"
BUILTIN_JUJU_KEYS = {"ingress-address", "private-address", "egress-subnets"}


# TODO: inherit enum.StrEnum when jammy is no longer supported.
# https://docs.python.org/3/library/enum.html#enum.StrEnum
@unique
class TempoRole(str, Enum):
    """Tempo component role names.

    References:
     arch:
      -> https://grafana.com/docs/tempo/latest/operations/architecture/
     config:
      -> https://grafana.com/docs/tempo/latest/configuration/#server
    """

    scaling_monolithic = "scalable-single-binary"  # default
    querier = "querier"
    query_frontend = "query-frontend"
    ingester = "ingester"
    distributor = "distributor"
    compactor = "compactor"
    metrics_generator = "metrics-generator"
    all = "all"


class ConfigReceivedEvent(ops.EventBase):
    """Event emitted when the "tempo-cluster" provider has shared a new tempo config."""

    config: Dict[str, Any]
    """The tempo config."""

    def __init__(self, handle: ops.framework.Handle, config: Dict[str, Any]):
        super().__init__(handle)
        self.config = config

    def snapshot(self) -> Dict[str, Any]:
        """Used by the framework to serialize the event to disk.

        Not meant to be called by charm code.
        """
        return {"config": json.dumps(self.config)}

    def restore(self, snapshot: Dict[str, Any]):
        """Used by the framework to deserialize the event from disk.

        Not meant to be called by charm code.
        """
        self.relation = json.loads(snapshot["config"])  # noqa


class TempoClusterError(Exception):
    """Base class for exceptions raised by this module."""


class DataValidationError(TempoClusterError):
    """Raised when relation databag validation fails."""


class DatabagAccessPermissionError(TempoClusterError):
    """Raised when a follower attempts to write leader settings."""


class JujuTopology(pydantic.BaseModel):
    """JujuTopology."""

    model: str
    unit: str
    # ...


class DatabagModel(BaseModel):
    """Base databag model."""

    model_config = ConfigDict(
        # Allow instantiating this class by field name (instead of forcing alias).
        populate_by_name=True,
        # Custom config key: whether to nest the whole datastructure (as json)
        # under a field or spread it out at the toplevel.
        _NEST_UNDER=None,  # noqa
    )  # type: ignore
    """Pydantic config."""

    @classmethod
    def load(cls, databag: MutableMapping[str, str]):
        """Load this model from a Juju databag."""
        nest_under = cls.model_config.get("_NEST_UNDER")  # noqa
        if nest_under:
            return cls.parse_obj(json.loads(databag[cast(str, nest_under)]))

        try:
            data = {k: json.loads(v) for k, v in databag.items() if k not in BUILTIN_JUJU_KEYS}
        except json.JSONDecodeError as e:
            msg = f"invalid databag contents: expecting json. {databag}"
            log.info(msg)
            raise DataValidationError(msg) from e

        try:
            return cls.parse_raw(json.dumps(data))  # type: ignore
        except pydantic.ValidationError as e:
            msg = f"failed to validate databag: {databag}"
            log.info(msg, exc_info=True)
            raise DataValidationError(msg) from e

    def dump(self, databag: Optional[MutableMapping[str, str]] = None, clear: bool = True):
        """Write the contents of this model to Juju databag.

        :param databag: the databag to write the data to.
        :param clear: ensure the databag is cleared before writing it.
        """
        if clear and databag:
            databag.clear()

        if databag is None:
            databag = {}
        nest_under = self.model_config.get("_NEST_UNDER")  # noqa
        if nest_under:
            databag[nest_under] = self.json()

        dct = self.model_dump(by_alias=True)
        for key, field in self.model_fields.items():  # type: ignore
            value = dct[key]
            databag[field.alias or key] = json.dumps(value)
        return databag


class TempoClusterRequirerAppData(DatabagModel):
    """TempoClusterRequirerAppData."""

    role: TempoRole


class TempoClusterRequirerUnitData(DatabagModel):
    """TempoClusterRequirerUnitData."""

    juju_topology: JujuTopology
    address: str


class TempoClusterProviderAppData(DatabagModel):
    """TempoClusterProviderAppData."""

    tempo_config: Dict[str, Any]
    loki_endpoints: Optional[Dict[str, str]] = None
    tempo_receiver: Optional[Dict[ReceiverProtocol, str]] = None


class TempoClusterRemovedEvent(ops.EventBase):
    """Event emitted when the relation with the "tempo-cluster" provider has been severed.

    Or when the relation data has been wiped.
    """


class TempoClusterRequirerEvents(ObjectEvents):
    """Events emitted by the TempoClusterRequirer "tempo-cluster" endpoint wrapper."""

    config_received = EventSource(ConfigReceivedEvent)
    created = EventSource(RelationCreatedEvent)
    removed = EventSource(TempoClusterRemovedEvent)


class TempoClusterRequirer(Object):
    """``tempo-cluster`` requirer endpoint wrapper."""

    on = TempoClusterRequirerEvents()  # type: ignore

    def __init__(
        self,
        charm: ops.CharmBase,
        key: Optional[str] = None,
        endpoint: str = DEFAULT_ENDPOINT_NAME,
    ):
        super().__init__(charm, key or endpoint)
        self._charm = charm
        self.juju_topology = {"unit": self.model.unit.name, "model": self.model.name}
        relation = self.model.get_relation(endpoint)
        # filter out common unhappy relation states
        self.relation: Optional[ops.Relation] = (
            relation if relation and relation.app and relation.data else None
        )

        self.framework.observe(
            self._charm.on[endpoint].relation_changed, self._on_tempo_cluster_relation_changed
        )
        self.framework.observe(
            self._charm.on[endpoint].relation_created, self._on_tempo_cluster_relation_created
        )
        self.framework.observe(
            self._charm.on[endpoint].relation_broken, self._on_tempo_cluster_relation_broken
        )

    def _on_tempo_cluster_relation_broken(self, _event: ops.RelationBrokenEvent):
        self.on.removed.emit()

    def _on_tempo_cluster_relation_created(self, event: ops.RelationCreatedEvent):
        self.on.created.emit(relation=event.relation, app=event.app, unit=event.unit)

    def _on_tempo_cluster_relation_changed(self, _event: ops.RelationChangedEvent):
        # to prevent the event from firing if the relation is in an unhealthy state (breaking...)
        if self.relation:
            new_config = self.get_tempo_config()
            if new_config:
                self.on.config_received.emit(new_config)

            # if we have published our data, but we receive an empty/invalid config,
            # then the remote end must have removed it.
            elif self.is_published():
                self.on.removed.emit()

    def is_published(self):
        """Verify that the local side has done all they need to do.

        - unit address is published
        - roles are published
        """
        relation = self.relation
        if not relation:
            return False

        unit_data = relation.data[self._charm.unit]
        app_data = relation.data[self._charm.app]

        try:
            TempoClusterRequirerUnitData.load(unit_data)
            TempoClusterRequirerAppData.load(app_data)
        except DataValidationError as e:
            log.info(f"invalid databag contents: {e}")
            return False
        return True

    def publish_unit_address(self, url: str):
        """Publish this unit's URL via the unit databag."""
        try:
            urlparse(url)
        except Exception as e:
            raise ValueError(f"{url} is an invalid url") from e

        databag_model = TempoClusterRequirerUnitData(
            juju_topology=self.juju_topology,  # type: ignore
            address=url,
        )
        relation = self.relation
        if relation:
            unit_databag = relation.data[self.model.unit]  # type: ignore # all checks are done in __init__
            databag_model.dump(unit_databag)

    def publish_app_role(self, role: TempoRole):
        """Publish this application's role via the application databag."""
        if not self._charm.unit.is_leader():
            raise DatabagAccessPermissionError("only the leader unit can publish role.")

        relation = self.relation
        if relation:
            databag_model = TempoClusterRequirerAppData(role=role)
            databag_model.dump(relation.data[self.model.app])

    def _get_data_from_coordinator(self) -> Optional[TempoClusterProviderAppData]:
        """Fetch the contents of the doordinator databag."""
        data: Optional[TempoClusterProviderAppData] = None
        relation = self.relation
        if relation:
            try:
                databag = relation.data[relation.app]  # type: ignore # all checks are done in __init__
                coordinator_databag = TempoClusterProviderAppData.load(databag)
                data = coordinator_databag
            except DataValidationError as e:
                log.info(f"invalid databag contents: {e}")

        return data

    def get_tracing_endpoint(self, protocol: ReceiverProtocol) -> Optional[str]:
        """Get the coordinator's receiver endpoint from the coordinator databag."""
        data = self._get_data_from_coordinator()
        if data and data.tempo_receiver:
            return data.tempo_receiver.get(protocol)

    def get_tempo_config(self) -> Dict[str, Any]:
        """Fetch the tempo config from the coordinator databag."""
        data = self._get_data_from_coordinator()
        if data:
            return data.tempo_config
        return {}

    def get_loki_endpoints(self) -> Dict[str, str]:
        """Fetch the loki endpoints from the coordinator databag."""
        data = self._get_data_from_coordinator()
        if data:
            return data.loki_endpoints or {}
        return {}

    def get_cert_secret_ids(self) -> Optional[str]:
        """Fetch certificates secrets ids for the tempo config."""
        if self.relation and self.relation.app:
            return self.relation.data[self.relation.app].get("secrets", None)

    def cert_secrets_ready(self):
        """Check if cert secrets are ready."""
        return self.get_cert_secret_ids() is not None

    def get_privkey(self) -> Optional[str]:
        """Get the private key from secret."""
        secret_ids = self.get_cert_secret_ids()
        if not secret_ids:
            return None

        cert_secrets = json.loads(secret_ids)
        private_key_secret = self.model.get_secret(id=cert_secrets["private_key_secret_id"])
        return private_key_secret.get_content().get("private-key")

    def get_ca_and_server_certs(self) -> Tuple[Optional[str], Optional[str]]:
        """Get the ca and server certs from secret."""
        secret_ids = self.get_cert_secret_ids()
        if not secret_ids:
            return None, None

        cert_secrets = json.loads(secret_ids)
        ca_server_secret = self.model.get_secret(id=cert_secrets["ca_server_cert_secret_id"])
        ca_cert = ca_server_secret.get_content().get("ca-cert")
        server_cert = ca_server_secret.get_content().get("server-cert")
        return ca_cert, server_cert

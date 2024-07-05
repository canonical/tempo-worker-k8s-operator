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
from typing import Any, Dict, MutableMapping, Optional, Tuple
from urllib.parse import urlparse

from cosl import JujuTopology
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

    Note that in the distributed tempo configuration, the `all` role translates into `scalable-single-binary`,
    effectively hiding the original Tempo `all` role (meaning non-scalable single monolithic binary). This choice was
    made to keep design compatibility with other distributed observability charms (mimir, loki).
    """

    # scalable-single-binary is a bit too long to type
    all = "all"  # default, meta-role.

    querier = "querier"
    query_frontend = "query-frontend"
    ingester = "ingester"
    distributor = "distributor"
    compactor = "compactor"
    metrics_generator = "metrics-generator"

    @property
    def all_nonmeta(self):
        return (
            TempoRole.querier,
            TempoRole.query_frontend,
            TempoRole.ingester,
            TempoRole.distributor,
            TempoRole.compactor,
            TempoRole.metrics_generator,
        )


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


class _JujuTopologyModel(pydantic.BaseModel):
    """_JujuTopologyModel."""

    model: str
    model_uuid: str
    application: str
    charm_name: str
    unit: str


# DatabagModel implementation from traefik.v1.ingress charm lib.
PYDANTIC_IS_V1 = int(pydantic.version.VERSION.split(".")[0]) < 2
if PYDANTIC_IS_V1:

    class DatabagModel(BaseModel):  # type: ignore
        """Base databag model."""

        class Config:
            """Pydantic config."""

            allow_population_by_field_name = True
            """Allow instantiating this class by field name (instead of forcing alias)."""

        _NEST_UNDER = None

        @classmethod
        def load(cls, databag: MutableMapping):
            """Load this model from a Juju databag."""
            if cls._NEST_UNDER:
                return cls.parse_obj(json.loads(databag[cls._NEST_UNDER]))

            try:
                data = {
                    k: json.loads(v)
                    for k, v in databag.items()
                    # Don't attempt to parse model-external values
                    if k in {f.alias for f in cls.__fields__.values()}  # type: ignore
                }
            except json.JSONDecodeError as e:
                msg = f"invalid databag contents: expecting json. {databag}"
                log.error(msg)
                raise DataValidationError(msg) from e

            try:
                return cls.parse_raw(json.dumps(data))  # type: ignore
            except pydantic.ValidationError as e:
                msg = f"failed to validate databag: {databag}"
                log.debug(msg, exc_info=True)
                raise DataValidationError(msg) from e

        def dump(self, databag: Optional[MutableMapping] = None, clear: bool = True):
            """Write the contents of this model to Juju databag.

            :param databag: the databag to write the data to.
            :param clear: ensure the databag is cleared before writing it.
            """
            if clear and databag:
                databag.clear()

            if databag is None:
                databag = {}

            if self._NEST_UNDER:
                databag[self._NEST_UNDER] = self.json(by_alias=True, exclude_defaults=True)
                return databag

            for key, value in self.dict(by_alias=True, exclude_defaults=True).items():  # type: ignore
                databag[key] = json.dumps(value)

            return databag

else:
    from pydantic import ConfigDict

    class DatabagModel(BaseModel):
        """Base databag model."""

        model_config = ConfigDict(
            # tolerate additional keys in databag
            extra="ignore",
            # Allow instantiating this class by field name (instead of forcing alias).
            populate_by_name=True,
            # Custom config key: whether to nest the whole datastructure (as json)
            # under a field or spread it out at the toplevel.
            _NEST_UNDER=None,
        )  # type: ignore
        """Pydantic config."""

        @classmethod
        def load(cls, databag: MutableMapping):
            """Load this model from a Juju databag."""
            nest_under = cls.model_config.get("_NEST_UNDER")
            if nest_under:
                return cls.model_validate(json.loads(databag[nest_under]))  # type: ignore

            try:
                data = {
                    k: json.loads(v)
                    for k, v in databag.items()
                    # Don't attempt to parse model-external values
                    if k in {(f.alias or n) for n, f in cls.__fields__.items()}  # type: ignore
                }
            except json.JSONDecodeError as e:
                msg = f"invalid databag contents: expecting json. {databag}"
                log.error(msg)
                raise DataValidationError(msg) from e

            try:
                return cls.model_validate_json(json.dumps(data))  # type: ignore
            except pydantic.ValidationError as e:
                msg = f"failed to validate databag: {databag}"
                log.debug(msg, exc_info=True)
                raise DataValidationError(msg) from e

        def dump(self, databag: Optional[MutableMapping] = None, clear: bool = True):
            """Write the contents of this model to Juju databag.

            :param databag: the databag to write the data to.
            :param clear: ensure the databag is cleared before writing it.
            """
            if clear and databag:
                databag.clear()

            if databag is None:
                databag = {}
            nest_under = self.model_config.get("_NEST_UNDER")
            if nest_under:
                databag[nest_under] = self.model_dump_json(  # type: ignore
                    by_alias=True,
                    # skip keys whose values are default
                    exclude_defaults=True,
                )
                return databag

            dct = self.model_dump(mode="json", by_alias=True, exclude_defaults=True)  # type: ignore
            databag.update({k: json.dumps(v) for k, v in dct.items()})
            return databag


class TempoClusterRequirerAppData(DatabagModel):
    """TempoClusterRequirerAppData."""

    role: TempoRole


class TempoClusterRequirerUnitData(DatabagModel):
    """TempoClusterRequirerUnitData."""

    juju_topology: _JujuTopologyModel
    address: str


class TempoClusterProviderAppData(DatabagModel):
    """TempoClusterProviderAppData."""

    tempo_config: Dict[str, Any]
    loki_endpoints: Optional[Dict[str, str]] = None
    ca_cert: Optional[str] = None
    server_cert: Optional[str] = None
    privkey_secret_id: Optional[str] = None
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
        self.juju_topology = JujuTopology.from_charm(self._charm)
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
            juju_topology=dict(self.juju_topology.as_dict()),  # type: ignore
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

    def get_ca_and_server_certs(self) -> Tuple[Optional[str], Optional[str]]:
        """Fetch CA and server certificates."""
        data = self._get_data_from_coordinator()
        if data:
            return data.ca_cert, data.server_cert
        return None, None

    def get_privkey(self) -> Optional[str]:
        """Fetch private key."""
        data = self._get_data_from_coordinator()
        if data:
            secret_id = data.privkey_secret_id
            if not secret_id:
                return None
            secret = self.model.get_secret(id=secret_id)
            return secret.get_content().get("private-key")

        return

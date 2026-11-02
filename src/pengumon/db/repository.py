"""The database repository: one object, composed from a mixin per domain. This file holds no SQL; a new query goes in
the domain's own module (see the guard test in tests/test_repository_composition.py)."""

from pengumon.db.clients import ClientQueries
from pengumon.db.core import RepositoryCore
from pengumon.db.devices import DeviceQueries
from pengumon.db.incidents import IncidentQueries
from pengumon.db.path import PathQueries
from pengumon.db.probes import ProbeQueries
from pengumon.db.targets import TargetQueries


class DatabaseRepository(
    ClientQueries, DeviceQueries, IncidentQueries, PathQueries, TargetQueries, ProbeQueries, RepositoryCore
):
    """Every method callers use is defined in one of the mixins; the shared connections and caches are RepositoryCore's."""

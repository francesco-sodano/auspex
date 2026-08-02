"""Lazily-initialized shared clients — one instance per Function App cold start."""
import os
from typing import Optional

from .bronze_writer import BronzeWriter
from .control_plane import CosmosControlPlane

_cp: Optional[CosmosControlPlane] = None
_bw: Optional[BronzeWriter] = None


def get_control_plane() -> CosmosControlPlane:
    global _cp
    if _cp is None:
        _cp = CosmosControlPlane(os.environ["COSMOS_ENDPOINT"])
    return _cp


def get_bronze_writer() -> BronzeWriter:
    global _bw
    if _bw is None:
        control_plane = get_control_plane()
        _bw = BronzeWriter(
            workspace_id=os.environ["ONELAKE_WORKSPACE_ID"],
            lakehouse_name=os.environ.get("ONELAKE_LAKEHOUSE_NAME", "auspex_bronze"),
            universe_container=control_plane.container(
                os.environ.get("INGESTION_UNIVERSE_CONTAINER", "ingestion_universe")
            ),
        )
    return _bw

"""Enables ``python -m auspex <command>`` (arc42 §6.1, §7 IaC job commands).

The Container Apps Job definitions in ``infra/modules/containerapps.bicep``
invoke the pipeline and performance jobs as ``python -m auspex nightly`` /
``python -m auspex performance``; this module is what makes ``-m auspex``
resolve to the same CLI as the ``auspex`` console script.
"""

from __future__ import annotations

import sys

from auspex.cli.main import main

if __name__ == "__main__":
    sys.exit(main())

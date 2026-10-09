"""Run the hospital UI: ``python -m hacknc.ui`` (listens on port 8000)."""

from __future__ import annotations

import os

import uvicorn

from hacknc.common.config import get_settings
from hacknc.common.telemetry import setup_tracing
from hacknc.ui.app import build_dependencies, create_app
from hacknc.ui.settings import UiSettings


def main() -> None:
    settings = get_settings()
    setup_tracing("hospital-ui", settings.otel_exporter_otlp_endpoint)
    app = create_app(build_dependencies(settings, UiSettings()))
    uvicorn.run(app, host=os.environ.get("UI_HOST", "0.0.0.0"), port=8000)


if __name__ == "__main__":
    main()

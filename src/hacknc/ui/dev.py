"""Factory for ``uvicorn --factory --reload`` during UI development (``make ui-dev``)."""

from __future__ import annotations

from fastapi import FastAPI

from hacknc.common.config import get_settings
from hacknc.ui.app import build_dependencies, create_app
from hacknc.ui.settings import UiSettings


def app() -> FastAPI:
    return create_app(build_dependencies(get_settings(), UiSettings()))

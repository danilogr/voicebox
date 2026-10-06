"""This plugin's telemetry session for one process: the install brought up to date
by `wiring.start_install`, then this plugin's own `sessions` row. Shared by
`__main__` and `talk`, which read the real environment and pass it in."""

import sys
import uuid
from collections.abc import Mapping

from speak_core.telemetry import store, wiring

from speak.settings import Settings
from speak.telemetry.contract import IDENTITY, SCHEMA, db_path
from speak.telemetry.session import TelemetrySession

RECORDINGS_DIRNAME = "recordings"
TTS_ENGINE_MAX_LENGTH = 32


def build(
    env: Mapping[str, str],
    *,
    settings: Settings,
    plugin_version: str,
    host_platform: str | None = None,
) -> TelemetrySession | None:
    """The telemetry session for one process, or `None` when telemetry is off."""
    platform_name = host_platform if host_platform is not None else sys.platform
    path = db_path(env, host_platform=platform_name)
    install = wiring.start_install(
        env,
        IDENTITY,
        SCHEMA,
        db_path=path,
        plugin_version=plugin_version,
        host_platform=platform_name,
    )
    if install is None:
        return None

    harness, harness_version = wiring.detect_harness(env, IDENTITY)
    tts_engine = settings.speech.engine[:TTS_ENGINE_MAX_LENGTH]
    session_id = str(uuid.uuid4())
    store.insert_session(
        install.conn,
        SCHEMA,
        {
            "session_id": session_id,
            "install_id": install.consent.install_id,
            "started_at": store.now_iso(),
            "plugin_version": plugin_version,
            "tts_engine": tts_engine,
            "asr_engine": "nemo",
            # Always true since voice input stopped being optional. Kept as a column
            # because it is part of the shipped row contract.
            "listen_enabled": True,
            "harness": harness,
            "harness_version": harness_version,
        },
    )
    return TelemetrySession(
        conn=install.conn,
        install_id=install.consent.install_id,
        session_id=session_id,
        tts_engine=tts_engine,
        tts_model=settings.server.tts_model if settings.speech.engine == "nemo" else None,
        asr_engine="nemo",
        asr_model=settings.server.asr_model,
        record_audio=install.consent.record,
        recordings_dir=IDENTITY.config_dir(env, host_platform=platform_name) / RECORDINGS_DIRNAME,
        db_path=path,
    )

import json
import logging

import pytest
from pydantic import ValidationError
from typer.testing import CliRunner

from labflux.cli import app
from labflux.config import Settings, load_settings
from labflux.logging import JsonFormatter


def test_config_and_environment_override(tmp_path, monkeypatch):
    config = tmp_path / "config.toml"
    config.write_text('worker_id = "vm-a"\nscheduler_url = "http://old:8000"\n')
    monkeypatch.setenv("LABFLUX_SCHEDULER_URL", "http://new:8000")
    monkeypatch.setenv("LABFLUX_DATA_DIR", str(tmp_path / "state"))
    settings = load_settings(config)
    assert settings.worker_id == "vm-a"
    assert settings.scheduler_url == "http://new:8000"
    assert settings.data_dir == tmp_path / "state"


@pytest.mark.parametrize(
    "values",
    [
        {"users": {"a": "same", "b": "same"}},
        {"worker_token": "dev-user-token"},
        {"heartbeat_interval_s": 10, "offline_timeout_s": 2},
        {"worker_id": "../../outside"},
    ],
)
def test_reject_ambiguous_or_invalid_configuration(values):
    with pytest.raises(ValidationError):
        Settings(**values)


def test_logging_preserves_job_context():
    record = logging.LogRecord("worker", logging.INFO, "", 0, "started", (), None)
    record.job_id = "job-1"
    record.attempt_id = "attempt-1"
    value = json.loads(JsonFormatter().format(record))
    assert (value["component"], value["job_id"], value["attempt_id"]) == (
        "worker",
        "job-1",
        "attempt-1",
    )


def test_cli_entry_point():
    result = CliRunner().invoke(app, ["version"])
    assert result.exit_code == 0
    assert result.stdout.strip() == "0.1.0"

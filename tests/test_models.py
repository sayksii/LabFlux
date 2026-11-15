import pytest
from pydantic import ValidationError

from labflux.models import JobSpec, ResourceRequest


@pytest.mark.parametrize("values", [{"cpu": 0}, {"cpu": float("nan")}, {"memory_mib": 0}])
def test_reject_invalid_resource_requests(values):
    with pytest.raises(ValidationError):
        ResourceRequest(**values)


@pytest.mark.parametrize(
    "values",
    [
        {"argv": []},
        {"argv": ["echo", "a\x00b"]},
        {"datasets": ["../secret"]},
        {"datasets": ["data", "data"]},
        {"env": {"BAD=KEY": "x"}},
    ],
)
def test_reject_unsafe_execution_inputs(values):
    data = {"image": "python:3.12-slim", "argv": ["true"], **values}
    with pytest.raises(ValidationError):
        JobSpec(**data)


def test_rerun_flags_require_safe_to_rerun():
    for values, field in (
        ({"preemptible": True}, "preemptible requires safe_to_rerun: true"),
        ({"retries_on_node_failure": 1}, "retries_on_node_failure > 0 requires safe_to_rerun"),
    ):
        with pytest.raises(ValidationError, match=field):
            JobSpec(argv=["true"], **values)
    spec = JobSpec(argv=["true"], safe_to_rerun=True, preemptible=True, retries_on_node_failure=2)
    assert (spec.preemptible, spec.retries_on_node_failure) == (True, 2)


def test_legacy_max_retries_still_validates():
    """Specs stored before M11 (SQLite rows, Worker state.json) use the former name."""
    legacy = JobSpec.model_validate({"argv": ["true"], "max_retries": 2})
    assert (legacy.retries_on_node_failure, legacy.safe_to_rerun) == (2, True)
    assert "max_retries" not in legacy.model_dump()
    assert JobSpec.model_validate({"argv": ["true"], "max_retries": 0}).safe_to_rerun is False
    with pytest.raises(ValidationError, match="requires safe_to_rerun"):
        JobSpec.model_validate({"argv": ["true"], "max_retries": 1, "safe_to_rerun": False})
    with pytest.raises(ValidationError, match="give only retries_on_node_failure"):
        JobSpec.model_validate({"argv": ["true"], "max_retries": 1, "retries_on_node_failure": 1})

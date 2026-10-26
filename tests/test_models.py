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

import torch
from train import make_model, train


def test_model_output_shape():
    assert make_model()(torch.zeros(4, 32)).shape == (4, 1)


def test_training_reduces_the_loss():
    result = train(100, "cpu")
    assert result["last_loss"] < result["first_loss"]


def test_session_sees_exactly_one_gpu():
    # .labflux.toml asks for one GPU; LabFlux exposes only the card it assigned.
    assert torch.cuda.device_count() == 1

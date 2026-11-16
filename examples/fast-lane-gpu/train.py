"""A tiny training loop for the fast lane: `python train.py --smoke` trains for a few seconds on
the session's GPU and writes smoke-result.json, which LabFlux writes back to your machine."""

import argparse
import json
import time
from pathlib import Path

import torch
from torch import nn


def make_model() -> nn.Module:
    return nn.Sequential(nn.Linear(32, 64), nn.ReLU(), nn.Linear(64, 1))


def train(steps: int, device: str) -> dict:
    torch.manual_seed(0)
    model = make_model().to(device)
    optimizer = torch.optim.SGD(model.parameters(), lr=0.05)
    x = torch.randn(1024, 32, device=device)
    y = x[:, :1] * 2 - x[:, 1:2]  # a learnable target
    losses = []
    for _ in range(steps):
        loss = nn.functional.mse_loss(model(x), y)
        optimizer.zero_grad()
        loss.backward()
        optimizer.step()
        losses.append(loss.item())
    return {"first_loss": losses[0], "last_loss": losses[-1]}


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--smoke", action="store_true", help="200 steps instead of 5000")
    args = parser.parse_args()
    device = "cuda" if torch.cuda.is_available() else "cpu"
    started = time.monotonic()
    result = train(200 if args.smoke else 5000, device)
    result.update(
        device=torch.cuda.get_device_name() if device == "cuda" else "cpu",
        visible_gpus=torch.cuda.device_count(),
        seconds=round(time.monotonic() - started, 2),
    )
    print(json.dumps(result, indent=2))
    Path("smoke-result.json").write_text(json.dumps(result) + "\n")

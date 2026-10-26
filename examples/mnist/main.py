# Adapted from the PyTorch MNIST example:
# https://github.com/pytorch/examples/blob/d0b7e3709407074ac64bb34c5af33fe226abb47c/mnist/main.py
# Copyright (c) 2017, Pytorch contributors. BSD 3-Clause License; see LICENSE in this directory.
#
# LabFlux changes: --data-dir uses the read-only /datasets/mnist when a node provides it and
# otherwise downloads into /workspace/data; 3 epochs by default; prints the device name;
# writes the model and metrics.json (per-epoch loss and accuracy, total time) to --output-dir.
import argparse
import json
import time
from pathlib import Path

import torch
import torch.nn as nn
import torch.nn.functional as F
import torch.optim as optim
from torch.optim.lr_scheduler import StepLR
from torchvision import datasets, transforms

DATASET = Path("/datasets/mnist")


class Net(nn.Module):
    def __init__(self):
        super(Net, self).__init__()
        self.conv1 = nn.Conv2d(1, 32, 3, 1)
        self.conv2 = nn.Conv2d(32, 64, 3, 1)
        self.dropout1 = nn.Dropout(0.25)
        self.dropout2 = nn.Dropout(0.5)
        self.fc1 = nn.Linear(9216, 128)
        self.fc2 = nn.Linear(128, 10)

    def forward(self, x):
        x = self.conv1(x)
        x = F.relu(x)
        x = self.conv2(x)
        x = F.relu(x)
        x = F.max_pool2d(x, 2)
        x = self.dropout1(x)
        x = torch.flatten(x, 1)
        x = self.fc1(x)
        x = F.relu(x)
        x = self.dropout2(x)
        x = self.fc2(x)
        output = F.log_softmax(x, dim=1)
        return output


def train(args, model, device, train_loader, optimizer, epoch):
    model.train()
    total_loss = 0.0
    for batch_idx, (data, target) in enumerate(train_loader):
        data, target = data.to(device), target.to(device)
        optimizer.zero_grad()
        output = model(data)
        loss = F.nll_loss(output, target)
        loss.backward()
        optimizer.step()
        total_loss += loss.item() * len(data)
        if batch_idx % args.log_interval == 0:
            print(
                "Train Epoch: {} [{}/{} ({:.0f}%)]\tLoss: {:.6f}".format(
                    epoch,
                    batch_idx * len(data),
                    len(train_loader.dataset),
                    100.0 * batch_idx / len(train_loader),
                    loss.item(),
                )
            )
            if args.dry_run:
                break
    return total_loss / len(train_loader.dataset)


def test(model, device, test_loader):
    model.eval()
    test_loss = 0
    correct = 0
    with torch.no_grad():
        for data, target in test_loader:
            data, target = data.to(device), target.to(device)
            output = model(data)
            test_loss += F.nll_loss(output, target, reduction="sum").item()  # sum up batch loss
            pred = output.argmax(dim=1, keepdim=True)  # get the index of the max log-probability
            correct += pred.eq(target.view_as(pred)).sum().item()

    test_loss /= len(test_loader.dataset)
    accuracy = 100.0 * correct / len(test_loader.dataset)

    print(
        "\nTest set: Average loss: {:.4f}, Accuracy: {}/{} ({:.2f}%)\n".format(
            test_loss, correct, len(test_loader.dataset), accuracy
        )
    )
    return test_loss, accuracy


def main():
    # Training settings
    parser = argparse.ArgumentParser(description="PyTorch MNIST Example")
    parser.add_argument(
        "--batch-size",
        type=int,
        default=64,
        metavar="N",
        help="input batch size for training (default: 64)",
    )
    parser.add_argument(
        "--test-batch-size",
        type=int,
        default=1000,
        metavar="N",
        help="input batch size for testing (default: 1000)",
    )
    parser.add_argument(
        "--epochs", type=int, default=3, metavar="N", help="number of epochs to train (default: 3)"
    )
    parser.add_argument(
        "--lr", type=float, default=1.0, metavar="LR", help="learning rate (default: 1.0)"
    )
    parser.add_argument(
        "--gamma",
        type=float,
        default=0.7,
        metavar="M",
        help="Learning rate step gamma (default: 0.7)",
    )
    parser.add_argument("--no-accel", action="store_true", help="disables accelerator")
    parser.add_argument("--dry-run", action="store_true", help="quickly check a single pass")
    parser.add_argument("--seed", type=int, default=1, metavar="S", help="random seed (default: 1)")
    parser.add_argument(
        "--log-interval",
        type=int,
        default=10,
        metavar="N",
        help="how many batches to wait before logging training status",
    )
    parser.add_argument(
        "--data-dir",
        type=Path,
        default=DATASET if DATASET.is_dir() else Path("/workspace/data"),
        help="MNIST location (default: /datasets/mnist if present, else /workspace/data)",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path("/outputs"),
        help="where the model and metrics.json are written (default: /outputs)",
    )
    args = parser.parse_args()
    started = time.monotonic()

    use_accel = not args.no_accel and torch.accelerator.is_available()

    torch.manual_seed(args.seed)

    if use_accel:
        device = torch.accelerator.current_accelerator()
    else:
        device = torch.device("cpu")
    device_name = torch.cuda.get_device_name(device) if device.type == "cuda" else str(device)
    print(f"Device: {device_name}")

    train_kwargs = {"batch_size": args.batch_size}
    test_kwargs = {"batch_size": args.test_batch_size}
    if use_accel:
        accel_kwargs = {
            "num_workers": 1,
            "persistent_workers": True,
            "pin_memory": True,
            "shuffle": True,
        }
        train_kwargs.update(accel_kwargs)
        test_kwargs.update(accel_kwargs)

    transform = transforms.Compose(
        [transforms.ToTensor(), transforms.Normalize((0.1307,), (0.3081,))]
    )
    # Downloading is skipped when the files already exist, e.g. in a read-only node dataset.
    print(f"Data: {args.data_dir}")
    dataset1 = datasets.MNIST(args.data_dir, train=True, download=True, transform=transform)
    dataset2 = datasets.MNIST(args.data_dir, train=False, transform=transform)
    train_loader = torch.utils.data.DataLoader(dataset1, **train_kwargs)
    test_loader = torch.utils.data.DataLoader(dataset2, **test_kwargs)

    model = Net().to(device)
    optimizer = optim.Adadelta(model.parameters(), lr=args.lr)

    epochs = []
    scheduler = StepLR(optimizer, step_size=1, gamma=args.gamma)
    for epoch in range(1, args.epochs + 1):
        epoch_started = time.monotonic()
        train_loss = train(args, model, device, train_loader, optimizer, epoch)
        test_loss, accuracy = test(model, device, test_loader)
        scheduler.step()
        epochs.append(
            {
                "epoch": epoch,
                "train_loss": train_loss,
                "test_loss": test_loss,
                "test_accuracy": accuracy,
                "seconds": round(time.monotonic() - epoch_started, 2),
            }
        )

    args.output_dir.mkdir(parents=True, exist_ok=True)
    torch.save(model.state_dict(), args.output_dir / "mnist_cnn.pt")
    metrics = {
        "device": device_name,
        "data_dir": str(args.data_dir),
        "epochs": epochs,
        "final_test_accuracy": epochs[-1]["test_accuracy"] if epochs else None,
        "total_seconds": round(time.monotonic() - started, 2),
    }
    (args.output_dir / "metrics.json").write_text(json.dumps(metrics, indent=2) + "\n")
    print(json.dumps(metrics, indent=2))


if __name__ == "__main__":
    main()

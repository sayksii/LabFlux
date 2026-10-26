import json
from pathlib import Path

import torch

info = {
    "torch": torch.__version__,
    "torch_cuda": torch.version.cuda,
    "cuda_available": torch.cuda.is_available(),
    "device_count": torch.cuda.device_count(),
}
if not info["cuda_available"]:
    print(json.dumps(info))
    raise SystemExit("CUDA is not available; submit with resources.gpu >= 1")
info["device_name"] = torch.cuda.get_device_name()

a = torch.randn(512, 512)
b = torch.randn(512, 512)
gpu = (a.cuda() @ b.cuda()).cpu()
info["matmul_matches_cpu"] = torch.allclose(gpu, a @ b, rtol=1e-3, atol=1e-3)

print(json.dumps(info, indent=2))
Path("/outputs/result.json").write_text(json.dumps(info) + "\n")
if not info["matmul_matches_cpu"]:
    raise SystemExit("GPU matmul result differs from the CPU result")

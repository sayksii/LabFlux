"""印出在哪個容器、看到哪張 GPU。"""

import glob
import socket

names = []
for path in glob.glob("/proc/driver/nvidia/gpus/*/information"):
    for line in open(path):
        if line.startswith("Model:"):
            names.append(line.split(":", 1)[1].strip())
print(f"hello from {socket.gethostname()}, GPU: {', '.join(names) or 'none'}")

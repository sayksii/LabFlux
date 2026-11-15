"""模擬訓練：寫一個 20 MiB 的 checkpoint（超過 demo 的 5 MiB 寫回上限）和一個小的 metrics.json。"""

import json
import os

os.makedirs("ckpt", exist_ok=True)
with open("ckpt/model.pt", "wb") as f:
    f.write(os.urandom(20 * 1024 * 1024))
with open("metrics.json", "w") as f:
    json.dump({"loss": 0.123, "epochs": 1}, f)
print("training done: ckpt/model.pt (20 MiB) and metrics.json")

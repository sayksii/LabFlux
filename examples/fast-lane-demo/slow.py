"""每秒印一個數字並更新 progress.txt，用來試 Ctrl-C。"""

import time

for i in range(1, 31):
    print(i, flush=True)
    with open("progress.txt", "w") as f:
        f.write(f"reached {i}\n")
    time.sleep(1)
print("finished all 30")

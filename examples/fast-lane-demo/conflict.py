"""等 10 秒後寫 shared.txt；這段時間在本機也改 shared.txt，就會看到 .remote。"""

import time

time.sleep(10)
with open("shared.txt", "w") as f:
    f.write("written remotely\n")
print("wrote shared.txt")

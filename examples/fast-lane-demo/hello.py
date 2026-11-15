"""印出自己在哪台機器、哪個目錄執行，並寫一個小檔案（用來看寫回）。"""

import os
import socket

host = socket.gethostname()
print(f"hello from {host}, cwd {os.getcwd()}")
with open("result.txt", "w") as f:
    f.write(f"written remotely on {host}\n")

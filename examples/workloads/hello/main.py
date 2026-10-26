import time
from pathlib import Path

print("Hello from LabFlux", flush=True)
time.sleep(1)
Path("/outputs/result.txt").write_text("The job ran successfully.\n")
print("Output written to /outputs/result.txt", flush=True)

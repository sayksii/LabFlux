import json
import os
import sys
from pathlib import Path

home = Path(os.environ["HOME"])
(home / "labflux-probe").write_text("writable")
print(
    json.dumps(
        {
            "prefix": sys.prefix,
            "home": str(home),
            "uv_cache_dir": os.environ.get("UV_CACHE_DIR"),
            "uv_link_mode": os.environ.get("UV_LINK_MODE"),
        }
    )
)

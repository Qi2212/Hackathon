import os
from pathlib import Path

import uvicorn
from dotenv import load_dotenv

load_dotenv(Path(__file__).parent / ".env")

# 与 A 同步启用演示口径：单价与总览来自演示快照。
os.environ.setdefault("FABRIC_DEMO", "1")

if __name__ == "__main__":
    uvicorn.run(
        "ext.main:app",
        host=os.getenv("EXT_HOST", "0.0.0.0"),
        port=int(os.getenv("EXT_PORT", "8090")),
        reload=os.getenv("RELOAD") == "1",
    )

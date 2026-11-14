import os
import subprocess
import sys
from pathlib import Path

import uvicorn
from dotenv import load_dotenv

load_dotenv(Path(__file__).parent / ".env")

# 演示模式：从 app/data/demo_seed.json 回放 29 节点 / 84 卡的占用快照。
# 没有该文件时自动退回 resources.json。
os.environ.setdefault("FABRIC_DEMO", "1")

if __name__ == "__main__":
    extension = None
    try:
        if os.getenv("FABRIC_EXT_AUTOSTART", "1") == "1":
            child_env = dict(os.environ)
            child_env["RELOAD"] = "0"
            child_env.setdefault("FABRIC_BASE_URL", f"http://127.0.0.1:{os.getenv('PORT', '8080')}")
            extension = subprocess.Popen(
                [sys.executable, str(Path(__file__).with_name("run_ext.py"))],
                cwd=Path(__file__).parent, env=child_env,
            )
        print(f"Aurora Demo: http://localhost:{os.getenv('PORT', '8080')}")
        uvicorn.run(
            "app.main:app",
            host=os.getenv("HOST", "0.0.0.0"),
            port=int(os.getenv("PORT", "8080")),
            reload=os.getenv("RELOAD") == "1",
        )
    finally:
        if extension is not None and extension.poll() is None:
            extension.terminate()
            try:
                extension.wait(timeout=5)
            except subprocess.TimeoutExpired:
                extension.kill()
                extension.wait()

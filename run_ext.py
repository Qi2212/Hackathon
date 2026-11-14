import os

import uvicorn

if __name__ == "__main__":
    uvicorn.run(
        "ext.main:app",
        host=os.getenv("EXT_HOST", "0.0.0.0"),
        port=int(os.getenv("EXT_PORT", "8090")),
        reload=os.getenv("RELOAD") == "1",
    )

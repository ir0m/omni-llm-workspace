"""`python -m omniws` starts the server on OMNI_HOST:OMNI_PORT (127.0.0.1:8300 by default)."""

import uvicorn

from .config import get_settings


def main() -> None:
    settings = get_settings()
    uvicorn.run("omniws.main:app", host=settings.omni_host, port=settings.omni_port,
                proxy_headers=True, forwarded_allow_ips="127.0.0.1")


if __name__ == "__main__":
    main()

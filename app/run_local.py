"""Start the local UI without placing an API key in shell command history."""

import getpass
import os

import uvicorn


def main() -> None:
    """Prompt for a missing key, then run the same FastAPI app used by Docker."""
    if not os.getenv("OPENAI_API_KEY"):
        key = getpass.getpass("OpenAI API key (input hidden, then press Enter): ").strip()
        if not key:
            raise SystemExit("OPENAI_API_KEY is required to answer questions.")
        os.environ["OPENAI_API_KEY"] = key

    uvicorn.run("app.api:app", host="127.0.0.1", port=8000)


if __name__ == "__main__":
    main()

import asyncio
import sys

from app.warlord.main import main


def run() -> int:
    try:
        asyncio.run(main())
        return 0
    except KeyboardInterrupt:
        return 0
    except Exception as exc:  # noqa: BLE001
        print(f"Критическая ошибка: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(run())
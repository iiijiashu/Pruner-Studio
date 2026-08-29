from pruner_studio.app import serve
from pruner_studio.errors import AppError


def main() -> None:
    try:
        serve()
    except AppError as exc:
        if exc.code == "INSTANCE_ALREADY_RUNNING":
            raise SystemExit("Pruner Studio is already running for this data directory.") from None
        raise


if __name__ == "__main__":
    main()

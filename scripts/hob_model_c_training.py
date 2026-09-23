"""Run the packaged offline HOB coarse-context model trainer."""

from draftomen.augmented_training import main as _main


def main() -> int:
    """Run the packaged HOB training and promotion workflow."""

    return _main()


if __name__ == "__main__":
    raise SystemExit(main())

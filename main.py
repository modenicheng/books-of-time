from utils.logger import get_logger

logger = get_logger(__name__)


def main() -> None:
    logger.info("Hello from books-of-time!")
    logger.debug("This is a debug message (won't show at INFO level)")
    logger.warning("Something worth paying attention to")
    try:
        1 / 0
    except ZeroDivisionError:
        logger.error("Caught an exception", exc_info=True)


if __name__ == "__main__":
    main()

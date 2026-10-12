from __future__ import annotations

import argparse
import logging
import sys

from dotenv import load_dotenv

from . import __version__
from .config import ConfigError, load_config
from .http_client import ApiError
from .sync import sync_bucket


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="cos2pag",
        description="Set up COS -> PAG bucket replication via PDR for a given bucket.",
    )
    parser.add_argument("--version", action="version", version=f"cos2pag version {__version__}")
    parser.add_argument("bucket_name", help="Name of the COS bucket to synchronize (also used as the PAG repository name)")
    parser.add_argument("--tenant", required=True, help="PAG partition/tenant name (required -- several real tenant codes share a common prefix, e.g. \"ME\", \"ME-SR\", \"ME-SRE\", so this is never guessed from the bucket name)")
    parser.add_argument("-c", "--config", default="config.yaml", help="Path to the YAML config file (default: config.yaml)")
    parser.add_argument("--env-file", default=".env", help="Path to a .env file with secrets (default: .env in the current directory; silently skipped if absent)")
    parser.add_argument(
        "--noncurrent-expiration-days",
        type=int,
        default=None,
        help="Override pag.lifecycle.noncurrent_version_expiration_days for this run only",
    )
    parser.add_argument("--dry-run", action="store_true", help="Log what would be done without sending any mutating request")
    parser.add_argument("-v", "--verbose", action="store_true", help="Enable debug logging")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )

    # Populate os.environ from a .env file (if present) before the YAML
    # config's ${VAR} placeholders are resolved. override=True so .env is
    # always authoritative -- otherwise a variable already exported in the
    # shell (e.g. from an earlier `source .env` done to test with curl)
    # would silently shadow a since-corrected value in the file.
    load_dotenv(args.env_file, override=True)

    try:
        config = load_config(args.config)
        report = sync_bucket(
            config,
            args.bucket_name,
            args.tenant,
            dry_run=args.dry_run,
            noncurrent_expiration_days=args.noncurrent_expiration_days,
        )
    except (ConfigError, ApiError, LookupError) as exc:
        logging.getLogger("cos2pag").error("%s", exc)
        return 1

    print(f"\nSummary for bucket '{args.bucket_name}':")
    for step in report.steps:
        status = "SKIPPED" if step.skipped else ("CHANGED" if step.changed else "OK")
        line = f"  [{status:7s}] {step.name}"
        if step.detail:
            line += f" - {step.detail}"
        print(line)
    return 0


if __name__ == "__main__":
    sys.exit(main())

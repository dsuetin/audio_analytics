from __future__ import annotations

import argparse
import logging
import os
import subprocess
import sys
import time as time_module
from datetime import date, datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo


logger = logging.getLogger(__name__)


def env_value(name: str, default: str) -> str:
    value = os.getenv(name, "").strip()
    return value or default


def resolve_report_date(zone: ZoneInfo) -> date:
    return datetime.now(zone).date() - timedelta(days=1)


def build_output_path(output_dir: Path, report_date: date) -> Path:
    return output_dir / f"transcript_report_{report_date.isoformat()}.xlsx"


def seconds_until_next_midnight(zone: ZoneInfo) -> float:
    now = datetime.now(zone)
    next_midnight = datetime.combine(now.date() + timedelta(days=1), time.min, tzinfo=zone)
    return max((next_midnight - now).total_seconds(), 0.0)


def run_daily_report(
    report_date: date,
    timezone_name: str,
    output_dir: Path,
    postgres_host: str,
    postgres_port: str,
    postgres_user: str,
    postgres_password: str,
    postgres_db: str,
) -> None:
    repo_root = Path(__file__).resolve().parents[1]
    script_path = repo_root / "stats_service" / "export_daily_transcript_report.py"
    output_path = build_output_path(output_dir, report_date)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    command = [
        sys.executable,
        str(script_path),
        "--date",
        report_date.isoformat(),
        "--timezone",
        timezone_name,
        "--output",
        str(output_path),
    ]

    logger.info("Running report for %s", report_date.isoformat())
    child_env = os.environ.copy()
    child_env.update(
        {
            "POSTGRES_HOST": postgres_host,
            "POSTGRES_PORT": postgres_port,
            "POSTGRES_USER": postgres_user,
            "POSTGRES_PASSWORD": postgres_password,
            "POSTGRES_DB": postgres_db,
        }
    )
    result = subprocess.run(
        command,
        cwd=str(repo_root),
        check=False,
        capture_output=True,
        text=True,
        env=child_env,
    )

    if result.stdout:
        logger.info("report stdout:\n%s", result.stdout.rstrip())
    if result.stderr:
        logger.warning("report stderr:\n%s", result.stderr.rstrip())

    if result.returncode != 0:
        raise RuntimeError(f"report generation failed with exit code {result.returncode}")

    logger.info("Report generated: %s and %s", output_path, output_path.with_suffix(".pdf"))


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the daily statistics export at midnight.")
    parser.add_argument(
        "--timezone",
        default=env_value("REPORT_TIMEZONE", "Europe/Moscow"),
        help="Timezone used to calculate midnight and the previous day.",
    )
    parser.add_argument(
        "--date",
        help="Optional report date in YYYY-MM-DD format. Defaults to yesterday in the chosen timezone.",
    )
    parser.add_argument(
        "--output-dir",
        default=env_value("REPORT_OUTPUT_DIR", "reports"),
        help="Directory for generated report files.",
    )
    parser.add_argument(
        "--postgres-host",
        default=env_value("POSTGRES_HOST", "localhost"),
        help="PostgreSQL host for local runs. Use postgres inside Docker.",
    )
    parser.add_argument(
        "--postgres-port",
        default=env_value("POSTGRES_PORT", "5432"),
        help="PostgreSQL port.",
    )
    parser.add_argument(
        "--postgres-user",
        default=env_value("POSTGRES_USER", "speech"),
        help="PostgreSQL user.",
    )
    parser.add_argument(
        "--postgres-password",
        default=env_value("POSTGRES_PASSWORD", "speech"),
        help="PostgreSQL password.",
    )
    parser.add_argument(
        "--postgres-db",
        default=env_value("POSTGRES_DB", "speech_db"),
        help="PostgreSQL database name.",
    )
    parser.add_argument(
        "--run-on-startup",
        action="store_true",
        help="Generate the previous day report immediately, then continue with midnight scheduling.",
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="Generate one report and exit instead of waiting for midnight.",
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=os.getenv("LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    zone = ZoneInfo(args.timezone)
    output_dir = Path(args.output_dir)
    report_date = datetime.strptime(args.date, "%Y-%m-%d").date() if args.date else resolve_report_date(zone)

    if args.run_on_startup or args.once:
        run_daily_report(
            report_date,
            args.timezone,
            output_dir,
            args.postgres_host,
            args.postgres_port,
            args.postgres_user,
            args.postgres_password,
            args.postgres_db,
        )
        if args.once:
            return

    while True:
        sleep_seconds = seconds_until_next_midnight(zone)
        logger.info("Sleeping %.1f seconds until midnight", sleep_seconds)
        time_module.sleep(sleep_seconds)

        report_date = resolve_report_date(zone)
        try:
            run_daily_report(
                report_date,
                args.timezone,
                output_dir,
                args.postgres_host,
                args.postgres_port,
                args.postgres_user,
                args.postgres_password,
                args.postgres_db,
            )
        except Exception:
            logger.exception("Daily report failed")
            time_module.sleep(60)


if __name__ == "__main__":
    main()

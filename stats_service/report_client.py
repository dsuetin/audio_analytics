import argparse
from datetime import date, timedelta
from pathlib import Path

import requests


DEFAULT_SERVER = "http://localhost:8000"
DEFAULT_OUTPUT_DIR = "./reports"


def get_default_date():
    """
    По умолчанию отчет за вчера.
    """
    return date.today() - timedelta(days=1)


def download_report(
    server: str,
    report_date: str,
    output: Path,
    report_format: str,
):

    url = f"{server}/reports/daily"

    response = requests.get(
        url,
        params={
            "report_date": report_date,
            "format": report_format,
        },
        timeout=600,
    )

    response.raise_for_status()

    output.parent.mkdir(
        parents=True,
        exist_ok=True,
    )

    output.write_bytes(
        response.content
    )

    print(
        f"Saved: {output}"
    )


def main():

    parser = argparse.ArgumentParser(
        description="Download daily transcript report"
    )


    parser.add_argument(
        "--server",
        default=DEFAULT_SERVER,
        help=(
            f"Report service URL "
            f"(default: {DEFAULT_SERVER})"
        ),
    )


    parser.add_argument(
        "--date",
        default=str(get_default_date()),
        help=(
            "Report date YYYY-MM-DD "
            f"(default: {get_default_date()})"
        ),
    )


    parser.add_argument(
        "--format",
        choices=[
            "pdf",
            "xlsx",
        ],
        default="pdf",
        help="Report format",
    )


    parser.add_argument(
        "--output",
        default=None,
        help="Output file path",
    )


    args = parser.parse_args()


    if args.output:

        output = Path(args.output)

    else:

        output = Path(
            DEFAULT_OUTPUT_DIR,
            (
                f"transcript_report_"
                f"{args.date}."
                f"{args.format}"
            )
        )


    download_report(
        server=args.server,
        report_date=args.date,
        output=output,
        report_format=args.format,
    )


if __name__ == "__main__":
    main()
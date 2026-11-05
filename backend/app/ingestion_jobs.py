"""Safe local diagnostics/retry; stop the API before resetting an exhausted job."""

import argparse
import asyncio
import json
import re

from app.repositories.ingestion_jobs import IngestionJobs


async def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--retry", metavar="MESSAGE_REF")
    args = parser.parse_args()
    jobs = IngestionJobs()
    if args.retry:
        if not re.fullmatch(r"[0-9a-f]{24}", args.retry):
            parser.error("Expected a 24-character message reference")
        print(json.dumps({"reset_jobs": await jobs.retry(args.retry)}))
    else:
        print(json.dumps(await jobs.diagnostics()))


if __name__ == "__main__":
    asyncio.run(main())

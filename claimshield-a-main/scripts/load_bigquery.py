"""Create the ClaimShield BigQuery tables and load the reference pricing CSVs.

    GOOGLE_CLOUD_PROJECT=... BQ_DATASET=claimshield python scripts/load_bigquery.py
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from google.cloud import bigquery  # noqa: E402

from claimshield.config import load_settings  # noqa: E402


def main() -> None:
    s = load_settings()
    if not s.project:
        sys.exit("Set GOOGLE_CLOUD_PROJECT (in .env or the environment).")
    client = bigquery.Client(project=s.project)
    dataset = f"{s.project}.{s.bq_dataset}"
    ddl = (ROOT / "claimshield" / "sql" / "schema_bigquery.sql").read_text().replace("{dataset}", dataset)
    for stmt in ddl.split(";"):
        body = "\n".join(line for line in stmt.splitlines() if not line.strip().startswith("--")).strip()
        if body:
            client.query(body).result()
    migration = (ROOT / "claimshield" / "sql" / "migrate_benchmark_v2.sql").read_text().replace("{dataset}", dataset)
    for stmt in migration.split(";"):
        body = "\n".join(line for line in stmt.splitlines() if not line.strip().startswith("--")).strip()
        if body:
            client.query(body).result()
    print(f"tables ready in {dataset}")
    for table in ("reference_pricing", "procedure_benchmarks"):
        job = client.load_table_from_file(
            open(ROOT / "data" / "reference" / f"{table}.csv", "rb"),
            f"{dataset}.{table}",
            job_config=bigquery.LoadJobConfig(
                source_format=bigquery.SourceFormat.CSV, skip_leading_rows=1,
                write_disposition=bigquery.WriteDisposition.WRITE_TRUNCATE,
            ),
        )
        job.result()
        print(f"loaded {job.output_rows} rows into {dataset}.{table}")


if __name__ == "__main__":
    main()

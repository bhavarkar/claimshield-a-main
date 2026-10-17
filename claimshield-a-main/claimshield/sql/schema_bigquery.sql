-- BigQuery schema for ClaimShield AI. Replace `{dataset}` (scripts/load_bigquery.py does this).

CREATE TABLE IF NOT EXISTS `{dataset}.line_items` (
  case_id      STRING NOT NULL,
  line_no      INT64  NOT NULL,
  service_date STRING,
  code         STRING,
  description  STRING,
  dept         STRING,
  category     STRING,
  unit         STRING,
  region       STRING,
  qty          FLOAT64,
  unit_price   FLOAT64,
  amount       FLOAT64,
  hospital     STRING,
  procedure    STRING,
  ingested_at  TIMESTAMP
)
PARTITION BY DATE(ingested_at)
CLUSTER BY case_id;

CREATE TABLE IF NOT EXISTS `{dataset}.reference_pricing` (
  code           STRING NOT NULL,
  description    STRING,
  category       STRING,
  family         STRING,
  tier           INT64,
  p50_unit_price FLOAT64,
  p90_unit_price FLOAT64,
  city_tier      INT64,
  source         STRING,
  region         STRING,
  unit           STRING,
  price_source   STRING,
  price_type     STRING,
  provider_id    STRING,
  reference_date DATE,
  dataset_version STRING
);

CREATE TABLE IF NOT EXISTS `{dataset}.procedure_benchmarks` (
  procedure_key     STRING NOT NULL,
  family            STRING,
  max_expected_tier INT64,
  expected_code     STRING,
  rationale         STRING
);

CREATE TABLE IF NOT EXISTS `{dataset}.findings` (
  case_id     STRING NOT NULL,
  finding_id  STRING,
  type        STRING,
  title       STRING,
  bill_lines  ARRAY<INT64>,
  confidence  STRING,
  impact      FLOAT64,
  counterparty STRING,
  clause_id   STRING,
  created_at  TIMESTAMP
);

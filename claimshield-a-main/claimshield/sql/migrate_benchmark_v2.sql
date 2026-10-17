-- Apply once to an existing BigQuery deployment before enabling regional comparisons.
ALTER TABLE `{dataset}.line_items` ADD COLUMN IF NOT EXISTS unit STRING;
ALTER TABLE `{dataset}.line_items` ADD COLUMN IF NOT EXISTS region STRING;
ALTER TABLE `{dataset}.reference_pricing` ADD COLUMN IF NOT EXISTS region STRING;
ALTER TABLE `{dataset}.reference_pricing` ADD COLUMN IF NOT EXISTS unit STRING;
ALTER TABLE `{dataset}.reference_pricing` ADD COLUMN IF NOT EXISTS price_source STRING;
ALTER TABLE `{dataset}.reference_pricing` ADD COLUMN IF NOT EXISTS price_type STRING;
ALTER TABLE `{dataset}.reference_pricing` ADD COLUMN IF NOT EXISTS provider_id STRING;
ALTER TABLE `{dataset}.reference_pricing` ADD COLUMN IF NOT EXISTS reference_date DATE;
ALTER TABLE `{dataset}.reference_pricing` ADD COLUMN IF NOT EXISTS dataset_version STRING;

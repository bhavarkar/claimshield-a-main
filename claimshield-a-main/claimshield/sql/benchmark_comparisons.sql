-- One deterministic comparison per billed line against the active regional dataset version.
SELECT
  li.line_no, li.code, li.description, li.category, li.procedure, li.region AS billed_region,
  li.unit, li.qty, li.unit_price AS billed_unit_rate, li.amount,
  rp.description AS benchmark_description, rp.p50_unit_price, rp.p90_unit_price,
  rp.region AS benchmark_region, rp.price_source, rp.price_type, rp.provider_id, rp.reference_date, rp.dataset_version,
  (SELECT COUNT(*) FROM {reference_pricing} AS alt
    WHERE alt.code = li.code AND alt.unit = li.unit AND alt.dataset_version = @dataset_version
      AND alt.region != @region) AS other_region_matches,
  (SELECT COUNT(*) FROM {reference_pricing} AS alt
    WHERE alt.code = li.code AND alt.region = @region AND alt.dataset_version = @dataset_version
      AND alt.unit != li.unit) AS other_unit_matches
FROM {line_items} AS li
LEFT JOIN {reference_pricing} AS rp
  ON rp.code = li.code AND rp.unit = li.unit
  AND rp.region = @region AND rp.dataset_version = @dataset_version
  AND (rp.price_type != 'contracted_tariff' OR
       (rp.provider_id IS NOT NULL AND LOWER(TRIM(rp.provider_id)) = LOWER(TRIM(li.hospital))))
  AND (rp.price_type = 'contracted_tariff' OR NOT EXISTS (
       SELECT 1 FROM {reference_pricing} AS contract
       WHERE contract.code = li.code AND contract.unit = li.unit AND contract.region = @region
         AND contract.dataset_version = @dataset_version AND contract.price_type = 'contracted_tariff'
         AND LOWER(TRIM(contract.provider_id)) = LOWER(TRIM(li.hospital))))
WHERE li.case_id = @case_id
ORDER BY li.line_no

-- Unit prices well above the 90th-percentile reference price for the same code.
SELECT
  li.line_no, li.code, li.description, li.qty, li.unit_price, li.amount,
  rp.p50_unit_price, rp.p90_unit_price, rp.source, rp.region, rp.unit,
  rp.price_source, rp.price_type, rp.provider_id, rp.reference_date, rp.dataset_version,
  li.unit_price / rp.p90_unit_price AS ratio
FROM {line_items} AS li
JOIN {reference_pricing} AS rp ON rp.code = li.code AND rp.unit = li.unit
  AND rp.region = @region AND rp.dataset_version = @dataset_version
  AND (rp.price_type != 'contracted_tariff' OR
       (rp.provider_id IS NOT NULL AND LOWER(TRIM(rp.provider_id)) = LOWER(TRIM(li.hospital))))
  AND (rp.price_type = 'contracted_tariff' OR NOT EXISTS (
       SELECT 1 FROM {reference_pricing} AS contract
       WHERE contract.code = li.code AND contract.unit = li.unit AND contract.region = @region
         AND contract.dataset_version = @dataset_version AND contract.price_type = 'contracted_tariff'
         AND LOWER(TRIM(contract.provider_id)) = LOWER(TRIM(li.hospital))))
WHERE li.case_id = @case_id
  AND CASE WHEN rp.price_type = 'contracted_tariff'
           THEN li.unit_price > rp.p90_unit_price
           ELSE li.unit_price > rp.p90_unit_price * @outlier_ratio END
ORDER BY ratio DESC

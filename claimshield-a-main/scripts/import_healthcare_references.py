"""Import source-derived JSON as pending records; this command cannot approve records."""
from __future__ import annotations

import argparse
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from claimshield.services import get_services
from claimshield.services.healthcare_references import HealthcareReferenceRegistry, ReferenceImport


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('input', type=Path, help='Source-derived JSON containing rates and/or room_rules')
    parser.add_argument('--validate-only', action='store_true', help='Validate without accessing configured storage')
    args = parser.parse_args()
    batch = ReferenceImport.model_validate_json(args.input.read_bytes())
    if args.validate_only:
        print(f'Valid: {len(batch.rates)} rates, {len(batch.room_rules)} room rules. Nothing imported or activated.')
    else:
        result = HealthcareReferenceRegistry(get_services().storage).import_records(batch)
        print(f"Imported {result['imported']} pending records. No records were activated.")


if __name__ == '__main__':
    main()

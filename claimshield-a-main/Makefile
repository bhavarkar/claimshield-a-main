PY ?= .venv/bin/python
PORT ?= 8080

.PHONY: setup data run test samples docker gcp-setup bq-load index-policies deploy

setup:            ## create the virtualenv and install everything
	python3.11 -m venv .venv
	.venv/bin/python -m pip install -r requirements.lock
	.venv/bin/python -m pip check

data:             ## regenerate the synthetic bills, policies and answer key
	$(PY) data/generate.py

run:              ## start the API + web UI on http://localhost:$(PORT)
	$(PY) -m uvicorn claimshield.api.main:app --reload --port $(PORT)

test:             ## run the test suite (local mode)
	$(PY) -m pytest -q

samples:          ## run every synthetic bill through the agents and print findings
	$(PY) -W ignore scripts/run_samples.py

docker:           ## build and run the container locally
	docker build -t claimshield-ai .
	docker run --rm --network host -e CLAIMSHIELD_AUTH_MODE=demo -e CLAIMSHIELD_DEMO_PACE=0.35 claimshield-ai python -m uvicorn claimshield.api.main:app --host 127.0.0.1 --port $(PORT)

gcp-setup:        ## enable APIs, create bucket, dataset, service account (needs gcloud)
	bash scripts/setup_gcp.sh

bq-load:          ## create BigQuery tables and load reference pricing
	$(PY) scripts/load_bigquery.py

index-policies:   ## upload sample policies to GCS and index them in Vertex AI Search
	$(PY) scripts/index_policies.py

deploy:           ## deploy to Cloud Run
	bash scripts/deploy_cloud_run.sh

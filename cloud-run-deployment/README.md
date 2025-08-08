# Medical PDF Parser - Cloud Run Deployment

This repository contains the production-ready files for deploying the Medical PDF Parser to Google Cloud Run.

## Files

- `main.py` - Cloud Function entry point
- `requirements.txt` - Python dependencies
- `production_medical_parser.py` - Core parsing logic
- `MASTERPLAN SHEET TEMPLATE - Reference Values.csv` - Reference values for medical markers

## Deployment

This repository is configured for automatic deployment to Google Cloud Run via GitHub integration.

### Environment Variables Required

- `GCP_PROJECT_ID`: Your Google Cloud Project ID
- `DOCUMENT_AI_PROCESSOR_ID`: Document AI Processor ID

## Usage

Once deployed, the service accepts POST requests with base64-encoded PDF data and returns structured medical data in JSON format.
"""
Cloud Run Entry Point - Medical PDF Parser for Make.com
Simplified and focused Flask app using trained Document AI processor
"""

import os
import logging
from flask import Flask, request, jsonify
from parser import process_for_makecom

# Configure logging
import google.cloud.logging
client = google.cloud.logging.Client()
client.setup_logging()

logger = logging.getLogger(__name__)

# Create Flask app
app = Flask(__name__)

# Configuration for trained processor
PROCESSOR_CONFIG = {
    "project_id": "naehrstoff-masterplan",
    "processor_id": "7d33797caa970d86",  # analysis-pdf-extractor-rowbased (new trained processor)
    "location": "us"
}

# Fallback processor if new one not available yet
FALLBACK_CONFIG = {
    "project_id": "naehrstoff-masterplan",
    "processor_id": "660c2eb22fc5de56",  # Medical Analysis Report Parser (backup)
    "location": "us"
}


@app.route('/health', methods=['GET'])
def health_check():
    """Health check endpoint"""
    return jsonify({
        'status': 'healthy',
        'service': 'medical-pdf-parser',
        'processor_id': PROCESSOR_CONFIG["processor_id"],
        'version': '2.0-streamlined'
    })


@app.route('/', methods=['POST', 'OPTIONS'])
def parse_medical_pdf():
    """
    Main endpoint for Medical PDF parsing
    Designed for Make.com webhook integration
    """
    
    # Handle CORS preflight
    if request.method == 'OPTIONS':
        response = jsonify({'status': 'ok'})
        response.headers.add('Access-Control-Allow-Origin', '*')
        response.headers.add('Access-Control-Allow-Methods', 'POST, OPTIONS')
        response.headers.add('Access-Control-Allow-Headers', 'Content-Type, Authorization, X-API-Key')
        return response
    
    try:
        # Parse JSON request
        data = request.get_json()
        if not data:
            return jsonify({
                "status": "error",
                "message": "No JSON data provided"
            }), 400
        
        # Validate required fields
        if 'pdf_base64' not in data:
            return jsonify({
                "status": "error", 
                "message": "Missing required field: pdf_base64"
            }), 400
        
        # Optional API key validation
        api_key = request.headers.get('X-API-Key')
        expected_key = os.environ.get('MAKECOM_API_KEY')
        
        if expected_key and (not api_key or api_key != expected_key):
            return jsonify({
                "status": "error",
                "message": "Invalid or missing API key"
            }), 401
        
        # Process PDF
        filename = data.get('filename', 'medical_report.pdf')
        logger.info(f"Processing: {filename}")
        
        result = process_for_makecom(
            pdf_base64=data['pdf_base64'],
            processor_config=PROCESSOR_CONFIG,
            fallback_config=FALLBACK_CONFIG
        )
        
        # Add filename to result
        result['filename'] = filename
        
        # Log result summary
        if result.get('status') == 'success':
            stats = result.get('extraction_stats', {})
            logger.info(f"Success: {stats.get('total_markers_found', 0)} markers extracted")
        else:
            logger.error(f"Failed: {result.get('message', 'Unknown error')}")
        
        # Return result with CORS headers
        response = jsonify(result)
        response.headers.add('Access-Control-Allow-Origin', '*')
        return response
        
    except Exception as e:
        logger.error(f"Request processing failed: {str(e)}", exc_info=True)
        return jsonify({
            "status": "error",
            "message": f"Server error: {str(e)}"
        }), 500


if __name__ == '__main__':
    port = int(os.environ.get('PORT', 8080))
    app.run(host='0.0.0.0', port=port)
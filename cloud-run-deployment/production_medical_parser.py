#!/usr/bin/env python3
"""
Fixed Production Medical Parser - Addresses Cloud Run issues
Main fixes:
1. Robust text anchor extraction with multiple fallbacks
2. Better error handling to prevent crashes
3. Enhanced table detection and parsing
4. Defensive programming for Cloud Run environment

Document AI API 2025 Features Used:
- Form Parser with enhanced table extraction (simple tables only)
- Maximum file size increased to 40MB (up from 20MB)
- Maximum page limit increased to 30 pages
- Latest stable foundation model support
"""

import json
import re
import os
import csv
import logging
import base64
import time
from typing import Dict, Any, List, Tuple, Optional, Set
from dataclasses import dataclass, field
from enum import Enum
from functools import lru_cache
from google.cloud import documentai
from google.api_core import retry
from google.api_core.exceptions import GoogleAPIError, RetryError

# Configure logging for Cloud Run
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

class ProcessingError(Exception):
    """Custom exception for processing errors"""
    pass

class ValidationError(Exception):
    """Custom exception for validation errors"""
    pass

class MarkerCategory(Enum):
    """Enum for marker categories"""
    HEMATOLOGY = "hematology"
    CLINICAL_CHEMISTRY = "clinical_chemistry"
    HORMONES = "hormones"
    CLINICAL_IMMUNOLOGY = "clinical_immunology"
    METALS_TRACE_ELEMENTS = "metals_trace_elements"
    MICRONUTRIENTS = "micronutrients"
    FATTY_ACIDS = "fatty_acids"
    QUOTIENTS = "quotients"

@dataclass
class BloodMarker:
    """Data class for blood marker with validation"""
    test: str
    result: str
    unit: str = ""
    reference_range: str = ""
    category: Optional[MarkerCategory] = None
    confidence: float = 0.0
    is_critical: bool = False
    additional_notes: str = ""
    
    def __post_init__(self):
        """Validate marker data after initialization"""
        if not self.test or not self.result:
            raise ValidationError(f"Invalid marker: test='{self.test}', result='{self.result}'")
        
        # Clean and normalize values
        self.test = self._normalize_text(self.test)
        self.result = self._normalize_value(self.result)
        self.unit = self._normalize_unit(self.unit)
        
    def _normalize_text(self, text: str) -> str:
        """Normalize text values"""
        if not text:
            return ""
        # Remove multiple spaces and special characters
        text = re.sub(r'\s+', ' ', text.strip())
        text = re.sub(r'[\x00-\x1f\x7f-\x9f]', '', text)
        return text
    
    def _normalize_value(self, value: str) -> str:
        """Normalize numeric values"""
        if not value:
            return ""
        # Handle special cases like < and >
        value = value.strip()
        # Replace comma with dot for decimal numbers (German format)
        value = value.replace(',', '.')
        return value
    
    def _normalize_unit(self, unit: str) -> str:
        """Normalize units"""
        if not unit:
            return ""
        unit = unit.strip()
        # Common unit normalizations
        unit_map = {
            'mg/dl': 'mg/dL',
            'ug/l': 'µg/L',
            'ng/ml': 'ng/mL',
            'pg/ml': 'pg/mL',
            'mmol/l': 'mmol/L',
            'umol/l': 'µmol/L'
        }
        return unit_map.get(unit.lower(), unit)

class ProductionMedicalParser:
    """Fixed medical parser addressing Cloud Run issues"""
    
    def __init__(self, project_id: str, processor_id: str, location: str = "eu"):
        self.project_id = project_id
        self.processor_id = processor_id
        self.location = location
        
        try:
            # Initialize Document AI client with retry settings
            self.client = documentai.DocumentProcessorServiceClient()
            self.processor_name = f"projects/{project_id}/locations/{location}/processors/{processor_id}"
            logger.info(f"Initialized parser for processor: {self.processor_name}")
        except Exception as e:
            logger.error(f"Failed to initialize DocumentAI client: {e}")
            raise ProcessingError(f"DocumentAI initialization failed: {e}")
        
        # Load reference markers safely
        try:
            self.reference_markers = self._load_reference_markers()
        except Exception as e:
            logger.warning(f"Failed to load reference markers: {e}")
            self.reference_markers = {}
        
        # Initialize category patterns
        self._init_category_patterns()
    
    def _init_category_patterns(self):
        """Initialize comprehensive category patterns"""
        self.category_patterns = {
            MarkerCategory.HEMATOLOGY: {
                'keywords': ['leukoz', 'erythroz', 'hämoglobin', 'hämatokrit', 'mcv', 
                            'mch', 'mchc', 'thromboz', 'rdw', 'neutrophil', 'lymphoz', 
                            'monoz', 'eosinophil', 'basophil', 'hematocrit', 'platelets'],
                'regex': re.compile(r'(leuko|erythro|hb|hct|mcv|mch|mchc|plt|rdw)', re.I)
            },
            MarkerCategory.CLINICAL_CHEMISTRY: {
                'keywords': ['ferritin', 'gesamteiweiß', 'calcium', 'protein', 'albumin',
                            'glucose', 'creatinine', 'urea', 'bilirubin', 'ast', 'alt'],
                'regex': re.compile(r'(ferritin|protein|calcium|glucose|creatinin|urea)', re.I)
            },
            MarkerCategory.HORMONES: {
                'keywords': ['t3', 't4', 'tsh', 'freies', 'hormone', 'cortisol', 
                            'testosterone', 'estradiol', 'insulin', 'dhea'],
                'regex': re.compile(r'(t3|t4|tsh|ft3|ft4|cortisol|testosteron)', re.I)
            },
            MarkerCategory.CLINICAL_IMMUNOLOGY: {
                'keywords': ['crp', 'immunoglobulin', 'igg', 'iga', 'igm', 'ige',
                            'interleukin', 'complement', 'antibody'],
                'regex': re.compile(r'(crp|ig[agme]|interleukin|complement)', re.I)
            },
            MarkerCategory.METALS_TRACE_ELEMENTS: {
                'keywords': ['magnesium', 'selen', 'zink', 'kupfer', 'chrom', 'blei', 
                            'cadmium', 'nickel', 'quecksilber', 'kalium', 'natrium', 
                            'phosphor', 'mangan', 'molybdän', 'iron', 'copper', 'zinc'],
                'regex': re.compile(r'(mg|se|zn|cu|cr|pb|cd|ni|hg|k|na|p|mn|mo|fe)', re.I)
            },
            MarkerCategory.MICRONUTRIENTS: {
                'keywords': ['vitamin', 'folsäure', 'cobalamin', 'holotrans', 'biotin',
                            'niacin', 'riboflavin', 'thiamin', 'folic acid', 'b12'],
                'regex': re.compile(r'(vitamin|vit|folsäure|folate|b12|cobalamin)', re.I)
            },
            MarkerCategory.FATTY_ACIDS: {
                'keywords': ['linol', 'omega', 'epa', 'dha', 'arachidon', 'fettsäuren',
                            'palmitin', 'stearin', 'fatty acid', 'lipid'],
                'regex': re.compile(r'(omega|epa|dha|linol|arachidon|fatty|lipid)', re.I)
            },
            MarkerCategory.QUOTIENTS: {
                'keywords': ['index', 'verhältnis', 'quotient', 'ratio', 'aa/epa',
                            'omega-6/omega-3', 'ldl/hdl'],
                'regex': re.compile(r'(index|ratio|quotient|verhältnis|/)', re.I)
            }
        }
    
    @lru_cache(maxsize=1)
    def _load_reference_markers(self) -> Dict[str, Dict[str, str]]:
        """Load and cache reference markers from CSV - with error handling"""
        markers = {}
        csv_files = [
            "MASTERPLAN SHEET TEMPLATE - Reference Values.csv",
            "../MASTERPLAN SHEET TEMPLATE - Reference Values.csv",
            "./MASTERPLAN SHEET TEMPLATE - Reference Values.csv"
        ]
        
        for csv_file in csv_files:
            try:
                if os.path.exists(csv_file):
                    with open(csv_file, 'r', encoding='utf-8') as f:
                        reader = csv.DictReader(f)
                        for row in reader:
                            marker_name = row.get('Markername', '').strip()
                            if marker_name:
                                markers[marker_name.lower()] = {
                                    'original_name': marker_name,
                                    'unit': row.get('Unit', '').strip(),
                                    'optimal_range': row.get('Optimalbereich', '').strip(),
                                    'very_low': row.get('very low', '').strip(),
                                    'low': row.get('low', '').strip(),
                                    'optimal': row.get('optimal', '').strip(),
                                    'high': row.get('high', '').strip(),
                                    'too_high': row.get('too high', '').strip()
                                }
                    logger.info(f"Loaded {len(markers)} reference markers from {csv_file}")
                    break
            except Exception as e:
                logger.warning(f"Could not load {csv_file}: {e}")
                continue
        
        if not markers:
            logger.warning("No reference markers loaded - continuing without reference data")
        
        return markers
    
    @retry.Retry(predicate=retry.if_exception_type(GoogleAPIError), deadline=60.0)
    def process_document(self, file_path: str = None, pdf_content: bytes = None) -> Dict[str, Any]:
        """Process document with enhanced error handling"""
        
        try:
            # Prepare document content
            if file_path:
                logger.info(f"Processing file: {file_path}")
                with open(file_path, "rb") as f:
                    document_content = f.read()
            elif pdf_content:
                logger.info("Processing PDF from bytes")
                document_content = pdf_content
            else:
                raise ValidationError("Either file_path or pdf_content must be provided")
            
            # Create DocumentAI request
            raw_document = documentai.RawDocument(
                content=document_content,
                mime_type="application/pdf"
            )
            
            request = documentai.ProcessRequest(
                name=self.processor_name,
                raw_document=raw_document
            )
            
            # Process with DocumentAI (with automatic retry)
            logger.info("Sending request to Document AI...")
            start_time = time.time()
            result = self.client.process_document(request=request)
            processing_time = time.time() - start_time
            logger.info(f"Document AI processing completed in {processing_time:.2f}s")
            
            # Extract data with enhanced error handling
            extracted_data = self._robust_extraction(result.document)
            extracted_data['processing_metadata'] = {
                'processing_time': processing_time,
                'processor_id': self.processor_id,
                'document_pages': len(result.document.pages)
            }
            
            return extracted_data
            
        except GoogleAPIError as e:
            logger.error(f"Google API error: {e}")
            raise ProcessingError(f"Document AI processing failed: {e}")
        except Exception as e:
            logger.error(f"Unexpected error in document processing: {e}")
            raise ProcessingError(f"Document processing failed: {e}")
    
    def _robust_extraction(self, document) -> Dict[str, Any]:
        """Robust extraction with comprehensive error handling"""
        
        logger.info("Starting robust extraction...")
        
        # Initialize result structure
        result = self._initialize_result_structure()
        
        try:
            # Strategy 1: Safe table extraction
            self._safe_table_extraction(document, result)
        except Exception as e:
            logger.warning(f"Table extraction failed: {e}")
        
        try:
            # Strategy 2: Pattern-based extraction as fallback
            self._pattern_extraction_fallback(document, result)
        except Exception as e:
            logger.warning(f"Pattern extraction failed: {e}")
        
        try:
            # Strategy 3: Form field extraction for patient info
            self._safe_form_field_extraction(document, result)
        except Exception as e:
            logger.warning(f"Form field extraction failed: {e}")
        
        # Post-processing with error handling
        try:
            self._safe_post_processing(result)
        except Exception as e:
            logger.warning(f"Post-processing failed: {e}")
        
        return result
    
    def _initialize_result_structure(self) -> Dict[str, Any]:
        """Initialize comprehensive result structure"""
        return {
            "header": {
                "medical_director": "",
                "scientists": "",
                "address": "",
                "contact": "",
                "insurance": "",
                "collection_date": "",
                "collection_time": ""
            },
            "patient_info": {
                "name": "",
                "diary_number": "",
                "birth_date_gender": "",
                "entry_date": "",
                "exit_date": ""
            },
            "hematology": [],
            "clinical_chemistry": [],
            "hormones": [],
            "clinical_immunology": [],
            "metals_trace_elements": [],
            "micronutrients": [],
            "fatty_acids": {
                "omega_3_fatty_acids": [],
                "omega_6_fatty_acids": [],
                "monounsaturated_fatty_acids": [],
                "trans_fatty_acids": [],
                "saturated_fatty_acids": []
            },
            "quotients": [],
            "extraction_stats": {
                "total_markers_found": 0,
                "markers_with_reference": 0,
                "markers_without_reference": 0,
                "critical_values": [],
                "extraction_confidence": 0.0,
                "validation_status": "pending"
            }
        }
    
    def _safe_table_extraction(self, document, result: Dict[str, Any]):
        """Safe table extraction with multiple fallback methods"""
        
        logger.info("Starting safe table extraction...")
        
        if not hasattr(document, 'pages') or not document.pages:
            logger.warning("Document has no pages")
            return
        
        for page_num, page in enumerate(document.pages):
            try:
                logger.info(f"Processing page {page_num + 1}...")
                
                if not hasattr(page, 'tables') or not page.tables:
                    logger.info(f"Page {page_num + 1} has no tables")
                    continue
                
                for table_idx, table in enumerate(page.tables):
                    try:
                        logger.info(f"Processing table {table_idx + 1} on page {page_num + 1}")
                        table_data = self._extract_table_with_fallbacks(table, document.text)
                        
                        if table_data:
                            logger.info(f"Extracted {len(table_data)} rows from table {table_idx + 1}")
                            self._process_table_rows(table_data, result)
                        else:
                            logger.warning(f"No data extracted from table {table_idx + 1}")
                            
                    except Exception as e:
                        logger.error(f"Error processing table {table_idx + 1}: {e}")
                        continue
                        
            except Exception as e:
                logger.error(f"Error processing page {page_num + 1}: {e}")
                continue
    
    def _extract_table_with_fallbacks(self, table, full_text: str) -> List[List[str]]:
        """Extract table data with multiple fallback methods"""
        
        rows = []
        
        # Method 1: Try body_rows first
        if hasattr(table, 'body_rows') and table.body_rows:
            try:
                for row in table.body_rows:
                    if hasattr(row, 'cells') and row.cells:
                        row_data = []
                        for cell in row.cells:
                            cell_text = self._safe_cell_text_extraction(cell, full_text)
                            row_data.append(cell_text)
                        if row_data and any(cell.strip() for cell in row_data):  # At least one non-empty cell
                            rows.append(row_data)
                logger.debug(f"Method 1 (body_rows): extracted {len(rows)} rows")
                if rows:
                    return rows
            except Exception as e:
                logger.debug(f"Method 1 (body_rows) failed: {e}")
        
        # Method 2: Try header_rows + body_rows
        if hasattr(table, 'header_rows') and hasattr(table, 'body_rows'):
            try:
                all_rows = []
                if table.header_rows:
                    all_rows.extend(table.header_rows)
                if table.body_rows:
                    all_rows.extend(table.body_rows)
                
                for row in all_rows:
                    if hasattr(row, 'cells') and row.cells:
                        row_data = []
                        for cell in row.cells:
                            cell_text = self._safe_cell_text_extraction(cell, full_text)
                            row_data.append(cell_text)
                        if row_data and any(cell.strip() for cell in row_data):
                            rows.append(row_data)
                            
                logger.debug(f"Method 2 (header+body): extracted {len(rows)} rows")
                if rows:
                    return rows
            except Exception as e:
                logger.debug(f"Method 2 (header+body) failed: {e}")
        
        # Method 3: Try generic rows attribute
        if hasattr(table, 'rows') and table.rows:
            try:
                for row in table.rows:
                    if hasattr(row, 'cells') and row.cells:
                        row_data = []
                        for cell in row.cells:
                            cell_text = self._safe_cell_text_extraction(cell, full_text)
                            row_data.append(cell_text)
                        if row_data and any(cell.strip() for cell in row_data):
                            rows.append(row_data)
                            
                logger.debug(f"Method 3 (rows): extracted {len(rows)} rows")
                if rows:
                    return rows
            except Exception as e:
                logger.debug(f"Method 3 (rows) failed: {e}")
        
        logger.warning("All table extraction methods failed")
        return rows
    
    def _safe_cell_text_extraction(self, cell, full_text: str) -> str:
        """Safe cell text extraction with multiple fallback methods"""
        
        # Method 1: layout.text_anchor (most common)
        try:
            if hasattr(cell, 'layout') and hasattr(cell.layout, 'text_anchor'):
                text = self._safe_text_anchor_extraction(cell.layout.text_anchor, full_text)
                if text and text.strip():
                    return text.strip()
        except Exception as e:
            logger.debug(f"Method 1 (text_anchor) failed: {e}")
        
        # Method 2: direct text attribute
        try:
            if hasattr(cell, 'text'):
                text = str(cell.text).strip()
                if text:
                    return text
        except Exception as e:
            logger.debug(f"Method 2 (direct text) failed: {e}")
        
        # Method 3: content attribute
        try:
            if hasattr(cell, 'content'):
                text = str(cell.content).strip()
                if text:
                    return text
        except Exception as e:
            logger.debug(f"Method 3 (content) failed: {e}")
        
        # Method 4: try to get text from cell children
        try:
            if hasattr(cell, 'layout') and hasattr(cell.layout, 'text'):
                text = str(cell.layout.text).strip()
                if text:
                    return text
        except Exception as e:
            logger.debug(f"Method 4 (layout.text) failed: {e}")
        
        return ""
    
    def _safe_text_anchor_extraction(self, text_anchor, full_text: str) -> str:
        """Safe text anchor extraction with robust error handling"""
        
        if not text_anchor:
            return ""
        
        # Handle string directly
        if isinstance(text_anchor, str):
            return text_anchor.strip()
        
        # Try content attribute
        try:
            if hasattr(text_anchor, 'content'):
                content = text_anchor.content
                if content:
                    return str(content).strip()
        except Exception as e:
            logger.debug(f"Content extraction failed: {e}")
        
        # Extract from text segments (most complex but most reliable)
        try:
            if hasattr(text_anchor, 'text_segments') and text_anchor.text_segments:
                text_parts = []
                for segment in text_anchor.text_segments:
                    try:
                        if hasattr(segment, 'start_index') and hasattr(segment, 'end_index'):
                            start = int(segment.start_index) if segment.start_index is not None else 0
                            end = int(segment.end_index) if segment.end_index is not None else len(full_text)
                            
                            # Boundary checks to prevent crashes
                            start = max(0, min(start, len(full_text)))
                            end = max(start, min(end, len(full_text)))
                            
                            if start < end:
                                extracted = full_text[start:end]
                                if extracted:  # Only add non-empty segments
                                    text_parts.append(extracted)
                                    
                    except (ValueError, TypeError, AttributeError) as e:
                        logger.debug(f"Text segment extraction error: {e}")
                        continue
                
                if text_parts:
                    result = "".join(text_parts).strip()
                    # Clean up common extraction artifacts
                    result = re.sub(r'\s+', ' ', result)  # Multiple spaces
                    result = re.sub(r'[\x00-\x1f\x7f-\x9f]', '', result)  # Control chars
                    return result
        except Exception as e:
            logger.debug(f"Text segments extraction failed: {e}")
        
        return ""
    
    def _process_table_rows(self, table_data: List[List[str]], result: Dict[str, Any]):
        """Process extracted table rows into markers"""
        
        for row_idx, row in enumerate(table_data):
            try:
                if self._is_valid_marker_row(row):
                    marker = self._create_safe_marker(row)
                    if marker:
                        self._add_marker_to_result(marker, result)
            except Exception as e:
                logger.debug(f"Error processing row {row_idx}: {e}")
                continue
    
    def _is_valid_marker_row(self, row: List[str]) -> bool:
        """Validate if row contains valid marker data - enhanced validation"""
        
        if len(row) < 2:
            return False
        
        test_name = row[0].strip()
        result_value = row[1].strip() if len(row) > 1 else ""
        
        # Must have test name and result
        if not test_name or not result_value:
            return False
        
        # Length validation
        if len(test_name) < 2 or len(test_name) > 200:
            return False
        
        # Exclude obvious non-medical terms
        exclusion_patterns = [
            # Header patterns
            r'^(seite|page|datum|date|patient|name|einheit|unit|ergebnis|result|referenz|test|parameter)',
            # Addresses and contact info
            r'(straße|str\.|plz|telefon|phone|fax|email|@|www\.)',
            # Document structure
            r'^[-=]+$',
            r'^\d+$',
            r'^(von|to|from|der|die|das|ein|eine|für|for|with|mit)',
            # Navigation/UI elements
            r'(eingang|ausgang|entry|exit)$',
            # Pure numbers without context
            r'^\d+[.,]\d+$'
        ]
        
        for pattern in exclusion_patterns:
            if re.match(pattern, test_name, re.I):
                return False
        
        # Result must contain numeric value, comparison operator, or status
        if not re.search(r'[\d.,<>≤≥±]|negativ|positiv|normal|erhöht|niedrig|high|low', result_value, re.I):
            return False
        
        # Additional check: test name should contain letters
        if not re.search(r'[a-zA-ZäöüßÄÖÜ]', test_name):
            return False
        
        return True
    
    def _create_safe_marker(self, row: List[str]) -> Optional[BloodMarker]:
        """Create BloodMarker object with enhanced safety"""
        
        try:
            test_name = row[0].strip()
            result = row[1].strip() if len(row) > 1 else ""
            unit = row[2].strip() if len(row) > 2 else ""
            reference = row[3].strip() if len(row) > 3 else ""
            
            # Enhanced unit extraction from result if needed
            if not unit and result:
                # Look for unit patterns at the end of result
                unit_match = re.search(r'(\d+(?:[.,]\d+)?)\s+([a-zA-Zµ/%]+(?:/[a-zA-Zµ]+)?)$', result)
                if unit_match:
                    result = unit_match.group(1)
                    unit = unit_match.group(2)
                else:
                    # Check for space-separated values
                    parts = result.split()
                    if len(parts) >= 2:
                        potential_unit = parts[-1]
                        if re.match(r'^[a-zA-Zµ/%]+(?:/[a-zA-Zµ]+)?$', potential_unit):
                            result = " ".join(parts[:-1])
                            unit = potential_unit
            
            # Clean up truncated units (common issue in Cloud Run)
            unit = self._fix_truncated_unit(unit)
            
            # Determine category
            category = self._classify_marker(test_name)
            
            # Check if critical value
            is_critical = self._is_critical_value(result, reference)
            
            # Create marker with validation
            marker = BloodMarker(
                test=test_name,
                result=result,
                unit=unit,
                reference_range=reference,
                category=category,
                is_critical=is_critical
            )
            
            return marker
            
        except ValidationError as e:
            logger.debug(f"Marker validation failed: {e}")
            return None
        except Exception as e:
            logger.debug(f"Unexpected error creating marker: {e}")
            return None
    
    def _fix_truncated_unit(self, unit: str) -> str:
        """Fix common unit truncation issues"""
        
        if not unit:
            return unit
        
        # Common truncations in Cloud Run
        unit_fixes = {
            'mg/': 'mg/l',
            'µg/': 'µg/l',
            'ng/': 'ng/ml',
            'pg/': 'pg/ml',
            'mmol/': 'mmol/l',
            'pmol/': 'pmol/l',
            'op': '%',  # Common OCR error
            '1000/': '1000/µl',
            'Mill/': 'Mill/µl'
        }
        
        return unit_fixes.get(unit, unit)
    
    def _pattern_extraction_fallback(self, document, result: Dict[str, Any]):
        """Fallback pattern-based extraction when tables fail"""
        
        logger.info("Using pattern-based extraction as fallback...")
        
        if not hasattr(document, 'text') or not document.text:
            logger.warning("Document has no text for pattern extraction")
            return
        
        full_text = document.text
        
        # Enhanced patterns for medical data
        marker_patterns = [
            # Pattern 1: Test Value Unit (Reference)
            re.compile(r'^([A-Za-zäöüßÄÖÜ\s\-()]+?)\s+([\d.,<>≤≥±]+)\s+([a-zA-Zµ/%]+(?:/[a-zA-Zµ]+)?)\s*(?:\(?([\d.,\-\s<>≤≥±%]+)?\)?)?', re.M),
            # Pattern 2: Test: Value Unit
            re.compile(r'^([A-Za-zäöüßÄÖÜ\s\-()]+?):\s+([\d.,<>≤≥±]+)\s+([a-zA-Zµ/%]+(?:/[a-zA-Zµ]+)?)', re.M),
            # Pattern 3: Test Tab Value Tab Unit
            re.compile(r'^([A-Za-zäöüßÄÖÜ\s\-()]+?)\t+([\d.,<>≤≥±]+)\t+([a-zA-Zµ/%]+(?:/[a-zA-Zµ]+)?)', re.M)
        ]
        
        extracted_tests = set()
        
        for pattern in marker_patterns:
            try:
                matches = pattern.finditer(full_text)
                for match in matches:
                    try:
                        test_name = match.group(1).strip()
                        
                        # Skip if already extracted or invalid
                        if test_name in extracted_tests or not self._is_valid_test_name(test_name):
                            continue
                        
                        groups = match.groups()
                        if len(groups) >= 3:
                            marker = BloodMarker(
                                test=test_name,
                                result=groups[1].strip(),
                                unit=groups[2].strip(),
                                reference_range=groups[3].strip() if len(groups) > 3 and groups[3] else "",
                                category=self._classify_marker(test_name)
                            )
                            
                            self._add_marker_to_result(marker, result)
                            extracted_tests.add(test_name)
                            
                    except Exception as e:
                        logger.debug(f"Pattern match error: {e}")
                        continue
                        
            except Exception as e:
                logger.warning(f"Pattern extraction error: {e}")
                continue
    
    def _is_valid_test_name(self, test_name: str) -> bool:
        """Check if test name is valid for medical marker"""
        
        # Must contain letters and be reasonable length
        if not re.search(r'[a-zA-ZäöüßÄÖÜ]', test_name) or len(test_name) < 3:
            return False
        
        # Known medical terms (partial list)
        medical_keywords = [
            'vitamin', 'ferritin', 'calcium', 'magnesium', 'zink', 'selen',
            'leukoz', 'erythroz', 'hämoglobin', 'hämatokrit', 'thromboz',
            'crp', 'tsh', 'linol', 'omega', 'epa', 'dha'
        ]
        
        test_lower = test_name.lower()
        for keyword in medical_keywords:
            if keyword in test_lower:
                return True
        
        # Exclude obvious non-medical terms
        exclude_keywords = [
            'straße', 'telefon', 'email', 'datum', 'seite', 'eingang', 'ausgang'
        ]
        
        for keyword in exclude_keywords:
            if keyword in test_lower:
                return False
        
        return True
    
    def _safe_form_field_extraction(self, document, result: Dict[str, Any]):
        """Safe form field extraction for patient information"""
        
        logger.info("Extracting form fields...")
        
        try:
            if not hasattr(document, 'pages') or not document.pages:
                return
            
            for page in document.pages:
                if not hasattr(page, 'form_fields') or not page.form_fields:
                    continue
                
                for form_field in page.form_fields:
                    try:
                        field_name = self._safe_text_anchor_extraction(
                            getattr(form_field, 'field_name', None), document.text
                        )
                        field_value = self._safe_text_anchor_extraction(
                            getattr(form_field, 'field_value', None), document.text
                        )
                        
                        if field_name and field_value:
                            self._map_field_to_result(field_name, field_value, result)
                            
                    except Exception as e:
                        logger.debug(f"Form field extraction error: {e}")
                        continue
                        
        except Exception as e:
            logger.warning(f"Form field processing failed: {e}")
    
    def _classify_marker(self, test_name: str) -> Optional[MarkerCategory]:
        """Classify marker into appropriate category"""
        
        test_lower = test_name.lower()
        
        for category, patterns in self.category_patterns.items():
            # Check keywords
            for keyword in patterns['keywords']:
                if keyword in test_lower:
                    return category
            
            # Check regex
            if patterns['regex'].search(test_name):
                return category
        
        # Default to clinical chemistry if no match
        return MarkerCategory.CLINICAL_CHEMISTRY
    
    def _is_critical_value(self, result: str, reference: str) -> bool:
        """Check if value is critical based on reference range"""
        
        if not reference:
            return False
        
        # Check for critical indicators
        critical_patterns = [
            r'\*+',
            r'kritisch',
            r'critical',
            r'alarm',
            r'↑↑',
            r'↓↓',
            r'sehr (hoch|niedrig)',
            r'very (high|low)'
        ]
        
        for pattern in critical_patterns:
            if re.search(pattern, reference, re.I) or re.search(pattern, result, re.I):
                return True
        
        return False
    
    def _add_marker_to_result(self, marker: BloodMarker, result: Dict[str, Any]):
        """Add marker to appropriate category in result"""
        
        # Convert marker to dict
        marker_dict = {
            "test": marker.test,
            "result": marker.result,
            "unit": marker.unit,
            "reference_range": marker.reference_range
        }
        
        if marker.is_critical:
            marker_dict["critical"] = True
            result["extraction_stats"]["critical_values"].append(marker.test)
        
        # Add to appropriate category
        if marker.category == MarkerCategory.FATTY_ACIDS:
            # Classify fatty acid subcategory
            fa_category = self._classify_fatty_acid(marker.test)
            if fa_category in result["fatty_acids"]:
                # Avoid duplicates
                if not any(m["test"] == marker.test for m in result["fatty_acids"][fa_category]):
                    result["fatty_acids"][fa_category].append(marker_dict)
        else:
            # Regular categories
            category_key = marker.category.value
            if category_key in result:
                # Avoid duplicates
                if not any(m["test"] == marker.test for m in result[category_key]):
                    result[category_key].append(marker_dict)
        
        result["extraction_stats"]["total_markers_found"] += 1
    
    def _classify_fatty_acid(self, test_name: str) -> str:
        """Classify fatty acid into subcategory"""
        
        test_lower = test_name.lower()
        
        omega3_keywords = ['alpha-linolen', 'epa', 'dha', 'docosapentaen-n3', 'omega-3', 'omega 3']
        omega6_keywords = ['gamma-linolen', 'dihomo', 'linol', 'arachidon', 'docosatetraen', 
                          'docosapentaen-n6', 'omega-6', 'omega 6']
        mono_keywords = ['olein', 'palmitolein', 'gondo', 'nervon', 'einfach ungesättigt']
        trans_keywords = ['trans', 'elaidin']
        saturated_keywords = ['myristin', 'palmitin', 'stearin', 'arachin', 'behen', 
                             'lignocerin', 'gesättigt', 'saturated']
        
        if any(kw in test_lower for kw in omega3_keywords):
            return "omega_3_fatty_acids"
        elif any(kw in test_lower for kw in omega6_keywords):
            return "omega_6_fatty_acids"
        elif any(kw in test_lower for kw in mono_keywords):
            return "monounsaturated_fatty_acids"
        elif any(kw in test_lower for kw in trans_keywords):
            return "trans_fatty_acids"
        elif any(kw in test_lower for kw in saturated_keywords):
            return "saturated_fatty_acids"
        else:
            return "omega_3_fatty_acids"  # Default
    
    def _map_field_to_result(self, field_name: str, field_value: str, result: Dict[str, Any]):
        """Map form field to result structure"""
        
        field_name_lower = field_name.lower()
        
        # Patient information mappings
        patient_mappings = {
            ('name', 'patient'): ('patient_info', 'name'),
            ('geboren', 'birth', 'geburt'): ('patient_info', 'birth_date_gender'),
            ('tagebuch', 'diary', 'nummer'): ('patient_info', 'diary_number'),
            ('eingang', 'entry', 'received'): ('patient_info', 'entry_date'),
            ('ausgang', 'exit', 'report'): ('patient_info', 'exit_date')
        }
        
        # Header information mappings
        header_mappings = {
            ('direktor', 'director', 'leitung'): ('header', 'medical_director'),
            ('wissenschaft', 'scientist'): ('header', 'scientists'),
            ('adresse', 'address', 'straße'): ('header', 'address'),
            ('telefon', 'phone', 'contact'): ('header', 'contact'),
            ('versicher', 'insurance', 'kasse'): ('header', 'insurance'),
            ('entnahme', 'collection', 'datum'): ('header', 'collection_date'),
            ('uhrzeit', 'time', 'zeit'): ('header', 'collection_time')
        }
        
        # Check patient mappings
        for keywords, (section, field) in patient_mappings.items():
            if any(kw in field_name_lower for kw in keywords):
                result[section][field] = field_value
                return
        
        # Check header mappings
        for keywords, (section, field) in header_mappings.items():
            if any(kw in field_name_lower for kw in keywords):
                result[section][field] = field_value
                return
    
    def _safe_post_processing(self, result: Dict[str, Any]):
        """Safe post-processing with error handling"""
        
        logger.info("Starting post-processing...")
        
        try:
            # Remove duplicates
            self._remove_duplicates(result)
        except Exception as e:
            logger.warning(f"Duplicate removal failed: {e}")
        
        try:
            # Sort markers
            self._sort_markers(result)
        except Exception as e:
            logger.warning(f"Marker sorting failed: {e}")
        
        try:
            # Calculate confidence
            self._calculate_confidence(result)
        except Exception as e:
            logger.warning(f"Confidence calculation failed: {e}")
        
        try:
            # Validate against reference
            self._validate_against_reference(result)
        except Exception as e:
            logger.warning(f"Reference validation failed: {e}")
    
    def _remove_duplicates(self, result: Dict[str, Any]):
        """Remove duplicate markers"""
        
        for category in ["hematology", "clinical_chemistry", "hormones", 
                        "clinical_immunology", "metals_trace_elements", 
                        "micronutrients", "quotients"]:
            if category in result and isinstance(result[category], list):
                seen = {}
                unique_markers = []
                
                for marker in result[category]:
                    if not isinstance(marker, dict):
                        continue
                        
                    test_name = marker.get("test", "").lower()
                    
                    if test_name not in seen:
                        seen[test_name] = marker
                        unique_markers.append(marker)
                    else:
                        # Keep the one with more complete data
                        existing = seen[test_name]
                        if self._marker_completeness(marker) > self._marker_completeness(existing):
                            idx = unique_markers.index(existing)
                            unique_markers[idx] = marker
                            seen[test_name] = marker
                
                result[category] = unique_markers
    
    def _marker_completeness(self, marker: Dict[str, str]) -> int:
        """Calculate marker completeness score"""
        score = 0
        if marker.get("test"):
            score += 1
        if marker.get("result"):
            score += 2
        if marker.get("unit"):
            score += 1
        if marker.get("reference_range"):
            score += 1
        return score
    
    def _sort_markers(self, result: Dict[str, Any]):
        """Sort markers alphabetically"""
        
        for category in ["hematology", "clinical_chemistry", "hormones",
                        "clinical_immunology", "metals_trace_elements",
                        "micronutrients", "quotients"]:
            if category in result and isinstance(result[category], list):
                try:
                    result[category].sort(key=lambda x: x.get("test", ""))
                except Exception as e:
                    logger.debug(f"Failed to sort {category}: {e}")
        
        # Sort fatty acids
        try:
            fatty_acids = result.get("fatty_acids", {})
            for fa_category, fa_list in fatty_acids.items():
                if isinstance(fa_list, list):
                    fa_list.sort(key=lambda x: x.get("test", ""))
        except Exception as e:
            logger.debug(f"Failed to sort fatty acids: {e}")
    
    def _calculate_confidence(self, result: Dict[str, Any]):
        """Calculate extraction confidence"""
        
        try:
            stats = result.get("extraction_stats", {})
            total_markers = stats.get("total_markers_found", 0)
            markers_with_ref = stats.get("markers_with_reference", 0)
            
            if total_markers > 0:
                confidence = (markers_with_ref / total_markers) * 100
                stats["extraction_confidence"] = round(confidence, 2)
            else:
                stats["extraction_confidence"] = 0.0
        except Exception as e:
            logger.warning(f"Confidence calculation error: {e}")
            result["extraction_stats"]["extraction_confidence"] = 0.0
    
    def _validate_against_reference(self, result: Dict[str, Any]):
        """Validate against reference markers"""
        
        try:
            if not self.reference_markers:
                logger.info("No reference markers available for validation")
                return
            
            all_extracted = set()
            
            # Collect all extracted marker names
            for category in ["hematology", "clinical_chemistry", "hormones",
                            "clinical_immunology", "metals_trace_elements",
                            "micronutrients", "quotients"]:
                if category in result and isinstance(result[category], list):
                    for marker in result[category]:
                        if isinstance(marker, dict) and marker.get("test"):
                            all_extracted.add(marker["test"].lower())
            
            # Add fatty acids
            fatty_acids = result.get("fatty_acids", {})
            for fa_list in fatty_acids.values():
                if isinstance(fa_list, list):
                    for marker in fa_list:
                        if isinstance(marker, dict) and marker.get("test"):
                            all_extracted.add(marker["test"].lower())
            
            # Validation
            reference_names = set(self.reference_markers.keys())
            found_in_reference = all_extracted & reference_names
            not_in_reference = all_extracted - reference_names
            
            stats = result.get("extraction_stats", {})
            stats["markers_with_reference"] = len(found_in_reference)
            stats["markers_without_reference"] = len(not_in_reference)
            
            # Validation status
            if stats.get("total_markers_found", 0) < 5:
                stats["validation_status"] = "warning: low marker count"
            elif stats.get("extraction_confidence", 0) < 50:
                stats["validation_status"] = "warning: low confidence"
            else:
                stats["validation_status"] = "success"
                
        except Exception as e:
            logger.warning(f"Reference validation error: {e}")
            result["extraction_stats"]["validation_status"] = "validation failed"

def process_for_makecom(pdf_base64: str, processor_config: Dict[str, str]) -> Dict[str, Any]:
    """Process PDF for make.com integration - FIXED VERSION"""
    
    try:
        # Decode base64
        try:
            pdf_content = base64.b64decode(pdf_base64)
            logger.info(f"Decoded PDF: {len(pdf_content)} bytes")
        except Exception as e:
            logger.error(f"Base64 decode error: {e}")
            return {
                "status": "error",
                "message": f"Invalid base64 PDF data: {e}",
                "data": None
            }
        
        # Initialize fixed parser
        try:
            parser = ProductionMedicalParser(
                project_id=processor_config["project_id"],
                processor_id=processor_config["processor_id"],
                location=processor_config.get("location", "eu")
            )
        except Exception as e:
            logger.error(f"Parser initialization error: {e}")
            return {
                "status": "error",
                "message": f"Parser initialization failed: {e}",
                "data": None
            }
        
        # Process document
        try:
            result = parser.process_document(pdf_content=pdf_content)
            
            # Add success status
            result["status"] = "success"
            result["message"] = "Document processed successfully"
            
            logger.info(f"Processing successful: {result['extraction_stats']['total_markers_found']} markers found")
            
            return result
            
        except Exception as e:
            logger.error(f"Document processing error: {e}")
            return {
                "status": "error",
                "message": f"Document processing failed: {e}",
                "data": None
            }
        
    except Exception as e:
        logger.error(f"Unexpected error in process_for_makecom: {e}")
        return {
            "status": "error",
            "message": f"Unexpected processing error: {e}",
            "data": None
        }
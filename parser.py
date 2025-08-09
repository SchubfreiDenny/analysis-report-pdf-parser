"""
Streamlined Medical PDF Parser for Make.com Integration
Uses trained Google Document AI processor for accurate medical data extraction
Includes fallback support for processor availability
"""

import base64
import json
import logging
from typing import Dict, List, Any, Optional
from google.cloud import documentai
from google.api_core import exceptions

logger = logging.getLogger(__name__)


class MedicalPDFParser:
    """Simplified parser using trained Document AI processor with fallback"""
    
    def __init__(self, project_id: str, processor_id: str, location: str = "us", 
                 fallback_config: Optional[Dict[str, str]] = None):
        self.project_id = project_id
        self.processor_id = processor_id
        self.location = location
        self.fallback_config = fallback_config
        self.client = documentai.DocumentProcessorServiceClient()
        
        # Full processor resource name
        self.processor_name = f"projects/{project_id}/locations/{location}/processors/{processor_id}"
        logger.info(f"Initialized parser with processor: {self.processor_name}")
        
        # Check if processor is available
        self.active_processor = self._verify_processor()
    
    def _verify_processor(self) -> str:
        """Verify if processor exists and is available"""
        try:
            processor = self.client.get_processor(name=self.processor_name)
            if processor.state.name == "ENABLED":
                logger.info(f"Primary processor {self.processor_id} is available")
                return self.processor_name
        except exceptions.NotFound:
            logger.warning(f"Primary processor {self.processor_id} not found")
        except Exception as e:
            logger.warning(f"Error checking primary processor: {str(e)}")
        
        # Try fallback if configured
        if self.fallback_config:
            fallback_name = f"projects/{self.fallback_config['project_id']}/locations/{self.fallback_config['location']}/processors/{self.fallback_config['processor_id']}"
            try:
                processor = self.client.get_processor(name=fallback_name)
                if processor.state.name == "ENABLED":
                    logger.info(f"Using fallback processor {self.fallback_config['processor_id']}")
                    return fallback_name
            except Exception as e:
                logger.error(f"Fallback processor also failed: {str(e)}")
        
        # Use primary anyway and let it fail with proper error
        return self.processor_name
    
    def process_pdf(self, pdf_base64: str, filename: str = "document.pdf") -> Dict[str, Any]:
        """
        Process PDF using trained Document AI processor
        
        Args:
            pdf_base64: Base64 encoded PDF content
            filename: Original filename for reference
            
        Returns:
            Structured medical data extraction results
        """
        try:
            # Decode PDF content
            pdf_content = base64.b64decode(pdf_base64)
            logger.info(f"Processing PDF: {filename} ({len(pdf_content)} bytes)")
            
            # Create Document AI request
            document = documentai.RawDocument(
                content=pdf_content,
                mime_type="application/pdf"
            )
            
            request = documentai.ProcessRequest(
                name=self.active_processor,
                raw_document=document
            )
            
            # Process document
            result = self.client.process_document(request=request)
            document = result.document
            
            logger.info(f"Document processed successfully. Pages: {len(document.pages)}")
            
            # Extract structured data
            extracted_data = self._extract_medical_data(document)
            
            # Add metadata
            processor_used = self.active_processor.split('/')[-1]
            extracted_data.update({
                "status": "success",
                "message": "Document processed successfully",
                "filename": filename,
                "processing_metadata": {
                    "processor_id": processor_used,
                    "document_pages": len(document.pages),
                    "confidence_score": self._calculate_confidence(document)
                }
            })
            
            return extracted_data
            
        except exceptions.NotFound as e:
            # Try with fallback if primary failed
            if self.fallback_config and self.active_processor == self.processor_name:
                logger.info("Primary processor not found, trying fallback...")
                self.active_processor = f"projects/{self.fallback_config['project_id']}/locations/{self.fallback_config['location']}/processors/{self.fallback_config['processor_id']}"
                return self.process_pdf(pdf_base64, filename)
            
            logger.error(f"Processor not found: {str(e)}")
            return {
                "status": "error",
                "message": f"Processor not available: {str(e)}",
                "filename": filename,
                "extracted_data": {}
            }
            
        except Exception as e:
            logger.error(f"Error processing PDF: {str(e)}", exc_info=True)
            return {
                "status": "error",
                "message": f"Processing failed: {str(e)}",
                "filename": filename,
                "extracted_data": {}
            }
    
    def _extract_medical_data(self, document: documentai.Document) -> Dict[str, Any]:
        """Extract structured medical data from processed document"""
        
        medical_markers = []
        
        # Extract entities from trained processor
        for entity in document.entities:
            marker = self._process_entity(entity, document.text)
            if marker:
                medical_markers.append(marker)
        
        # If no entities, try to extract from tables
        if not medical_markers and document.pages:
            for page in document.pages:
                if page.tables:
                    markers_from_tables = self._extract_from_tables(page.tables, document.text)
                    medical_markers.extend(markers_from_tables)
        
        # Organize by categories
        categorized_data = self._categorize_markers(medical_markers)
        
        # Add summary statistics
        total_markers = len(medical_markers)
        markers_with_values = len([m for m in medical_markers if m.get('result')])
        
        return {
            **categorized_data,
            "extraction_stats": {
                "total_markers_found": total_markers,
                "markers_with_values": markers_with_values,
                "extraction_confidence": self._calculate_confidence(document),
                "categories_found": len([k for k, v in categorized_data.items() if isinstance(v, list) and v])
            }
        }
    
    def _process_entity(self, entity: documentai.Document.Entity, full_text: str) -> Optional[Dict[str, Any]]:
        """Process individual entity from trained processor"""
        
        try:
            # Get entity properties
            test_name = None
            result_value = None
            reference_range = None
            unit = None
            category = None
            
            # Extract main entity text
            entity_text = self._get_entity_text(entity, full_text)
            if not entity_text:
                return None
            
            # Process entity properties
            for prop in entity.properties:
                prop_text = self._get_entity_text(prop, full_text)
                
                if prop.type_ == "test_name":
                    test_name = prop_text
                elif prop.type_ == "result_value":
                    result_value = prop_text
                elif prop.type_ == "reference_range":
                    reference_range = prop_text
                elif prop.type_ == "unit":
                    unit = prop_text
                elif prop.type_ == "category":
                    category = prop_text
            
            # Build marker data
            if test_name:
                return {
                    "test": test_name.strip(),
                    "result": result_value.strip() if result_value else "",
                    "reference_range": reference_range.strip() if reference_range else "",
                    "unit": unit.strip() if unit else "",
                    "category": category.lower() if category else "clinical_chemistry",
                    "confidence": entity.confidence if hasattr(entity, 'confidence') else 0.0
                }
                
        except Exception as e:
            logger.warning(f"Error processing entity: {str(e)}")
            
        return None
    
    def _extract_from_tables(self, tables: List, full_text: str) -> List[Dict[str, Any]]:
        """Extract medical markers from tables as fallback"""
        markers = []
        
        for table in tables:
            if not hasattr(table, 'header_rows') or not hasattr(table, 'body_rows'):
                continue
            
            # Find column indices
            test_col = result_col = ref_col = unit_col = -1
            
            if table.header_rows:
                for i, cell in enumerate(table.header_rows[0].cells):
                    cell_text = self._get_cell_text(cell, full_text).lower()
                    if any(word in cell_text for word in ['test', 'parameter', 'analyt']):
                        test_col = i
                    elif any(word in cell_text for word in ['result', 'ergebnis', 'wert']):
                        result_col = i
                    elif any(word in cell_text for word in ['reference', 'referenz', 'norm']):
                        ref_col = i
                    elif any(word in cell_text for word in ['unit', 'einheit']):
                        unit_col = i
            
            # Extract data from body rows
            for row in table.body_rows:
                if test_col >= 0 and test_col < len(row.cells):
                    test_name = self._get_cell_text(row.cells[test_col], full_text)
                    
                    if test_name and not self._is_non_medical(test_name):
                        marker = {"test": test_name}
                        
                        if result_col >= 0 and result_col < len(row.cells):
                            marker["result"] = self._get_cell_text(row.cells[result_col], full_text)
                        
                        if ref_col >= 0 and ref_col < len(row.cells):
                            marker["reference_range"] = self._get_cell_text(row.cells[ref_col], full_text)
                        
                        if unit_col >= 0 and unit_col < len(row.cells):
                            marker["unit"] = self._get_cell_text(row.cells[unit_col], full_text)
                        
                        markers.append(marker)
        
        return markers
    
    def _get_cell_text(self, cell, full_text: str) -> str:
        """Extract text from table cell"""
        if hasattr(cell, 'layout') and hasattr(cell.layout, 'text_anchor'):
            return self._get_entity_text(cell.layout, full_text)
        return ""
    
    def _get_entity_text(self, entity: documentai.Document.Entity, full_text: str) -> str:
        """Extract text from entity with robust error handling"""
        
        try:
            if hasattr(entity, 'text_anchor') and entity.text_anchor:
                text_segments = []
                for segment in entity.text_anchor.text_segments:
                    start = int(segment.start_index) if hasattr(segment, 'start_index') else 0
                    end = int(segment.end_index) if hasattr(segment, 'end_index') else len(full_text)
                    
                    # Safety bounds check
                    start = max(0, min(start, len(full_text)))
                    end = max(start, min(end, len(full_text)))
                    
                    text_segments.append(full_text[start:end])
                
                return " ".join(text_segments).strip()
                
        except Exception as e:
            logger.debug(f"Text extraction failed: {str(e)}")
            
        return ""
    
    def _is_non_medical(self, text: str) -> bool:
        """Check if text is non-medical content"""
        non_medical_terms = [
            'straße', 'strasse', 'telefon', 'email', 'datum', 'seite',
            'page', 'eingang', 'ausgang', 'unterschrift', 'adresse'
        ]
        text_lower = text.lower()
        return any(term in text_lower for term in non_medical_terms)
    
    def _categorize_markers(self, markers: List[Dict[str, Any]]) -> Dict[str, List[Dict[str, Any]]]:
        """Organize markers by medical categories"""
        
        categories = {
            "hematology": [],
            "hormones": [],
            "fatty_acids": [],
            "clinical_chemistry": [],
            "metals_trace_elements": [],
            "micronutrients": [],
            "clinical_immunology": []
        }
        
        for marker in markers:
            category = marker.get('category', 'clinical_chemistry')
            if category in categories:
                # Remove category field from individual marker
                clean_marker = {k: v for k, v in marker.items() if k != 'category'}
                categories[category].append(clean_marker)
        
        return categories
    
    def _calculate_confidence(self, document: documentai.Document) -> float:
        """Calculate overall extraction confidence"""
        
        if not document.entities:
            return 0.0
        
        confidences = []
        for entity in document.entities:
            if hasattr(entity, 'confidence'):
                confidences.append(entity.confidence)
        
        if confidences:
            return round(sum(confidences) / len(confidences) * 100, 1)
        
        return 85.0  # Default confidence for trained processor


def process_for_makecom(pdf_base64: str, processor_config: Dict[str, str], 
                        fallback_config: Optional[Dict[str, str]] = None) -> Dict[str, Any]:
    """
    Main entry point for Make.com integration
    
    Args:
        pdf_base64: Base64 encoded PDF content
        processor_config: Configuration with project_id, processor_id, location
        fallback_config: Optional fallback processor configuration
        
    Returns:
        Structured medical data for Make.com
    """
    
    try:
        # Initialize parser with fallback support
        parser = MedicalPDFParser(
            project_id=processor_config["project_id"],
            processor_id=processor_config["processor_id"],
            location=processor_config.get("location", "us"),
            fallback_config=fallback_config
        )
        
        # Process document
        result = parser.process_pdf(pdf_base64)
        
        logger.info(f"Processing completed: {result.get('status', 'unknown')}")
        return result
        
    except Exception as e:
        logger.error(f"Make.com processing failed: {str(e)}", exc_info=True)
        return {
            "status": "error",
            "message": f"Processing failed: {str(e)}",
            "extracted_data": {}
        }
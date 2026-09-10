"""Small provider interfaces that keep external services optional and replaceable."""
from __future__ import annotations
from abc import ABC, abstractmethod
from typing import Any, Dict, Optional

class MapTileProvider(ABC):
    @abstractmethod
    def configuration(self) -> Dict[str, Any]: ...

class DevelopmentProvider(MapTileProvider):
    def configuration(self):
        return {"url": "https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png", "attribution": "© OpenStreetMap contributors", "requires_key": False}

class OpenMapProvider(DevelopmentProvider):
    pass

class OptionalFutureProvider(MapTileProvider):
    def __init__(self, url: str, attribution: str): self.url, self.attribution = url, attribution
    def configuration(self): return {"url": self.url, "attribution": self.attribution, "requires_key": False}

class GeocodingProvider(ABC):
    @abstractmethod
    def geocode(self, query: str) -> Optional[Dict[str, float]]: ...

class LocalNoOpGeocoder(GeocodingProvider):
    def geocode(self, query: str): return None

class DocumentProvider(ABC):
    @abstractmethod
    def get_document(self, document_id: str) -> Dict[str, Any]: ...

class OCRProvider(ABC):
    @abstractmethod
    def extract_text(self, payload: bytes) -> Dict[str, Any]: ...

class AIProvider(ABC):
    @abstractmethod
    def extract_fields(self, text: str) -> Dict[str, Any]: ...

class PropertyResolver(ABC):
    @abstractmethod
    def resolve(self, fields: Dict[str, Any]) -> Dict[str, Any]: ...

class GISProvider(ABC):
    @abstractmethod
    def parcels(self, bbox: Optional[tuple] = None) -> list[dict]: ...

class MeasurementProvider(ABC):
    @abstractmethod
    def area_difference(self, document_area: float, parcel_area: float) -> float: ...

class VerificationEngine(ABC):
    @abstractmethod
    def compare(self, document: Dict[str, Any], parcel: Dict[str, Any]) -> Dict[str, Any]: ...

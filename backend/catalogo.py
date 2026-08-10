"""
Catálogo de departamentos y ciudades habilitadas para reportar, con el
centroide aproximado de cada ciudad. Es la fuente única del backend; el
frontend lleva una copia inline (comentada como espejo de este archivo)
para que el formulario funcione sin conexión.

Los centroides son aproximados a propósito: el pin cae en el centro de la
ciudad y el reportante puede arrastrarlo (ubicacion_ajustada=1). No se usa
ninguna API de geocoding: en emergencia no se depende de servicios externos.
"""

from __future__ import annotations

# departamento -> ciudad -> (lat, lng)
CATALOGO: dict[str, dict[str, tuple[float, float]]] = {
    "Valle del Cauca": {
        "Cali": (3.4516, -76.5320),
        "Palmira": (3.5394, -76.3036),
        "Buenaventura": (3.8801, -77.0312),
        "Tuluá": (4.0847, -76.1954),
        "Buga": (3.9008, -76.2983),
        "Jamundí": (3.2610, -76.5340),
        "Yumbo": (3.5849, -76.4959),
        "Cartago": (4.7464, -75.9117),
    },
    "Quindío": {
        "Armenia": (4.5339, -75.6811),
        "Calarcá": (4.5296, -75.6413),
        "Montenegro": (4.5664, -75.7505),
        "La Tebaida": (4.4525, -75.7877),
        "Circasia": (4.6194, -75.6353),
        "Quimbaya": (4.6236, -75.7625),
    },
    "Chocó": {
        "Quibdó": (5.6947, -76.6611),
        "Istmina": (5.1606, -76.6839),
        "Condoto": (5.0912, -76.6499),
        "Tadó": (5.2652, -76.5586),
        "Bahía Solano": (6.2244, -77.4014),
    },
    "Cauca": {
        "Popayán": (2.4448, -76.6147),
        "Santander de Quilichao": (3.0094, -76.4846),
        "Puerto Tejada": (3.2341, -76.4180),
        "Guapi": (2.5716, -77.8858),
    },
    "Risaralda": {
        "Pereira": (4.8087, -75.6906),
        "Dosquebradas": (4.8318, -75.6731),
        "Santa Rosa de Cabal": (4.8686, -75.6214),
        "La Virginia": (4.8996, -75.8828),
    },
}


def ciudad_valida(departamento: str, ciudad: str) -> bool:
    return ciudad in CATALOGO.get(departamento, {})


def centroide(departamento: str, ciudad: str) -> tuple[float, float] | None:
    return CATALOGO.get(departamento, {}).get(ciudad)


def como_json() -> dict:
    """Forma que consume el frontend y el futuro bot de WhatsApp."""
    return {
        depto: {ciudad: {"lat": lat, "lng": lng} for ciudad, (lat, lng) in ciudades.items()}
        for depto, ciudades in CATALOGO.items()
    }

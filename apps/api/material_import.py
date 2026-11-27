"""Read selectable PDF/PPTX text without executing or extracting document contents."""

from io import BytesIO
from pathlib import PurePosixPath
import re
from xml.etree import ElementTree as ET
from zipfile import BadZipFile, ZipFile

from pypdf import PdfReader


MAX_FILE_BYTES = 40 * 1024 * 1024
MAX_PAGES = 100
MAX_TEXT = 1_000_000
MAX_UNCOMPRESSED = 200 * 1024 * 1024


def _xml(data: bytes):
    if len(data) > 10 * 1024 * 1024 or b"<!DOCTYPE" in data.upper() or b"<!ENTITY" in data.upper():
        raise ValueError("El archivo contiene una estructura XML no admitida.")
    return ET.fromstring(data)


def extract_pages(filename: str, data: bytes) -> tuple[str, list[dict]]:
    if not data or len(data) > MAX_FILE_BYTES:
        raise ValueError("Carga un archivo de hasta 40 MB que contenga texto seleccionable.")
    suffix = PurePosixPath(filename.replace("\\", "/")).suffix.lower()
    if suffix not in {".pdf", ".pptx"}:
        raise ValueError("Usa un PDF o una presentación PowerPoint .pptx.")
    pages = []
    try:
        if suffix == ".pdf":
            if not data.startswith(b"%PDF-"):
                raise ValueError("El archivo no es un PDF válido.")
            reader = PdfReader(BytesIO(data))
            if reader.is_encrypted and not reader.decrypt(""):
                raise ValueError("El PDF está protegido. Carga una copia sin contraseña.")
            if len(reader.pages) > MAX_PAGES:
                raise ValueError("Divide el documento en archivos de hasta 100 páginas.")
            for index, page in enumerate(reader.pages, 1):
                pages.append({"number": index, "label": f"Página {index}", "text": (page.extract_text() or "").strip()})
        else:
            with ZipFile(BytesIO(data)) as archive:
                entries = archive.infolist()
                if len(entries) > 5000 or sum(entry.file_size for entry in entries) > MAX_UNCOMPRESSED:
                    raise ValueError("La presentación contiene demasiados datos descomprimidos.")
                for entry in entries:
                    path = PurePosixPath(entry.filename.replace("\\", "/"))
                    if path.is_absolute() or ".." in path.parts or entry.flag_bits & 1:
                        raise ValueError("La presentación contiene archivos internos no admitidos.")
                    if "vbaproject" in entry.filename.lower():
                        raise ValueError("Carga una presentación .pptx sin macros.")
                names = set(archive.namelist())
                if "ppt/presentation.xml" not in names or "ppt/_rels/presentation.xml.rels" not in names:
                    raise ValueError("El archivo no es una presentación PowerPoint válida.")
                relations = {
                    node.attrib["Id"]: node.attrib.get("Target", "")
                    for node in _xml(archive.read("ppt/_rels/presentation.xml.rels"))
                    if node.attrib.get("TargetMode") != "External"
                }
                presentation = _xml(archive.read("ppt/presentation.xml"))
                slide_ids = presentation.findall(".//{http://schemas.openxmlformats.org/presentationml/2006/main}sldId")
                if len(slide_ids) > MAX_PAGES:
                    raise ValueError("Divide la presentación en archivos de hasta 100 diapositivas.")
                for index, slide in enumerate(slide_ids, 1):
                    target = relations.get(slide.attrib.get("{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id"))
                    if not target:
                        raise ValueError("No se pudo localizar una diapositiva de la presentación.")
                    part = target.lstrip("/") if target.startswith("/") else "ppt/" + target
                    if ".." in PurePosixPath(part).parts or not re.fullmatch(r"ppt/slides/slide\d+\.xml", part):
                        raise ValueError("La presentación contiene una referencia de diapositiva no admitida.")
                    tree = _xml(archive.read(part))
                    text = "\n".join(
                        "".join(node.itertext()).strip()
                        for node in tree.findall(".//{http://schemas.openxmlformats.org/drawingml/2006/main}t")
                    )
                    pages.append({"number": index, "label": f"Diapositiva {index}", "text": text.strip()})
    except ValueError:
        raise
    except (Exception,) as error:
        raise ValueError("No pudimos leer el archivo. Usa una copia válida con texto seleccionable.") from error
    if not pages or not any(page["text"].strip() for page in pages):
        raise ValueError("El archivo no tiene texto seleccionable. Puedes pegar su contenido como texto.")
    if sum(len(page["text"]) for page in pages) > MAX_TEXT:
        raise ValueError("El documento contiene demasiado texto. Carga una sección más breve.")
    return suffix[1:], pages

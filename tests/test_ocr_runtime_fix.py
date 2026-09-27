import sys
import types


def test_ocr_runtime_patch_installs_and_falls_back(monkeypatch):
    fake_pipeline = types.SimpleNamespace(_OCR_RUNTIME_RELIABILITY_INSTALLED=False)

    class FakeImageOps:
        @staticmethod
        def exif_transpose(x): return x
        @staticmethod
        def autocontrast(x, cutoff=0.5): return x

    fake_pytesseract = types.SimpleNamespace(
        pytesseract=types.SimpleNamespace(tesseract_cmd="tesseract"),
        Output=types.SimpleNamespace(DICT="dict"),
        get_languages=lambda config="": ["eng"],
        image_to_data=lambda *args, **kwargs: {
            "text": ["Survey", "128", "Village", "Test"],
            "conf": ["90", "92", "88", "91"],
            "block_num": [1, 1, 1, 1], "par_num": [1, 1, 1, 1], "line_num": [1, 1, 2, 2],
        },
    )

    fake_server = types.SimpleNamespace(
        locate_tesseract=lambda: "tesseract",
        tesseract_available=lambda: True,
        _TESSERACT_OK=None,
        SUPPORTED_LANGUAGES=[{"code": "eng"}],
        detect_primary_script=lambda text: "eng",
    )
    fake_pipeline.get_server = lambda: fake_server
    monkeypatch.setitem(sys.modules, "ocr_pipeline", fake_pipeline)
    monkeypatch.setitem(sys.modules, "pytesseract", fake_pytesseract)
    monkeypatch.setitem(sys.modules, "PIL", types.SimpleNamespace(ImageEnhance=types.SimpleNamespace(Contrast=lambda x: x, Sharpness=lambda x: x), ImageFilter=types.SimpleNamespace(MedianFilter=lambda size: None), ImageOps=FakeImageOps))

    import ocr_runtime_fix
    ocr_runtime_fix.install()
    result = fake_pipeline.run_fast_ocr(object(), "auto")
    assert result["word_count"] == 4
    assert "Survey 128" in result["text"]
    assert result["confidence"] > 0.8


def test_ocr_runtime_patch_surfaces_blank_engine_error(monkeypatch):
    fake_pipeline = types.SimpleNamespace(_OCR_RUNTIME_RELIABILITY_INSTALLED=False)
    class Ops:
        @staticmethod
        def exif_transpose(x): return x
        @staticmethod
        def autocontrast(x, cutoff=0.5): return x
    class Enh:
        @staticmethod
        def Contrast(x): return Enh()
        @staticmethod
        def Sharpness(x): return Enh()
        def enhance(self, value): return self
    class Filter:
        @staticmethod
        def MedianFilter(size=3): return None
    fake_pytesseract = types.SimpleNamespace(
        pytesseract=types.SimpleNamespace(tesseract_cmd="tesseract"),
        Output=types.SimpleNamespace(DICT="dict"),
        get_languages=lambda config="": ["eng"],
        image_to_data=lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("binary failed")),
    )
    fake_server = types.SimpleNamespace(
        locate_tesseract=lambda: "tesseract", tesseract_available=lambda: True,
        _TESSERACT_OK=None, SUPPORTED_LANGUAGES=[{"code": "eng"}],
        detect_primary_script=lambda text: "eng",
    )
    fake_pipeline.get_server = lambda: fake_server
    monkeypatch.setitem(sys.modules, "ocr_pipeline", fake_pipeline)
    monkeypatch.setitem(sys.modules, "pytesseract", fake_pytesseract)
    monkeypatch.setitem(sys.modules, "PIL", types.SimpleNamespace(ImageEnhance=Enh, ImageFilter=Filter, ImageOps=Ops))
    import importlib
    mod = importlib.import_module("ocr_runtime_fix")
    mod.install()
    result = fake_pipeline.run_fast_ocr(object(), "auto")
    assert result["text"] == ""
    assert result["engine_error"]

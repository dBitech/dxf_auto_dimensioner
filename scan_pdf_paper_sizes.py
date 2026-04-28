#!/usr/bin/env python3
"""
Scan produced engineering PDFs and report the largest paper size needed for printing.

The script reads each PDF page MediaBox/CropBox, converts points to inches/mm,
and maps each page to the smallest ANSI engineering sheet that can contain it.
If a page exceeds ANSI E, it is reported as a custom sheet requirement.

When --show-pages is used, the script can also rasterize each page and estimate
ink/colour coverage. This is intended for print-cost estimation, especially for
distinguishing simple black line art from colour-heavy pages.
"""
from __future__ import annotations

import argparse
import math
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Optional

try:
    from pypdf import PdfReader
except ImportError:  # pragma: no cover - compatibility fallback
    try:
        from PyPDF2 import PdfReader  # type: ignore
    except ImportError:  # pragma: no cover
        PdfReader = None  # type: ignore


POINTS_PER_INCH = 72.0
MM_PER_INCH = 25.4


@dataclass(frozen=True)
class SheetSize:
    name: str
    width_in: float
    height_in: float

    @property
    def short_in(self) -> float:
        return min(self.width_in, self.height_in)

    @property
    def long_in(self) -> float:
        return max(self.width_in, self.height_in)

    @property
    def area_in2(self) -> float:
        return self.width_in * self.height_in


ANSI_SHEETS = [
    SheetSize("ANSI A", 8.5, 11.0),
    SheetSize("ANSI B", 11.0, 17.0),
    SheetSize("ANSI C", 17.0, 22.0),
    SheetSize("ANSI D", 22.0, 34.0),
    SheetSize("ANSI E", 34.0, 44.0),
]


@dataclass
class CoverageResult:
    white_pct: float
    ink_pct: float
    gray_black_pct: float
    color_pct: float
    c_pct: float
    m_pct: float
    y_pct: float
    k_pct: float


@dataclass
class PageResult:
    pdf_path: Path
    page_number: int
    width_in: float
    height_in: float
    required_sheet: Optional[SheetSize]
    coverage: Optional[CoverageResult] = None

    @property
    def short_in(self) -> float:
        return min(self.width_in, self.height_in)

    @property
    def long_in(self) -> float:
        return max(self.width_in, self.height_in)

    @property
    def is_custom(self) -> bool:
        return self.required_sheet is None


def iter_pdf_files(root: Path, recursive: bool) -> Iterable[Path]:
    pattern = "**/*.pdf" if recursive else "*.pdf"
    yield from sorted(root.glob(pattern), key=lambda p: str(p).lower())


def get_page_size_in(page, use_cropbox: bool = False) -> tuple[float, float]:
    box = page.cropbox if use_cropbox else page.mediabox
    width_pt = float(box.width)
    height_pt = float(box.height)
    return width_pt / POINTS_PER_INCH, height_pt / POINTS_PER_INCH


def choose_ansi_sheet(width_in: float, height_in: float, tolerance_in: float) -> Optional[SheetSize]:
    short = min(width_in, height_in)
    long = max(width_in, height_in)
    for sheet in ANSI_SHEETS:
        if short <= sheet.short_in + tolerance_in and long <= sheet.long_in + tolerance_in:
            return sheet
    return None


def estimate_page_coverage(
    pdf_path: Path,
    page_index_zero_based: int,
    dpi: int,
    white_threshold: int,
    gray_delta: int,
) -> CoverageResult:
    """
    Estimate print coverage by rasterizing one page and classifying pixels.

    Metrics:
    - white_pct: page area that is effectively blank/background.
    - ink_pct: area with any visible mark, equal to 100 - white_pct.
    - gray_black_pct: non-white neutral pixels, typical of black/gray line art.
    - color_pct: non-white pixels with meaningful RGB channel separation.
    - c/m/y/k_pct: process-colour area coverage estimates. These are averages
      over the full page area, not just over the marked pixels.

    The CMYK conversion is an RGB raster approximation, not a RIP/prepress
    separations analysis. It is good enough for cost triage but not for press
    calibration.
    """
    try:
        import fitz  # PyMuPDF
    except ImportError as exc:
        raise RuntimeError("Coverage estimation requires PyMuPDF. Install with: pip install PyMuPDF") from exc

    doc = fitz.open(str(pdf_path))
    try:
        page = doc.load_page(page_index_zero_based)
        zoom = dpi / POINTS_PER_INCH
        pix = page.get_pixmap(matrix=fitz.Matrix(zoom, zoom), alpha=False, colorspace=fitz.csRGB)
        data = pix.samples
        total = pix.width * pix.height
        if total <= 0:
            return CoverageResult(100.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0)

        white = 0
        gray_black = 0
        color = 0
        c_sum = 0.0
        m_sum = 0.0
        y_sum = 0.0
        k_sum = 0.0

        # RGB byte triplets. Use explicit loop to keep dependencies minimal.
        for i in range(0, len(data), 3):
            r = data[i]
            g = data[i + 1]
            b = data[i + 2]

            if r >= white_threshold and g >= white_threshold and b >= white_threshold:
                white += 1
                continue

            if max(r, g, b) - min(r, g, b) <= gray_delta:
                gray_black += 1
            else:
                color += 1

            # Approximate CMYK from RGB, normalized 0..1.
            rf = r / 255.0
            gf = g / 255.0
            bf = b / 255.0
            k = 1.0 - max(rf, gf, bf)
            if k >= 0.999:
                c = m = y = 0.0
            else:
                denom = 1.0 - k
                c = (1.0 - rf - k) / denom
                m = (1.0 - gf - k) / denom
                y = (1.0 - bf - k) / denom
            c_sum += max(0.0, min(1.0, c))
            m_sum += max(0.0, min(1.0, m))
            y_sum += max(0.0, min(1.0, y))
            k_sum += max(0.0, min(1.0, k))

        ink = total - white
        return CoverageResult(
            white_pct=white * 100.0 / total,
            ink_pct=ink * 100.0 / total,
            gray_black_pct=gray_black * 100.0 / total,
            color_pct=color * 100.0 / total,
            c_pct=c_sum * 100.0 / total,
            m_pct=m_sum * 100.0 / total,
            y_pct=y_sum * 100.0 / total,
            k_pct=k_sum * 100.0 / total,
        )
    finally:
        doc.close()


def fmt_pct(value: float) -> str:
    if value < 0.005:
        return "0.00%"
    return f"{value:.2f}%"


def fmt_inches(value: float, decimals: int = 2) -> str:
    return f'{value:.{decimals}f}"'


def fmt_mm(value_in: float, decimals: int = 1) -> str:
    return f"{value_in * MM_PER_INCH:.{decimals}f} mm"


def fmt_size(width_in: float, height_in: float, units: str) -> str:
    if units == "mm":
        return f"{fmt_mm(width_in)} x {fmt_mm(height_in)}"
    return f"{fmt_inches(width_in)} x {fmt_inches(height_in)}"


def analyze_pdfs(
    root: Path,
    recursive: bool,
    use_cropbox: bool,
    tolerance_in: float,
    estimate_coverage: bool,
    coverage_dpi: int,
    white_threshold: int,
    gray_delta: int,
) -> tuple[list[PageResult], list[tuple[Path, str]]]:
    if PdfReader is None:
        raise RuntimeError("Missing PDF reader dependency. Install with: pip install pypdf")

    results: list[PageResult] = []
    errors: list[tuple[Path, str]] = []

    for pdf_path in iter_pdf_files(root, recursive):
        try:
            reader = PdfReader(str(pdf_path))
            for idx, page in enumerate(reader.pages, start=1):
                width_in, height_in = get_page_size_in(page, use_cropbox=use_cropbox)
                sheet = choose_ansi_sheet(width_in, height_in, tolerance_in)
                coverage = None
                if estimate_coverage:
                    coverage = estimate_page_coverage(
                        pdf_path,
                        idx - 1,
                        coverage_dpi,
                        white_threshold,
                        gray_delta,
                    )
                results.append(PageResult(pdf_path, idx, width_in, height_in, sheet, coverage))
        except Exception as exc:  # keep batch scans useful even with one bad PDF
            errors.append((pdf_path, str(exc)))

    return results, errors


def print_report(results: list[PageResult], errors: list[tuple[Path, str]], root: Path, units: str, show_pages: bool, show_coverage: bool) -> int:
    if not results:
        print(f"No PDF pages found under: {root}")
        if errors:
            print("\nErrors:")
            for path, err in errors:
                print(f"  ERROR {path}: {err}")
        return 1

    max_physical = max(results, key=lambda r: r.short_in * r.long_in)
    max_short = max(r.short_in for r in results)
    max_long = max(r.long_in for r in results)

    fitted = [r for r in results if r.required_sheet is not None]
    custom = [r for r in results if r.required_sheet is None]
    largest_standard = None
    if fitted:
        largest_standard = max((r.required_sheet for r in fitted if r.required_sheet), key=lambda s: ANSI_SHEETS.index(s))

    pdf_count = len({r.pdf_path for r in results})
    print("PDF paper size scan")
    print("===================")
    print(f"Folder: {root}")
    print(f"PDFs scanned: {pdf_count}")
    print(f"Pages scanned: {len(results)}")
    print("")

    if custom:
        print("Largest paper required: CUSTOM / OVERSIZE")
        print(f"Minimum printable area needed, orientation independent: {fmt_size(max_short, max_long, units)}")
        print(f"Largest actual page encountered: {fmt_size(max_physical.width_in, max_physical.height_in, units)}")
        print(f"Largest page file: {max_physical.pdf_path} page {max_physical.page_number}")
        print(f"Pages exceeding ANSI E: {len(custom)}")
    else:
        assert largest_standard is not None
        print(f"Largest ANSI paper required: {largest_standard.name}")
        print(f"Sheet size: {fmt_size(largest_standard.width_in, largest_standard.height_in, units)}")
        print(f"Largest actual page encountered: {fmt_size(max_physical.width_in, max_physical.height_in, units)}")
        print(f"Largest page file: {max_physical.pdf_path} page {max_physical.page_number}")

    if show_pages:
        print("\nPer-page requirements")
        print("---------------------")
        if show_coverage:
            print(
                "Sheet     Size                  Ink     Gray/Blk  Color    C        M        Y        K        File / page"
            )
            for r in results:
                sheet_name = r.required_sheet.name if r.required_sheet else "CUSTOM"
                if r.coverage:
                    cov = (
                        f"{fmt_pct(r.coverage.ink_pct):>7s}  "
                        f"{fmt_pct(r.coverage.gray_black_pct):>7s}  "
                        f"{fmt_pct(r.coverage.color_pct):>7s}  "
                        f"{fmt_pct(r.coverage.c_pct):>7s}  "
                        f"{fmt_pct(r.coverage.m_pct):>7s}  "
                        f"{fmt_pct(r.coverage.y_pct):>7s}  "
                        f"{fmt_pct(r.coverage.k_pct):>7s}"
                    )
                else:
                    cov = "      -        -        -        -        -        -        -"
                print(
                    f"{sheet_name:8s}  {fmt_size(r.width_in, r.height_in, units):>20s}  "
                    f"{cov}  {r.pdf_path}  page {r.page_number}"
                )
            print(
                "\nCoverage notes: Ink is any non-white page area. Gray/Blk is neutral black/gray line art. "
                "Color is non-white, non-neutral area. C/M/Y/K are approximate process-colour area coverages "
                "from the rendered RGB page, suitable for print-cost triage rather than formal prepress separation."
            )
        else:
            for r in results:
                sheet_name = r.required_sheet.name if r.required_sheet else "CUSTOM"
                print(f"{sheet_name:8s}  {fmt_size(r.width_in, r.height_in, units):>20s}  {r.pdf_path}  page {r.page_number}")

    if errors:
        print("\nErrors")
        print("------")
        for path, err in errors:
            print(f"ERROR {path}: {err}")

    return 2 if custom else 0


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Scan PDF files and print the largest paper size needed for printing."
    )
    parser.add_argument(
        "folder",
        type=Path,
        help="Folder containing generated PDFs."
    )
    parser.add_argument(
        "-r", "--recursive",
        action="store_true",
        help="Scan nested subdirectories."
    )
    parser.add_argument(
        "--units",
        choices=["in", "mm"],
        default="in",
        help="Units used in the printed report. Default: in."
    )
    parser.add_argument(
        "--cropbox",
        action="store_true",
        help="Use the PDF CropBox instead of MediaBox. Default: MediaBox."
    )
    parser.add_argument(
        "--tolerance-in",
        type=float,
        default=0.03,
        help="Fit tolerance in inches when mapping pages to ANSI sheets. Default: 0.03."
    )
    parser.add_argument(
        "--show-pages",
        action="store_true",
        help="Print every page and its required sheet size. Also shows coverage unless --no-coverage is used."
    )
    parser.add_argument(
        "--no-coverage",
        action="store_true",
        help="Disable ink/colour coverage estimation in --show-pages output."
    )
    parser.add_argument(
        "--coverage-dpi",
        type=int,
        default=96,
        help="Raster DPI used for coverage estimation. Higher is slower but more precise. Default: 96."
    )
    parser.add_argument(
        "--white-threshold",
        type=int,
        default=248,
        help="RGB threshold from 0-255 used to classify near-white pixels as blank. Default: 248."
    )
    parser.add_argument(
        "--gray-delta",
        type=int,
        default=10,
        help="Maximum RGB channel spread used to classify non-white pixels as gray/black. Default: 10."
    )

    args = parser.parse_args(argv)
    root = args.folder.resolve()
    if not root.exists() or not root.is_dir():
        print(f"ERROR: folder does not exist or is not a directory: {root}", file=sys.stderr)
        return 1

    show_coverage = bool(args.show_pages and not args.no_coverage)
    if show_coverage:
        if args.coverage_dpi < 24 or args.coverage_dpi > 600:
            print("ERROR: --coverage-dpi must be between 24 and 600.", file=sys.stderr)
            return 1
        if not (0 <= args.white_threshold <= 255):
            print("ERROR: --white-threshold must be between 0 and 255.", file=sys.stderr)
            return 1
        if not (0 <= args.gray_delta <= 255):
            print("ERROR: --gray-delta must be between 0 and 255.", file=sys.stderr)
            return 1

    try:
        results, errors = analyze_pdfs(
            root,
            args.recursive,
            args.cropbox,
            args.tolerance_in,
            show_coverage,
            args.coverage_dpi,
            args.white_threshold,
            args.gray_delta,
        )
    except RuntimeError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    return print_report(results, errors, root, args.units, args.show_pages, show_coverage)


if __name__ == "__main__":
    raise SystemExit(main())

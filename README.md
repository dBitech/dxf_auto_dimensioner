# DXF Auto Dimensioner / Y14.5 PDF Exporter

Command-line utility to batch process DXF files, add conservative automatic annotations, and export 1:1 engineering PDFs with an auto-scaling title block.

## What is improved in this version

- Title block uses a fixed default 12 pt font, with automatic fitting down to a configurable minimum.
- Long file names and values are truncated with ellipses instead of overlapping adjacent cells.
- Title block height scales as a percentage of sheet height, bounded to avoid consuming the page.
- Recursive subdirectory processing is supported and preserves folder structure.
- Metric source files are the default; imperial and auto-detected DXF units are supported.
- Imperial output can use decimal or fractional inches.
- Oversized DXFs automatically get custom-size 1:1 PDF pages so parts are not clipped.

## Install

```bash
python -m venv .venv
. .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

## Basic usage

```bash
python dxf_auto_dimension.py -i ./input_dxfs -o ./output --recursive
```

## Unit handling

Metric source DXFs are assumed to be in millimetres.
Imperial source DXFs are assumed to be in inches.

```bash
# Metric source, metric output, recursive
python dxf_auto_dimension.py -i ./dxfs -o ./out --recursive --source-units metric --output-units metric

# Imperial source, fractional inch dimensions
python dxf_auto_dimension.py -i ./dxfs -o ./out --recursive --source-units imperial --output-units imperial --fractional-inches

# Use DXF $INSUNITS where available
python dxf_auto_dimension.py -i ./dxfs -o ./out --recursive --source-units auto --output-units source
```

## Title block controls

```bash
python dxf_auto_dimension.py -i ./dxfs -o ./out --recursive \
  --title-font-pt 12 \
  --min-title-font-pt 8 \
  --title-block-height-ratio 0.22 \
  --drawn-by "DB" \
  --revision A \
  --description "AUTO-DIMENSIONED PART"
```

## Sheet sizing and clipping prevention

The utility first tries ANSI A-E sheets at true 1:1 scale. If a part and its annotations are physically larger than ANSI E, the default behaviour is to create a custom PDF page that is large enough to fit the entire annotated drawing plus the title block.

```bash
# Default: allows custom oversized pages when needed
python dxf_auto_dimension.py -i ./dxfs -o ./out --recursive

# Force ANSI A-E only. Very large drawings can be clipped at 1:1.
python dxf_auto_dimension.py -i ./dxfs -o ./out --recursive --standard-sheets-only
```

## Output

For each input `part.dxf`, the utility writes:

- `part.annotated.dxf`
- `part.pdf`

When `--recursive` is used, nested source folders are preserved under the output directory.

## Notes

Automatic dimensioning is heuristic. This tool is useful for batch review drawings, quoting, fabrication aids, and fast documentation, but drawings should still be reviewed before production release.

## Annotation clutter controls

This version suppresses line-angle notes by default. It only adds clean feature-level notes:

- overall width and height dimensions
- circle diameter notes
- arc radius notes
- one diameter note for faceted circular polylines when detected

To restore angle labels on original non-orthogonal straight edges, opt in explicitly:

```bash
python dxf_auto_dimension.py -i ./dxfs -o ./out --recursive --angle-notes
```

The tool never uses generated dimension extension lines as inputs for feature annotation.

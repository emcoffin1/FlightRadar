# Aircraft silhouette source

The 182 local SVG aircraft silhouettes in this directory come from
[RexKramer1/AircraftShapesSVG](https://github.com/RexKramer1/AircraftShapesSVG),
downloaded on 2026-10-11 at commit
`0743718760c42a5e91801adc053b5b828a434a5e`.

The source project licenses the images under GNU GPL v3.0. Its original
`README.md` is retained here as `SOURCE_README.md`, and the full license is
retained as `LICENSE`.

The application uses exact ICAO type-designator filenames whenever possible,
with a small alias table in `app.py` for closely related or alternate codes.
Unknown types use `Unidentified.svg`.

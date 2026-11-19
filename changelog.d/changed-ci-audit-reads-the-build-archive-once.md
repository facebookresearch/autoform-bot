- **CI audit reads the build archive once.** The audit helper read each
  module's `.ilean` and `.trace` from the `lake pack` archive in sorted order,
  and every step back in the gzip stream decompressed the archive again from
  its start: on a 900-module project that took 40 minutes before the probe
  started. It now reads the archive once, in its own order, and runs the same
  checks on the members it kept, so the modules it accepts are unchanged. A
  project set up before this keeps its `.github/autoform_audit.py`: delete that
  file and rerun `autoform init`, which writes only missing files, to get it
  (#845).

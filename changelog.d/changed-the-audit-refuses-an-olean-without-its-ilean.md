- **The audit refuses an `.olean` that has no `.ilean` in the build archive.**
  The audit helper lists the root package's modules from the `.ilean` files in
  the `lake pack` archive, so a module whose `.olean` came without one was left
  off the list: a root module could import it, and it would load as a
  dependency, outside the root scan and the kernel replay. Lake writes both
  files for every module, so only crafted build output has one without the
  other. The helper now refuses such an archive and names the file. A project
  set up before this keeps its `.github/autoform_audit.py`: delete that file and
  rerun `autoform init`, which writes only missing files, to get it (#883).
